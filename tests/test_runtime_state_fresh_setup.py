# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fresh setup asks the next EMS start for a runtime state built from its config.

A setup replaces config.json while an EMS may still run on the old one, and a
runtime state set aside at that moment would be written back from that EMS's
memory within a cycle. So the setup only leaves a request beside the file,
naming the config it wrote by its SHA-256, and the EMS that starts on that very
config honours it: the file is kept as a backup and set aside, and the state is
built from the config's values. A request naming any other config -- a backup
restored since, a write that never arrived -- is dropped. Nothing else that
opens the runtime state touches the request.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest

import ems.runtime_state as runtime_state_module
from ems import config as ems_config
from ems.runtime_state import (
    RuntimeState,
    config_digest,
    pending_runtime_reset,
    rebind_runtime_reset,
    request_runtime_reset,
    runtime_reset_request_path,
    start_runtime_state,
)

pytestmark = [
    pytest.mark.contract,
]

OLD_STATE = {
    "system": {"enabled": False, "max_total_power": 2400, "loop_interval": 9},
    "winter": {"enabled": False},
    "devices": {"WR1": {"identity": "SN1", "enabled": False, "max_power": 600}},
}


def _defaults(max_total_power=800):
    return {
        "system": {"enabled": True, "max_total_power": max_total_power, "loop_interval": 5},
        "winter": {"enabled": True},
        "ac_charge_control": {"enabled": True},
        "devices": {"INV_1": {"identity": "SN1", "enabled": True, "max_power": 800}},
    }


def _backups(tmp_path):
    return sorted(tmp_path.glob("runtime-state.json.before-setup-*"))


def _installed(tmp_path):
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps(OLD_STATE), encoding="utf-8")
    return path


def _config(tmp_path, text='{"set up": "fresh"}\n'):
    config = tmp_path / "config.json"
    config.write_bytes(text.encode("utf-8"))
    return config, hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_a_start_on_the_set_up_config_sets_the_old_state_aside(tmp_path):
    path = _installed(tmp_path)
    old_bytes = path.read_bytes()
    config, digest = _config(tmp_path)
    request_runtime_reset(str(path), digest)

    state = start_runtime_state(str(path), _defaults(), str(config))

    assert state.data["system"] == _defaults()["system"]
    assert state.data["winter"]["enabled"] is True
    assert set(state.data["devices"]) == {"INV_1"}
    assert state.data["devices"]["INV_1"]["enabled"] is True
    (backup,) = _backups(tmp_path)
    assert backup.read_bytes() == old_bytes
    assert not os.path.exists(runtime_reset_request_path(str(path)))
    assert json.loads(path.read_text(encoding="utf-8"))["system"]["max_total_power"] == 800


def test_a_request_naming_another_config_is_dropped_and_the_state_kept(tmp_path):
    """A backup restored since the setup put a config back the request does not name."""

    path = _installed(tmp_path)
    config, digest = _config(tmp_path)
    request_runtime_reset(str(path), digest)
    config.write_text('{"restored": "backup"}\n', encoding="utf-8")

    state = start_runtime_state(str(path), _defaults(), str(config))

    assert state.data["system"]["enabled"] is False
    assert state.data["devices"]["INV_1"]["enabled"] is False
    assert not os.path.exists(runtime_reset_request_path(str(path)))
    assert _backups(tmp_path) == []


def test_a_rollback_onto_the_set_up_config_still_honours_the_request(tmp_path):
    path = _installed(tmp_path)
    config, digest = _config(tmp_path)
    set_up = config.read_bytes()
    request_runtime_reset(str(path), digest)
    config.write_text('{"restore": "attempted"}\n', encoding="utf-8")
    config.write_bytes(set_up)

    state = start_runtime_state(str(path), _defaults(), str(config))

    assert state.data["system"]["max_total_power"] == 800
    assert len(_backups(tmp_path)) == 1


def test_an_edit_carries_the_request_to_the_config_it_writes(tmp_path):
    path = _installed(tmp_path)
    config, digest = _config(tmp_path)
    request_runtime_reset(str(path), digest)
    edited, edited_digest = _config(tmp_path, '{"set up": "fresh", "edited": true}\n')

    assert rebind_runtime_reset(str(path), digest, edited_digest) is True
    state = start_runtime_state(str(path), _defaults(), str(edited))

    assert state.data["system"]["max_total_power"] == 800


def test_an_edit_never_revives_a_void_request(tmp_path):
    path = _installed(tmp_path)
    request_runtime_reset(str(path), "0" * 64)

    assert rebind_runtime_reset(str(path), "1" * 64, "2" * 64) is False
    assert pending_runtime_reset(str(path)) == "0" * 64


def _installation(tmp_path, *, legacy=False):
    root = tmp_path / "install"
    if legacy:
        config_path = root / "config.json"
        relative = "runtime-state.json"
    else:
        config_path = root / "config" / "config.json"
        relative = "data/runtime-state.json"
    config = {"system": {"runtime_state_path": relative}}
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    runtime = root / relative
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text(json.dumps(OLD_STATE), encoding="utf-8")
    return config_path, config, runtime


@pytest.mark.parametrize("legacy", [False, True], ids=["config/config.json", "root config.json"])
def test_an_edit_through_the_ems_config_writer_carries_the_request(tmp_path, legacy):
    """Guided Upgrade, emsctl, the dashboard and the startup upgrade all write through it."""

    config_path, config, runtime = _installation(tmp_path, legacy=legacy)
    request_runtime_reset(str(runtime), config_digest(config_path.read_bytes()))

    ems_config.write_config_json_atomic(str(config_path), {**config, "edited": True})

    assert pending_runtime_reset(str(runtime)) == config_digest(config_path.read_bytes())
    state = start_runtime_state(str(runtime), _defaults(), str(config_path))
    assert state.data["system"]["max_total_power"] == 800


def test_the_ems_config_writer_never_revives_a_void_request(tmp_path):
    config_path, config, runtime = _installation(tmp_path)
    request_runtime_reset(str(runtime), "0" * 64)

    ems_config.write_config_json_atomic(str(config_path), {**config, "edited": True})

    assert pending_runtime_reset(str(runtime)) == "0" * 64


def test_a_request_that_cannot_land_leaves_no_temporary_file(tmp_path, monkeypatch):
    path = _installed(tmp_path)

    def failing(*_args):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(runtime_state_module.os, "replace", failing)

    with pytest.raises(OSError):
        request_runtime_reset(str(path), "0" * 64)

    assert list(tmp_path.glob("*.tmp")) == []


def test_a_carry_that_fails_never_fails_the_written_edit_nor_sets_up_logging(
    tmp_path, monkeypatch
):
    """The startup upgrade writes through here before the EMS sets up its logging."""

    import logging

    config_path, config, runtime = _installation(tmp_path)
    request_runtime_reset(str(runtime), config_digest(config_path.read_bytes()))

    def failing(*_args):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(runtime_state_module, "carry_runtime_reset", failing)
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    level = root.level

    ems_config.write_config_json_atomic(str(config_path), {**config, "edited": True})

    assert json.loads(config_path.read_text(encoding="utf-8"))["edited"] is True
    assert root.handlers == []
    assert root.level == level


def test_an_unreadable_request_is_dropped(tmp_path):
    path = _installed(tmp_path)
    config, _digest = _config(tmp_path)
    Path(runtime_reset_request_path(str(path))).write_text("{", encoding="utf-8")

    state = start_runtime_state(str(path), _defaults(), str(config))

    assert state.data["system"]["max_total_power"] == 2400
    assert not os.path.exists(runtime_reset_request_path(str(path)))


def test_each_honoured_setup_keeps_a_backup_of_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_state_module.time, "strftime", lambda *_args: "20261011-120000")
    path = _installed(tmp_path)
    original = path.read_bytes()
    config, digest = _config(tmp_path)
    request_runtime_reset(str(path), digest)
    start_runtime_state(str(path), _defaults(), str(config))
    request_runtime_reset(str(path), digest)
    start_runtime_state(str(path), _defaults(), str(config))

    backups = _backups(tmp_path)
    assert len(backups) == 2
    assert original in [backup.read_bytes() for backup in backups]


def test_without_a_request_a_start_keeps_the_operators_values(tmp_path):
    path = _installed(tmp_path)
    config, _digest = _config(tmp_path)

    state = start_runtime_state(str(path), _defaults(), str(config))

    assert state.data["system"]["enabled"] is False
    assert state.data["system"]["max_total_power"] == 2400
    assert state.data["devices"]["INV_1"]["enabled"] is False
    assert _backups(tmp_path) == []


def test_a_request_without_a_state_file_starts_fresh_and_is_gone(tmp_path):
    path = tmp_path / "runtime-state.json"
    config, digest = _config(tmp_path)
    request_runtime_reset(str(path), digest)

    state = start_runtime_state(str(path), _defaults(), str(config))

    assert state.data["system"] == _defaults()["system"]
    assert not os.path.exists(runtime_reset_request_path(str(path)))
    assert _backups(tmp_path) == []


def test_a_running_ems_never_honours_the_request(tmp_path):
    """Its config is the old one, so honouring it would rebuild the old values."""

    path = _installed(tmp_path)
    config, digest = _config(tmp_path)
    running = RuntimeState(str(path), _defaults(max_total_power=2400))
    running.load_or_create()
    request_runtime_reset(str(path), digest)

    running.load_if_changed()
    running.load_if_changed(force=True)
    running.save_atomic()

    assert os.path.exists(runtime_reset_request_path(str(path)))
    state = start_runtime_state(str(path), _defaults(), str(config))
    assert state.data["system"]["max_total_power"] == 800
    assert state.data["system"]["enabled"] is True


def test_opening_the_state_elsewhere_leaves_the_request(tmp_path):
    """The Admin's mirror and emsctl open the file too; only an EMS start settles it."""

    path = _installed(tmp_path)
    request_runtime_reset(str(path), "0" * 64)
    before = path.read_bytes()

    reloading = RuntimeState(str(path), {"devices": {}})
    reloading.load_if_changed(force=True)
    assert path.read_bytes() == before
    opening = RuntimeState(str(path), _defaults())
    opening.load_or_create()

    assert pending_runtime_reset(str(path)) == "0" * 64
    assert _backups(tmp_path) == []


def test_the_ems_opens_its_runtime_state_through_the_start_that_settles_a_request():
    root = Path(__file__).resolve().parents[1]
    main = (root / "ems-solarflow-api-control.py").read_text(encoding="utf-8")
    body = main.split("def main():", 1)[1]

    assert "start_runtime_state(" in body
    call = body.split("start_runtime_state(", 1)[1].split("\n    )", 1)[0]
    assert "cfg.config_path()" in call
    assert "RuntimeState(" not in body
    assert body.index("if args.preflight:") < body.index("start_runtime_state(")
