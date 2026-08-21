# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the A/B integration must never let a request reach.

Every path in this feature ends at a block device, a mount, a container image or
a firmware setting. The invariant is the same everywhere: the values come from
root-owned discovery or from a revalidated operation record, never from a
request, and every privileged command is a fixed argument vector.

These are source-level contracts on purpose. A route test proves one route; a
grep over the modules proves that no second route was added later.
"""

import ast
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]
APPLIANCE = ROOT / "appliance"

AB_MODULES = (
    "ab_blocks.py",
    "ab_boot.py",
    "ab_bootstrap.py",
    "ab_health.py",
    "ab_image.py",
    "ab_inspect.py",
    "ab_layout.py",
    "ab_persistence.py",
    "ab_state.py",
    "os_artifacts.py",
    "os_releases.py",
    "os_update.py",
    "rpi_image_gen.py",
    "sparse.py",
)


def source(name):
    return (APPLIANCE / name).read_text(encoding="utf-8")


def tree(name):
    return ast.parse(source(name))


# --- no shell anywhere --------------------------------------------------------


@pytest.mark.parametrize("name", AB_MODULES)
def test_no_ab_module_uses_a_shell(name):
    text = source(name)

    assert "shell=True" not in text
    assert "os.system" not in text
    assert "os.popen" not in text


@pytest.mark.parametrize("name", AB_MODULES)
def test_no_ab_module_builds_a_command_from_a_format_string(name):
    """An interpolated argument vector is how a path becomes a command."""

    for node in ast.walk(tree(name)):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        called = getattr(target, "attr", getattr(target, "id", ""))
        if called not in ("run", "Popen", "check_output", "call"):
            continue
        for argument in node.args:
            assert not isinstance(argument, ast.JoinedStr), (name, called)
            assert not isinstance(argument, ast.BinOp), (name, called)


def imports_subprocess(name):
    """Checked against the syntax tree; prose about subprocesses is not one."""

    for node in ast.walk(tree(name)):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == "subprocess" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "subprocess":
                return True
    return False


def test_the_only_subprocess_in_the_ab_modules_is_the_artifact_decompressor():
    """One reviewed exception, with a fixed argv and no shell."""

    users = [name for name in AB_MODULES if imports_subprocess(name)]

    assert users == ["os_artifacts.py"]
    assert 'subprocess.Popen(\n        [_zstd_binary(), "-d", "-c", "--", str(path)],' in source(
        "os_artifacts.py"
    )


# --- nothing privileged is named by a request ---------------------------------


@pytest.mark.parametrize("name", AB_MODULES)
def test_no_ab_module_reads_a_request_body(name):
    """Checked against the syntax tree; prose about requests is not a request."""

    for node in ast.walk(tree(name)):
        if isinstance(node, ast.Name):
            assert node.id not in ("request", "flask"), name
        if isinstance(node, ast.Attribute):
            base = getattr(node.value, "id", "")
            assert base not in ("request", "flask"), name
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            names = {alias.name for alias in node.names}
            assert "flask" not in module, name
            assert not names & {"flask", "request"}, name


def test_the_write_authority_is_the_only_source_of_a_block_device():
    """Every device the writer opens came out of the revalidated authority."""

    text = source("os_update.py")

    assert "authority.boot_device" in text
    assert "authority.root_device" in text
    # The plan is rebuilt from discovery immediately before the first write.
    assert "def _revalidate" in text
    assert "write_would_touch_active_slot" in text


def test_the_inspector_mounts_only_what_the_authority_names():
    text = source("ab_inspect.py")

    assert "authority.root_device" in text
    assert "authority.boot_device" in text
    assert 'MOUNT_ROOT = "/run/ems-appliance-manager/ab-inspect"' in text
    # The mount target is derived from a constant root, never from an argument.
    assert "base = self._path(self.mount_root)" in text


def test_the_mount_options_are_fixed_and_never_composed():
    text = source("ab_inspect.py")

    assert 'MOUNT_OPTIONS = "ro,nosuid,nodev,noexec"' in text
    assert '"mount", ["-o", MOUNT_OPTIONS, str(device), str(target)]' in text


def test_the_trial_reboot_argument_is_a_constant():
    text = source("ab_boot.py")

    assert 'REBOOT_TRYBOOT_ARGUMENT = "0 tryboot"' in text
    assert '"systemctl", ["reboot", REBOOT_TRYBOOT_OPTION]' in text


def test_the_generator_path_never_reaches_the_agent_or_the_web_service():
    """A build-host checkout is an operator's command line, not an API input."""

    for name in ("agent.py", "protocol.py", "commands.py", "status.py"):
        text = (APPLIANCE / name).read_text(encoding="utf-8")
        assert "rpi_image_gen" not in text, name
        assert "EMS_RPI_IMAGE_GEN" not in text, name


# --- artifacts ----------------------------------------------------------------


def test_an_update_url_is_never_accepted_from_anywhere():
    """Artifacts come from a root-owned release directory, never from a link."""

    text = source("os_releases.py")

    assert "urlopen" not in text
    assert "requests." not in text
    assert "http://" not in text.replace("https://github.com", "")


def test_a_mutable_tag_is_never_an_os_or_runtime_authority():
    assert "runtime_reference_not_pinned" in source("ab_bootstrap.py")
    assert '"@sha256:" in str(reference)' in source("ab_bootstrap.py")


def test_the_signing_key_is_never_named_by_anything_but_the_host_configuration():
    text = source("os_releases.py")

    assert "config.os_release_keyring" not in text
    assert "--keyring" in text


def test_extraction_refuses_every_member_that_could_escape_staging():
    text = source("os_artifacts.py")

    for guard in (
        "a parent-directory traversal",
        "an absolute path",
        "links are never extracted",
        "device nodes are never extracted",
        "not a member this appliance knows how to write",
    ):
        assert guard in text, guard


# --- the active slot -----------------------------------------------------------


def test_the_active_slot_can_never_be_the_write_target():
    text = source("ab_layout.py")

    assert "inactive_slot_is_active" in text
    assert "inactive_slot_mounted" in text
    assert "inactive_slot_foreign_device" in text


def test_a_slot_never_commits_itself_without_a_healthy_trial():
    text = source("ab_health.py")

    assert "commit_not_authorised" in text
    assert "if not report.healthy:" in text


def test_persistence_failure_is_never_repaired_by_a_status_call():
    text = source("ab_persistence.py")

    assert "def verify(" in text
    for mutating in ("os.mkdir", "shutil.", "open(", "subprocess"):
        assert mutating not in text, mutating


# --- the block-write guard -----------------------------------------------------


def test_development_storage_is_never_writable_by_a_test():
    text = source("ab_blocks.py")

    for device in ("/dev/mmcblk0", "/dev/nvme0n1", "/dev/sda", "/dev/vda"):
        assert device in text, device
    assert "EMS_APPLIANCE_AB_BLOCK_WRITE" in text
    assert "the caller is not root" in text


def test_every_privileged_tool_is_allowlisted():
    """A tool the runner cannot resolve cannot be run at all."""

    text = (APPLIANCE / "commands.py").read_text(encoding="utf-8")
    allowlist = set(re.findall(r'^\s{4}"([a-z0-9.\-]+)":', text, re.M))

    for tool in ("mount", "umount", "lsblk", "zstd", "systemctl"):
        assert tool in allowlist, tool
    for absent in ("sh", "bash", "sfdisk", "growpart", "resize2fs", "dd"):
        assert absent not in allowlist, absent


# --- what a request still cannot name -----------------------------------------

# One entry per identity the integration introduced. Each is derived from
# verified runtime discovery, signed release metadata, root-owned configuration
# or pinned upstream metadata, and a request that carried one would be a request
# that chose it.
NEVER_FROM_A_REQUEST = (
    "device_layer",
    "hardware_profile",
    "compatible_board_classes",
    "board_class",
    "encoding",
    "expanded_sha256",
    "expanded_size",
    "sparse_decoder",
    "zstd",
    "rpi_image_gen",
    "boot_device",
    "root_device",
    "staging_dir",
    "admin_url",
    "key_directory",
    "shared_root",
    "compose_file",
)


def ab_request_fields():
    """Every field the agent protocol accepts for an A/B operation."""

    from appliance import protocol

    fields = set()
    for name, spec in protocol.OPERATIONS.items():
        if not str(name).startswith("ab."):
            continue
        for field in getattr(spec, "fields", ()) or ():
            fields.add(field.name)
    return fields


def test_the_ab_protocol_accepts_only_a_release_id_and_an_operation_id():
    assert ab_request_fields() == {"release_id", "repair", "operation_id"}


@pytest.mark.parametrize("name", NEVER_FROM_A_REQUEST)
def test_no_ab_request_field_can_name_a_privileged_identity(name):
    assert not any(name in field for field in ab_request_fields()), name


@pytest.mark.parametrize("name", NEVER_FROM_A_REQUEST)
def test_the_web_service_never_forwards_a_privileged_identity(name):
    """The route table is the whole surface; anything not listed cannot pass."""

    routes = (APPLIANCE / "web.py").read_text(encoding="utf-8")
    block = routes.split('"/api/ab/plan-update"', 1)[1].split('"/api/ab/plan-rollback"', 1)[0]

    assert name not in block, name


def test_the_plan_update_route_forwards_exactly_two_values():
    routes = (APPLIANCE / "web.py").read_text(encoding="utf-8")
    block = routes.split('"/api/ab/plan-update"', 1)[1].split("\n            ),", 1)[0]

    assert 'b.get("release_id")' in block
    assert 'bool(b.get("repair"))' in block
    assert block.count("b.get(") == 2


def test_the_sparse_decoder_is_not_selectable_at_all():
    """There is no decoder path to supply: the expander is in-process."""

    text = source("sparse.py")

    assert "simg2img" not in text
    assert "import subprocess" not in text
    assert "def expand(" in text


def test_the_expanded_output_path_is_derived_from_the_staging_directory():
    text = source("os_update.py")

    assert 'destination = staged.directory / f"{member.name}.img"' in text


def test_the_admin_health_url_comes_from_the_host_configuration():
    """A request-supplied URL would make the health gate probe anything."""

    text = (APPLIANCE / "services.py").read_text(encoding="utf-8")

    assert "admin_url=config.admin_health_url" in text
    assert "http://" not in (APPLIANCE / "ab_health.py").read_text(encoding="utf-8")


def test_the_ssh_host_key_directory_is_a_constant():
    text = (APPLIANCE / "host_identity.py").read_text(encoding="utf-8")

    assert "KEY_DIRECTORY = ab_persistence.SSH_HOST_KEY_DIRECTORY" in text
    assert "request" not in text


def test_the_shared_persistence_paths_are_a_constant():
    text = source("ab_persistence.py")

    assert "SHARED_PATHS = (" in text
    assert "def slot_shared_conf():" in text
    assert "input(" not in text


def test_the_board_class_comes_from_the_device_tree_only():
    text = source("rpi_image_gen.py")

    assert 'Path(root) / "proc/device-tree/compatible"' in text
    assert "BOARD_CLASSES = {" in text


def test_the_hardware_profile_table_is_not_configurable():
    text = source("rpi_image_gen.py")

    assert "HARDWARE_PROFILES = {" in text
    # Derived from the device layer, never declared beside it.
    assert "compatible_board_classes=(" in text
