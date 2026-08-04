# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
]


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "dashboard" / "static" / "app.js"


def run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for executable dashboard rendering test")
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_should_send_heartbeat_throttles_to_one_per_minute():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
console.log(JSON.stringify({{
  immediate: app.shouldSendHeartbeat(1000, 1000),
  before: app.shouldSendHeartbeat(60000 + 999, 1000),
  after: app.shouldSendHeartbeat(61001, 1000),
}}));
"""
    output = run_node(script)
    assert output["immediate"] is False
    assert output["before"] is False  # < 60s since last
    assert output["after"] is True    # >= 60s


def test_heartbeat_skips_when_unauthenticated_and_posts_with_csrf():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const calls = [];
global.fetch = async (url, opts) => {{ calls.push({{ url, opts }}); return {{ status: 200 }}; }};

(async () => {{
  app.state.demoMode = false;
  // Unauthenticated: no request.
  app.state.auth = {{ configured: true, authenticated: false, csrfToken: null }};
  await app.sendSessionHeartbeat();
  const afterUnauth = calls.length;

  // Authenticated: posts refresh with CSRF header.
  app.state.auth = {{ configured: true, authenticated: true, csrfToken: "tok-123" }};
  await app.sendSessionHeartbeat();
  const firstCall = calls[calls.length - 1];

  // Immediate second call is throttled (no extra request).
  await app.sendSessionHeartbeat();
  const afterThrottle = calls.length;

  console.log(JSON.stringify({{
    afterUnauth,
    url: firstCall ? firstCall.url : null,
    method: firstCall ? firstCall.opts.method : null,
    csrf: firstCall ? firstCall.opts.headers["X-CSRF-Token"] : null,
    totalCalls: afterThrottle,
  }}));
}})();
"""
    output = run_node(script)
    assert output["afterUnauth"] == 0          # no request while unauthenticated
    assert output["url"] == "/api/auth/refresh"
    assert output["method"] == "POST"
    assert output["csrf"] == "tok-123"
    assert output["totalCalls"] == 1           # second call throttled
