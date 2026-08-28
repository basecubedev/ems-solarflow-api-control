# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a flashed card exposes over SSH before anyone has configured it.

Two documents describe an appliance that does not answer SSH until an operator
turns it on, and does not accept the rescue password over the network at all:
``docs/appliance/ssh-backup-access.md`` ("**Enable SSH** enables and starts the
service") and ``docs/appliance/console-recovery.md`` ("``ems-rescue`` is a
password account and the shipped sshd policy does not accept passwords").
``appliance/ssh_service.py`` says the same in its module docstring.

The image built neither. ``openssh-server`` is in the package list and Debian's
postinst enables it in the build chroot, and the only ``PasswordAuthentication
no`` this project wrote sat inside a ``Match User ems-backup`` block. So the box
came up on the LAN answering SSH, and ``ems-rescue`` -- a sudo account whose
password is printed in this repository -- was a valid login for anyone who found
it. The owner was told the opposite.

Both halves are asserted by running the thing rather than reading it: the layer
hook is executed against a fabricated root filesystem, and the policy is
rendered.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
LAYER = ROOT / "packaging" / "appliance" / "image" / "layer" / "ems-appliance.yaml"

ENABLEMENT_LINKS = (
    "multi-user.target.wants/ssh.service",
    "sockets.target.wants/ssh.socket",
    "sshd.service",
)


def ssh_hook():
    """The one customize hook that switches SSH off, found by what it does."""

    body = yaml.safe_load(LAYER.read_text(encoding="utf-8").split("# METAEND\n---\n", 1)[1])
    hooks = [
        hook
        for hook in body["mmdebstrap"]["customize-hooks"]
        if "ssh.service" in str(hook) and "disable" in str(hook)
    ]
    assert len(hooks) == 1, f"expected exactly one ssh-disabling hook, found {len(hooks)}"
    return hooks[0]


def rootfs(tmp_path, links=ENABLEMENT_LINKS):
    """A root filesystem with ssh enabled the way Debian's postinst leaves it."""

    root = tmp_path / "rootfs"
    units = root / "etc" / "systemd" / "system"
    (root / "usr" / "lib" / "systemd" / "system").mkdir(parents=True)
    for name in ("ssh.service", "ssh.socket"):
        (root / "usr" / "lib" / "systemd" / "system" / name).write_text("[Unit]\n")
    for link in links:
        path = units / link
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(f"/usr/lib/systemd/system/{Path(link).name}")
    return root


def run_hook(hook, root):
    return subprocess.run(
        ["sh", "-c", hook, "hook", str(root)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def remaining(root):
    units = root / "etc" / "systemd" / "system"
    return sorted(
        str(path.relative_to(units))
        for path in units.rglob("*")
        if path.name in ("ssh.service", "ssh.socket", "sshd.service")
    )


def test_the_image_ships_ssh_switched_off(tmp_path):
    """The service, the socket and the alias -- any one of them answers."""

    root = rootfs(tmp_path)
    assert remaining(root) != [], "the fixture did not enable anything"

    result = run_hook(ssh_hook(), root)

    assert result.returncode == 0, result.stderr
    assert remaining(root) == []


def test_a_socket_unit_alone_is_still_ssh_enabled(tmp_path):
    """Trixie can activate sshd from the socket with the service disabled, so
    disabling only ``ssh.service`` would look like a fix and change nothing."""

    root = rootfs(tmp_path, links=("sockets.target.wants/ssh.socket",))

    result = run_hook(ssh_hook(), root)

    assert result.returncode == 0, result.stderr
    assert remaining(root) == []


def test_the_hook_refuses_an_image_it_could_not_switch_off(tmp_path):
    """The build fails loudly rather than publishing a card that answers SSH.

    Verified by giving it a link it cannot remove: the check is what carries the
    guarantee, not the removal above it.
    """

    root = rootfs(tmp_path, links=())
    units = root / "etc" / "systemd" / "system" / "multi-user.target.wants"
    units.mkdir(parents=True)
    (units / "ssh.service").symlink_to("/usr/lib/systemd/system/ssh.service")
    units.chmod(0o555)
    try:
        result = run_hook(ssh_hook(), root)
    finally:
        units.chmod(0o755)

    assert result.returncode == 1
    assert "still enabled" in result.stderr


def test_the_rescue_password_is_refused_over_ssh():
    """``ems-rescue`` is sudo-capable and its password is published in
    docs/appliance/console-recovery.md. It is a console account, and the shipped
    policy now says so where sshd reads it."""

    from appliance.config import ApplianceConfig
    from appliance.host_config import render_sshd_policy
    from appliance.paths import AppliancePaths

    text = render_sshd_policy(
        AppliancePaths(
            install_root=Path("/opt/ems-solarflow"),
            config_dir=Path("/etc/ems-appliance-manager"),
            state_dir=Path("/var/lib/ems-appliance-manager"),
            log_dir=Path("/var/log/ems-appliance-manager"),
            runtime_dir=Path("/run/ems-appliance-manager"),
            export_root=Path("/srv/ems-appliance-export"),
        ),
        ApplianceConfig(),
    )
    block = text.split("Match User ems-rescue\n", 1)[1].split("Match User", 1)[0]

    assert "PasswordAuthentication no" in block
    assert "KbdInteractiveAuthentication no" in block, (
        "PasswordAuthentication alone leaves PAM's keyboard-interactive path, "
        "which asks for the same password"
    )


def test_the_policy_is_scoped_and_never_global():
    """The package installs on a Raspberry Pi somebody already administers over
    a password login. A global policy here would lock them out of their own
    machine, so every directive belongs to a Match block."""

    from appliance.config import ApplianceConfig
    from appliance.host_config import render_sshd_policy
    from appliance.paths import AppliancePaths

    text = render_sshd_policy(
        AppliancePaths(
            install_root=Path("/opt/ems-solarflow"),
            config_dir=Path("/etc/ems-appliance-manager"),
            state_dir=Path("/var/lib/ems-appliance-manager"),
            log_dir=Path("/var/log/ems-appliance-manager"),
            runtime_dir=Path("/run/ems-appliance-manager"),
            export_root=Path("/srv/ems-appliance-export"),
        ),
        ApplianceConfig(),
    )
    directives = [
        line
        for line in text.splitlines()
        if line and not line.startswith(("#", " ", "\t", "Match "))
    ]

    assert directives == [], f"these apply to every account on the host: {directives}"


def find_sshd():
    for candidate in ("sshd", "/usr/sbin/sshd", "/usr/local/sbin/sshd"):
        found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if found:
            return found
    return None


@pytest.mark.skipif(find_sshd() is None, reason="no sshd to ask; the parser is the authority here")
def test_real_sshd_confines_the_two_accounts_and_nobody_else(tmp_path):
    """Asked of the parser rather than of the file.

    Two ``Match`` blocks in one drop-in, and what each account ends up with is a
    question about how sshd scopes them -- not about what the generator wrote.
    ``sshd -T -C user=...`` answers it exactly, and it is the same program the
    appliance runs ``sshd -t`` against before it will enable the backup account.
    """

    from appliance.config import ApplianceConfig
    from appliance.host_config import render_sshd_policy
    from appliance.paths import AppliancePaths

    policy = render_sshd_policy(
        AppliancePaths(
            install_root=Path("/opt/ems-solarflow"),
            config_dir=Path("/etc/ems-appliance-manager"),
            state_dir=Path("/var/lib/ems-appliance-manager"),
            log_dir=Path("/var/log/ems-appliance-manager"),
            runtime_dir=Path("/run/ems-appliance-manager"),
            export_root=Path("/srv/ems-appliance-export"),
        ),
        ApplianceConfig(),
    )
    key = tmp_path / "hostkey"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                   check=True, capture_output=True, timeout=60)
    config = tmp_path / "sshd_config"
    config.write_text(f"HostKey {key}\n" + policy, encoding="utf-8")
    sshd = find_sshd()

    syntax = subprocess.run([sshd, "-t", "-f", str(config)],
                            capture_output=True, text=True, timeout=60)
    assert syntax.returncode == 0, syntax.stderr

    def effective(user):
        result = subprocess.run(
            [sshd, "-T", "-C", f"user={user},host=h,addr=1.2.3.4", "-f", str(config)],
            capture_output=True, text=True, check=True, timeout=60,
        )
        return dict(
            line.split(" ", 1) for line in result.stdout.splitlines() if " " in line
        )

    rescue = effective("ems-rescue")
    assert rescue["passwordauthentication"] == "no"
    assert rescue["kbdinteractiveauthentication"] == "no"
    assert rescue["pubkeyauthentication"] == "yes", "console-recovery.md offers a key login"

    backup = effective("ems-backup")
    assert backup["passwordauthentication"] == "no"
    assert backup["chrootdirectory"] == "/srv/ems-appliance-export", (
        "the rescue block above swallowed the backup account's confinement"
    )
    assert backup["forcecommand"].startswith("internal-sftp")

    other = effective("somebody-else")
    assert other["passwordauthentication"] == "yes", (
        "this package would lock the owner out of a Raspberry Pi they already administer"
    )


UNIT = ROOT / "packaging" / "appliance" / "systemd" / "ems-appliance-sshd-keys.service"


def unit_fields():
    fields = {}
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields.setdefault(key.strip(), []).append(value.strip())
    return fields


@pytest.mark.skipif(find_sshd() is None, reason="the premise is what sshd does without a key")
def test_sshd_refuses_to_start_at_all_without_a_host_key(tmp_path):
    """Why the appliance has to make its own.

    The image deletes the pair the build chroot made -- correctly, a private key
    in a public artefact is compromised. What used to replace it was Debian's
    sshd-keygen.service, which is ``WantedBy=ssh.service sshd.service
    sshd@.service ssh.socket`` and never ``multi-user.target``: it runs only when
    an ssh unit starts. Once the image ships ssh switched off, nothing ever
    starts one, and ``ConditionFirstBoot=yes`` has passed by the time an owner
    presses Enable SSH.

    What that leaves is not "SSH is off". It is a host sshd cannot run on, and
    ``ssh.service`` has ``ExecStartPre=/usr/sbin/sshd -t``.
    """

    config = tmp_path / "sshd_config"
    config.write_text("Port 22\n", encoding="utf-8")

    result = subprocess.run([find_sshd(), "-t", "-f", str(config)],
                            capture_output=True, text=True, timeout=60)

    assert result.returncode != 0
    assert "no hostkeys available" in result.stderr


def test_the_appliance_makes_its_own_host_identity():
    """And on its own schedule, not on an ssh unit's."""

    fields = unit_fields()

    assert UNIT.is_file()
    assert any("ssh-keygen -A" in value for value in fields.get("ExecStart", []))
    assert fields["WantedBy"] == ["multi-user.target"], (
        "hung off an ssh unit, this repeats the failure it exists to fix"
    )


def test_the_keys_exist_before_anything_judges_the_sshd_policy():
    """ems-appliance-export.service runs `backup-access activate` as
    ExecStartPost, and backup_confinement reads a failing `sshd -t` as an invalid
    policy: it renames authorized_keys and runs `chage -E 1` on the backup
    account. A boot that checked the policy before the keys existed would
    silently disable the account it had just set up."""

    before = " ".join(unit_fields().get("Before", []))

    assert "ems-appliance-export.service" in before
    assert "ssh.service" in before


def test_the_unit_is_shipped_and_enabled_on_both_install_paths():
    """A unit that is packaged and never enabled is the same as no unit."""

    build = (ROOT / "packaging" / "appliance" / "build-deb.sh").read_text(encoding="utf-8")
    postinst = (ROOT / "packaging" / "appliance" / "debian" / "postinst").read_text(encoding="utf-8")
    layer = LAYER.read_text(encoding="utf-8")

    assert "ems-appliance-sshd-keys.service" in build, "not installed into the package"
    assert "ems-appliance-sshd-keys.service" in postinst, "not enabled on a .deb install"
    assert "ems-appliance-sshd-keys.service" in layer.split("enable-units", 1)[1], (
        "not enabled in the image"
    )
