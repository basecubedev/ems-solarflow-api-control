# SPDX-License-Identifier: AGPL-3.0-or-later
"""The installer and the reverter, run in sequence on one state directory.

They were tested apart, never one after the other, which is how a revert the
installer had already made could be made a second time by the deadline that
judged the same install. These drive the real scripts against a fake dpkg
whose database both of them read.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from appliance import manager_verify

pytestmark = [pytest.mark.integration, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "packaging" / "appliance" / "bin" / "install-manager.sh"
REVERTER = ROOT / "packaging" / "appliance" / "bin" / "verify-manager.sh"


class _Runner:
    def available(self, tool):
        return True

    def run(self, tool, args, timeout=None):
        class _Ok:
            ok = True
            stdout = ""
            stderr = ""

        return _Ok()


class _Paths:
    def __init__(self, packages_dir):
        self.packages_dir = packages_dir


def _tools(directory, *, dpkg_body):
    """dpkg and dpkg-query sharing one database file; systemctl that agrees."""

    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "calls.log"
    db = directory / "db"
    db.write_text("installed|0.3.0\n", encoding="utf-8")
    (directory / "dpkg").write_text(
        f'#!/bin/sh\necho "dpkg $*" >> "{log}"\n' + dpkg_body.replace("DB", str(db)),
        encoding="utf-8",
    )
    (directory / "dpkg-query").write_text(
        "#!/bin/sh\n"
        "fmt=; want=\n"
        'for a in "$@"; do if [ "$want" = yes ]; then fmt=$a; want=; continue; fi; '
        '[ "$a" = -f ] && want=yes; done\n'
        f's=$(cut -d"|" -f1 < "{db}"); v=$(cut -d"|" -f2 < "{db}")\n'
        'printf %s "$fmt" | sed -e "s/[$]{Version}/$v/g" -e "s/[$]{db:Status-Status}/$s/g"\n',
        encoding="utf-8",
    )
    (directory / "systemctl").write_text(
        f'#!/bin/sh\necho "systemctl $*" >> "{log}"\nexit 0\n', encoding="utf-8"
    )
    (directory / "ss").write_text(
        '#!/bin/sh\necho "LISTEN 0 5 0.0.0.0:8088 0.0.0.0:*"\nexit 0\n', encoding="utf-8"
    )
    for name in ("dpkg", "dpkg-query", "systemctl", "ss"):
        (directory / name).chmod(0o755)
    return log, db


def _run(script, tools, *args):
    environment = dict(os.environ)
    environment["PATH"] = f"{tools}:{environment['PATH']}"
    return subprocess.run(
        ["sh", str(script), *args], capture_output=True, text=True, env=environment, timeout=60
    )


def _armed_install(tmp_path, *, armed_ago):
    """An install of 0.4.0 over a running 0.3.0, its deadline armed ``armed_ago`` s ago."""

    import time

    state = tmp_path / "packages"
    state.mkdir()
    (state / "current.deb").write_bytes(b"B 0.4.0")
    (state / "previous.deb").write_bytes(b"A 0.3.0, the package that was running")
    (state / "install-request.json").write_text(
        json.dumps(
            {"archive": str(state / "current.deb"), "version": "0.4.0", "build_id": "b", "sha256": "x"}
        ),
        encoding="utf-8",
    )
    packaged = tmp_path / "verify-manager.sh"
    packaged.write_text(REVERTER.read_text(encoding="utf-8"), encoding="utf-8")
    manager_verify.arm(
        _Paths(state),
        _Runner(),
        expected_version="0.4.0",
        build_id="b",
        previous=str(state / "previous.deb"),
        now=int(time.time()) - armed_ago,
        window_seconds=900,
        reverter=str(packaged),
    )
    installer = state / "install-manager-under-test.sh"
    installer.write_text(
        re.sub(r"^STATE=.*$", f'STATE="{state}"', INSTALLER.read_text(encoding="utf-8"), count=1, flags=re.M),
        encoding="utf-8",
    )
    return state, installer


POSTINST_FAILS = (
    'case "$*" in\n'
    "  *--configure*) exit 1 ;;\n"
    '  *previous.deb*) printf "installed|0.3.0\\n" > "DB"; exit 0 ;;\n'
    '  *current.deb*) printf "half-configured|0.4.0\\n" > "DB"; exit 1 ;;\n'
    "esac\n"
    "exit 0\n"
)


def test_a_revert_the_installer_made_is_not_made_again_by_the_deadline(tmp_path):
    state, installer = _armed_install(tmp_path, armed_ago=2000)
    log, db = _tools(tmp_path / "tools", dpkg_body=POSTINST_FAILS)

    _run(installer, tmp_path / "tools")
    reinstalls = log.read_text(encoding="utf-8").count(f"--install {state / 'previous.deb'}")
    _run(state / "verify-manager.armed.sh", tmp_path / "tools", str(state))

    assert db.read_text(encoding="utf-8").strip() == "installed|0.3.0"
    assert reinstalls == 1
    assert log.read_text(encoding="utf-8").count(f"--install {state / 'previous.deb'}") == 1
    verdict = manager_verify.read_verdict(_Paths(state)).to_dict()
    assert verdict["verdict"] == "reverted" and verdict["settled"] is True, verdict


def test_an_install_that_went_through_is_confirmed_by_the_ticks_that_follow(tmp_path):
    """A healthy look confirms only when the next tick finds the same processes."""

    state, installer = _armed_install(tmp_path, armed_ago=60)
    log, db = _tools(
        tmp_path / "tools",
        dpkg_body='case "$*" in *current.deb*) printf "installed|0.4.0\\n" > "DB" ;; esac\nexit 0\n',
    )

    first = _run(installer, tmp_path / "tools")
    for _ in range(2):
        _run(state / "verify-manager.armed.sh", tmp_path / "tools", str(state))

    assert first.returncode == 0, first.stderr
    assert db.read_text(encoding="utf-8").strip() == "installed|0.4.0"
    assert "previous.deb" not in log.read_text(encoding="utf-8").split("current.deb", 1)[-1]
    verdict = manager_verify.read_verdict(_Paths(state)).to_dict()
    assert verdict["verdict"] == "confirmed", verdict
