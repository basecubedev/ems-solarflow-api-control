# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only EMS CLI diagnostics bridge tests (no Docker/hardware/network)."""

import json
import subprocess

import pytest

from admin.ems_cli import (
    CHECK_ORDER,
    CHECKS,
    STDERR_CAP,
    STDOUT_CAP,
    UNAVAILABLE_MESSAGE,
    EmsCliDiagnostics,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.integration,
    pytest.mark.simulation,
]


COMPOSE_TEXT = """
services:
  ems:
    image: ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.1
    container_name: ems-solarflow-api-control
  influxdb:
    image: influxdb:2.7
    container_name: ems-influxdb
"""


class FakeDocker:
    def __init__(self, ready=True, container=None, raise_on_probe=False):
        self._ready = ready
        self._container = container
        self._raise_on_probe = raise_on_probe

    def probe(self):
        if self._raise_on_probe:
            raise RuntimeError("docker exploded")
        return {"state": "ready" if self._ready else "socket_missing"}

    def inspect_container(self, name):
        if self._container is None:
            return None
        return dict(self._container)


class FakeRun:
    """Records argv and returns scripted CompletedProcess-like results."""

    def __init__(self, results=None, default=None):
        self._results = results or {}
        self._default = default or subprocess.CompletedProcess([], 0, "", "")
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "kwargs": kwargs})
        for suffix, result in self._results.items():
            if tuple(argv[-len(suffix):]) == suffix:
                if isinstance(result, Exception):
                    raise result
                return result
        return self._default


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _standard_install(base_dir, with_compose=True, with_emsctl=False):
    (base_dir / "config").mkdir()
    (base_dir / "config" / "config.json").write_text(
        json.dumps({"dashboard": {"port": 8080}}), encoding="utf-8"
    )
    (base_dir / "data").mkdir()
    if with_compose:
        (base_dir / "docker-compose.yml").write_text(COMPOSE_TEXT, encoding="utf-8")
    if with_emsctl:
        (base_dir / "emsctl.py").write_text("# emsctl\n", encoding="utf-8")


def _running_ems():
    return {
        "container_name": "ems-solarflow-api-control",
        "image": "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.1",
        "status": "running",
    }


def _service(base_dir, docker=None, run=None):
    from admin.install_context import detect_install_context

    return EmsCliDiagnostics(
        install_context_provider=lambda: detect_install_context(base_dir=str(base_dir)),
        docker=docker,
        run=run,
    )


# --- allowlist ------------------------------------------------------------


def test_a_check_marked_to_warn_warns_when_it_is_killed_too():
    """`warn_on_fail` has to survive the timeout path.

    A subsystem that is merely unreachable warns; one that is too slow to answer
    is the same situation seen through a stopwatch. Reporting a hard failure
    there fails the guided-upgrade health gate closed over a slow InfluxDB,
    which is what the flag exists to prevent.
    """

    import subprocess

    from admin.ems_cli import CHECKS, EmsCliDiagnostics

    def _timing_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    diagnostics = EmsCliDiagnostics(run=_timing_out)
    result = diagnostics.run(check_ids=("influx_status", "runtime_status"))

    by_id = {check["id"]: check for check in result["checks"]}

    assert CHECKS["influx_status"]["warn_on_fail"] is True
    assert by_id["influx_status"]["status"] == "warning"
    assert CHECKS["runtime_status"]["warn_on_fail"] is False
    assert by_id["runtime_status"]["status"] == "timeout"

    # On its own the InfluxDB check no longer fails the run, which is what the
    # guided-upgrade gate reads (it accepts "ok" and "warning", nothing else).
    influx_only = diagnostics.run(check_ids=("influx_status",))

    assert influx_only["summary"]["status"] == "warning"


def test_allowlist_maps_named_checks_to_exact_argv(tmp_path):
    _standard_install(tmp_path)
    run = FakeRun(default=_completed(0, "{}"))
    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    service.run()

    from admin.ems_tool import EXEC_GUARD

    argvs = [call["argv"] for call in run.calls]
    # Container mode always goes through
    # `docker exec <name> timeout ... python3 /app/emsctl.py`, and the guard is
    # the only thing standing between the container and the interpreter.
    for argv in argvs:
        assert argv[:3] == ["docker", "exec", "ems-solarflow-api-control"]
        assert argv[3] == EXEC_GUARD
        assert argv[7:9] == ["python3", "/app/emsctl.py"]
    suffixes = {tuple(argv[9:]) for argv in argvs}
    assert ("diagnose", "--json") in suffixes
    assert ("config", "upgrade", "--dry-run") in suffixes
    assert ("influx", "status", "--json") in suffixes
    assert ("status",) in suffixes


def test_allowlist_has_no_mutating_commands():
    joined = [" ".join(spec["args"]) for spec in CHECKS.values()]
    forbidden = [
        "--support-bundle",
        "--hardware",
        "--control-quality",
        "--yes",
        "backup",
        "restore",
        "init",
        "sync",
        "stack",
    ]
    for spec_args in joined:
        for word in forbidden:
            assert word not in spec_args
    # config upgrade is only ever the dry-run variant.
    assert CHECKS["config_upgrade_dry_run"]["args"] == ("config", "upgrade", "--dry-run")


def test_run_ignores_unknown_check_ids(tmp_path):
    _standard_install(tmp_path)
    run = FakeRun(default=_completed(0, "{}"))
    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    result = service.run(check_ids=["quick_diagnose", "rm -rf /", "backup_create"])

    assert [check["id"] for check in result["checks"]] == ["quick_diagnose"]


def test_run_never_uses_shell(tmp_path):
    _standard_install(tmp_path)
    run = FakeRun(default=_completed(0, "{}"))
    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    service.run()

    for call in run.calls:
        assert "shell" not in call["kwargs"]
        assert isinstance(call["argv"], list)


# --- mode resolution ------------------------------------------------------


def test_container_mode_when_ems_container_running(tmp_path):
    _standard_install(tmp_path)
    run = FakeRun(default=_completed(0, "{}"))
    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    result = service.run()

    assert result["available"] is True
    assert result["mode"] == "container"
    assert result["container"] == "ems-solarflow-api-control"


def test_local_mode_when_no_container_but_emsctl_present(tmp_path):
    _standard_install(tmp_path, with_emsctl=True)
    run = FakeRun(default=_completed(0, "ok"))
    # Docker present but no running container -> local fallback.
    service = _service(tmp_path, docker=FakeDocker(container=None), run=run)

    result = service.run()

    assert result["mode"] == "local"
    assert result["container"] is None
    argv = run.calls[0]["argv"]
    assert argv[0] == "python3"
    assert argv[1].endswith("emsctl.py")
    assert argv[2] == "--config"
    assert argv[3].endswith("config.json")
    # Local mode runs with the install root as the working directory.
    assert run.calls[0]["kwargs"]["cwd"].endswith(str(tmp_path))


def test_local_mode_when_docker_absent(tmp_path):
    _standard_install(tmp_path, with_emsctl=True)
    run = FakeRun(default=_completed(0, "ok"))
    service = _service(tmp_path, docker=FakeDocker(ready=False), run=run)

    result = service.run()

    assert result["mode"] == "local"


def test_unavailable_when_no_container_and_no_emsctl(tmp_path):
    _standard_install(tmp_path, with_emsctl=False)
    run = FakeRun(default=_completed(0, "ok"))
    service = _service(tmp_path, docker=FakeDocker(container=None), run=run)

    result = service.run()

    assert result["available"] is False
    assert result["mode"] == "unavailable"
    assert result["checks"] == []
    assert result["message"] == UNAVAILABLE_MESSAGE
    assert result["summary"]["status"] == "unavailable"
    # Nothing was executed in unavailable mode.
    assert run.calls == []


def test_docker_probe_exception_falls_back_to_local(tmp_path):
    _standard_install(tmp_path, with_emsctl=True)
    run = FakeRun(default=_completed(0, "ok"))
    service = _service(tmp_path, docker=FakeDocker(raise_on_probe=True), run=run)

    result = service.run()

    assert result["mode"] == "local"


def test_stopped_container_is_not_used(tmp_path):
    _standard_install(tmp_path, with_emsctl=True)
    stopped = dict(_running_ems(), status="exited")
    run = FakeRun(default=_completed(0, "ok"))
    service = _service(tmp_path, docker=FakeDocker(container=stopped), run=run)

    result = service.run()

    assert result["mode"] == "local"


# --- result shaping -------------------------------------------------------


def test_json_parse_success(tmp_path):
    _standard_install(tmp_path)
    payload = {"diagnosis": {"status": "ok"}}
    run = FakeRun(
        results={("diagnose", "--json"): _completed(0, json.dumps(payload))},
        default=_completed(0, "text"),
    )
    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    result = service.run(check_ids=["quick_diagnose"])
    check = result["checks"][0]

    assert check["status"] == "ok"
    assert check["exit_code"] == 0
    assert check["json"] == payload


def test_json_parse_failure_keeps_raw_and_null_json(tmp_path):
    _standard_install(tmp_path)
    run = FakeRun(
        results={("diagnose", "--json"): _completed(0, "not json at all")},
    )
    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    result = service.run(check_ids=["quick_diagnose"])
    check = result["checks"][0]

    assert check["status"] == "ok"
    assert check["json"] is None
    assert check["stdout"] == "not json at all"


def test_influx_enabled_failure_is_a_warning_not_a_failure(tmp_path):
    # An enabled backend that is unreachable is a real (warn-level) problem.
    _standard_install(tmp_path)
    run = FakeRun(
        results={
            ("influx", "status", "--json"): _completed(1, "", "connection refused"),
        },
        default=_completed(0, "{}"),
    )
    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    result = service.run(check_ids=["influx_status"])
    check = result["checks"][0]

    assert check["status"] == "warning"
    assert check["exit_code"] == 1
    assert result["summary"]["warning"] == 1
    assert result["summary"]["status"] == "warning"


def test_influx_disabled_in_config_is_expected_not_warning(tmp_path):
    from admin.ems_cli import INFLUX_DISABLED_MESSAGE

    _standard_install(tmp_path)
    stderr = "ERROR: influxdb is disabled in config (set influxdb.enabled = true)"
    run = FakeRun(
        results={("influx", "status", "--json"): _completed(2, "", stderr)},
        default=_completed(0, "{}"),
    )
    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    result = service.run(check_ids=["influx_status"])
    check = result["checks"][0]

    assert check["status"] == "disabled"
    assert check["message"] == INFLUX_DISABLED_MESSAGE
    # Raw stderr stays available for transparency.
    assert stderr in check["stderr"]
    # Disabled-by-config never escalates the overall diagnostics summary.
    assert result["summary"]["disabled"] == 1
    assert result["summary"]["warning"] == 0
    assert result["summary"]["status"] == "ok"


def test_influx_disabled_marker_on_stdout_is_also_expected(tmp_path):
    # Some builds emit the disabled notice on stdout; detection is stream-agnostic.
    _standard_install(tmp_path)
    stdout = "influxdb is disabled in config"
    run = FakeRun(
        results={("influx", "status", "--json"): _completed(2, stdout, "")},
        default=_completed(0, "{}"),
    )
    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    result = service.run(check_ids=["influx_status"])
    assert result["checks"][0]["status"] == "disabled"


def test_non_influx_disabled_marker_is_not_remapped(tmp_path):
    # The remap is scoped to the InfluxDB check; other checks are untouched.
    _standard_install(tmp_path)
    run = FakeRun(
        results={("status",): _completed(2, "", "influxdb is disabled in config")},
        default=_completed(0, "{}"),
    )
    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    result = service.run(check_ids=["runtime_status"])
    assert result["checks"][0]["status"] == "failed"


def test_non_influx_failure_is_a_failure(tmp_path):
    _standard_install(tmp_path)
    run = FakeRun(
        results={("status",): _completed(2, "", "boom")},
        default=_completed(0, "{}"),
    )
    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    result = service.run(check_ids=["runtime_status"])
    check = result["checks"][0]

    assert check["status"] == "failed"
    assert result["summary"]["failed"] == 1
    assert result["summary"]["status"] == "failed"


def test_timeout_is_reported_as_timeout_and_not_retried(tmp_path):
    _standard_install(tmp_path)

    calls = {"count": 0}

    def run(argv, **kwargs):
        calls["count"] += 1
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    result = service.run(check_ids=["quick_diagnose"])
    check = result["checks"][0]

    assert check["status"] == "timeout"
    assert check["exit_code"] is None
    assert calls["count"] == 1


def test_stdout_and_stderr_are_truncated(tmp_path):
    _standard_install(tmp_path)
    big_out = "a" * (STDOUT_CAP + 500)
    big_err = "b" * (STDERR_CAP + 500)
    run = FakeRun(
        results={("status",): _completed(0, big_out, big_err)},
    )
    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    result = service.run(check_ids=["runtime_status"])
    check = result["checks"][0]

    assert len(check["stdout"]) == STDOUT_CAP
    assert len(check["stderr"]) == STDERR_CAP
    assert check["truncated"] is True


def test_missing_command_runner_degrades_to_unavailable_check(tmp_path):
    _standard_install(tmp_path)

    def run(argv, **kwargs):
        raise FileNotFoundError("docker not found")

    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    result = service.run(check_ids=["quick_diagnose"])
    check = result["checks"][0]

    assert check["status"] == "unavailable"
    # The route as a whole still returns; one bad check is not fatal.
    assert result["available"] is True


def test_all_checks_run_in_stable_order(tmp_path):
    _standard_install(tmp_path)
    run = FakeRun(default=_completed(0, "{}"))
    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    result = service.run()

    assert [check["id"] for check in result["checks"]] == list(CHECK_ORDER)


def test_duration_is_reported_per_check(tmp_path):
    _standard_install(tmp_path)
    run = FakeRun(default=_completed(0, "{}"))
    service = _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)

    result = service.run(check_ids=["runtime_status"])

    assert isinstance(result["checks"][0]["duration_ms"], int)
    assert result["checks"][0]["duration_ms"] >= 0


# --- a check is bounded where it runs -------------------------------------

_GUARD_MISSING = (
    'OCI runtime exec failed: exec failed: unable to start container process: '
    'exec: "timeout": executable file not found in $PATH: unknown'
)


def _container_service(tmp_path, run):
    _standard_install(tmp_path)
    return _service(tmp_path, docker=FakeDocker(container=_running_ems()), run=run)


def test_a_container_check_carries_its_deadline_inside(tmp_path):
    from admin.ems_cli import CHECKS
    from admin.ems_tool import EXEC_GUARD

    run = FakeRun(default=_completed(stdout="{}"))
    _container_service(tmp_path, run).run(check_ids=("runtime_status",))

    argv = run.calls[0]["argv"]
    assert EXEC_GUARD in argv
    assert str(CHECKS["runtime_status"]["timeout"]) in argv


def test_the_client_waits_past_a_container_checks_inner_deadline(tmp_path):
    from admin.ems_cli import CHECKS

    run = FakeRun(default=_completed(stdout="{}"))
    _container_service(tmp_path, run).run(check_ids=("runtime_status",))

    assert run.calls[0]["kwargs"]["timeout"] > CHECKS["runtime_status"]["timeout"]


def _elapsed_clock(monkeypatch, seconds):
    """Make every check appear to have run for ``seconds``."""

    from admin import ems_cli

    ticks = iter(range(0, 10_000, max(1, int(seconds))))
    monkeypatch.setattr(ems_cli.time, "monotonic", lambda: next(ticks))


def test_a_check_stopped_inside_the_container_reads_as_a_timeout(
    tmp_path, monkeypatch
):
    _elapsed_clock(monkeypatch, 600)
    run = FakeRun(default=_completed(returncode=124))
    result = _container_service(tmp_path, run).run(
        check_ids=("runtime_status", "influx_status")
    )

    by_id = {check["id"]: check for check in result["checks"]}
    assert by_id["runtime_status"]["status"] == "timeout"
    # `warn_on_fail` survives this path exactly as it does the client's own.
    assert by_id["influx_status"]["status"] == "warning"


def test_a_check_killed_after_refusing_to_stop_reads_as_a_timeout(
    tmp_path, monkeypatch
):
    _elapsed_clock(monkeypatch, 600)
    run = FakeRun(default=_completed(returncode=137))
    result = _container_service(tmp_path, run).run(check_ids=("runtime_status",))

    assert result["checks"][0]["status"] == "timeout"


def test_a_check_killed_long_before_its_deadline_is_not_a_timeout(tmp_path):
    """Anything may SIGKILL a check; only the guard means the ceiling was hit."""

    run = FakeRun(default=_completed(returncode=137, stderr="out of memory"))
    result = _container_service(tmp_path, run).run(check_ids=("runtime_status",))

    check = result["checks"][0]
    assert check["status"] != "timeout"
    assert check["exit_code"] == 137


def test_a_local_check_is_never_wrapped(tmp_path):
    """Nothing survives here: the process is this process's own child."""

    from admin.ems_tool import EXEC_GUARD

    _standard_install(tmp_path, with_compose=False, with_emsctl=True)
    run = FakeRun(default=_completed(stdout="{}"))
    service = _service(tmp_path, docker=FakeDocker(container=None), run=run)

    service.run(check_ids=("runtime_status",))

    assert EXEC_GUARD not in run.calls[0]["argv"]


def test_an_image_without_the_guard_still_answers_the_check(tmp_path):
    from admin.ems_tool import EXEC_GUARD

    class _GuardMissingOnce(FakeRun):
        def __call__(self, argv, **kwargs):
            self.calls.append({"argv": list(argv), "kwargs": kwargs})
            if EXEC_GUARD in argv:
                return _completed(returncode=126, stdout=_GUARD_MISSING)
            return _completed(returncode=0, stdout="ok")

    run = _GuardMissingOnce()
    result = _container_service(tmp_path, run).run(check_ids=("runtime_status",))

    assert result["checks"][0]["status"] == "ok"
    assert len(run.calls) == 2
    assert EXEC_GUARD not in run.calls[1]["argv"]
