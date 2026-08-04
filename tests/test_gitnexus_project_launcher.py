import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
]


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_mcp_process_does_not_hold_analyzer_writer_lock(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "gitnexus-project"
    shutil.copy2(REPO_ROOT / "scripts" / "gitnexus-project", launcher)
    (repo / ".gitnexusrc").write_text(
        '{"analyze": {"maxFileSize": "2048"}}\n', encoding="utf-8"
    )
    subprocess.run(
        ["git", "init", "--quiet", str(repo)],
        check=True,
        text=True,
        capture_output=True,
    )

    started = tmp_path / "mcp-started"
    fake_gitnexus = tmp_path / "gitnexus"
    fake_gitnexus.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = mcp ]; then\n"
        "  : >\"$FAKE_GITNEXUS_STARTED\"\n"
        "  exec sleep 30\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_gitnexus.chmod(0o755)
    env = {
        **os.environ,
        "FAKE_GITNEXUS_STARTED": str(started),
        "GITNEXUS_ANALYZE_LOCK_TIMEOUT": "0",
        "GITNEXUS_DB_WAIT_TIMEOUT": "0",
        "GITNEXUS_EXECUTABLE": str(fake_gitnexus),
    }

    reader = subprocess.Popen(
        [str(launcher), "mcp"],
        cwd=repo,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        reader_error = reader.stderr.read() if reader.poll() is not None else ""
        assert started.exists(), reader_error

        analyzer = subprocess.run(
            [str(launcher), "analyze", "--force", "--index-only"],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            timeout=5,
        )
    finally:
        reader.terminate()
        reader.wait(timeout=5)

    assert analyzer.returncode == 0, analyzer.stderr
