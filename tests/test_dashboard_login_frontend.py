# SPDX-License-Identifier: AGPL-3.0-or-later
"""The login dialog behaves as a dialog: it is announced as one, takes the
focus, keeps it while open, closes on Escape and gives the focus back."""

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
INDEX_HTML = ROOT / "dashboard" / "static" / "index.html"


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


FAKE_DOM = """
function element(id, extra = {}) {
  return {
    id,
    hidden: false,
    value: "",
    textContent: "",
    className: "",
    isConnected: true,
    focus() { focused.push(id); global.document.activeElement = this; },
    ...extra,
  };
}
const focused = [];
const nodes = {
  authButton: element("authButton"),
  loginModal: element("loginModal", { hidden: true }),
  loginPassword: element("loginPassword"),
  loginError: element("loginError", { hidden: true }),
  loginCloseButton: element("loginCloseButton"),
  loginSubmitButton: element("loginSubmitButton"),
  writeModeState: element("writeModeState"),
};
nodes.loginForm = element("loginForm", {
  contains: (node) => [
    nodes.loginCloseButton, nodes.loginPassword, nodes.loginSubmitButton,
  ].includes(node),
  querySelectorAll: () => [
    nodes.loginCloseButton, nodes.loginPassword, nodes.loginSubmitButton,
  ],
});
global.document = {
  hidden: false,
  activeElement: null,
  getElementById: (id) => nodes[id] || null,
  querySelectorAll: () => [],
};
"""


def test_the_login_form_is_announced_as_a_modal_dialog_with_its_title():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert (
        '<form id="loginForm" class="login-modal" role="dialog" aria-modal="true" '
        'aria-labelledby="loginTitle">'
    ) in html
    assert '<h2 id="loginTitle">Dashboard Login</h2>' in html


def test_the_dialog_takes_focus_keeps_it_and_gives_it_back():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{FAKE_DOM}
nodes.authButton.focus();
app.openLoginModal();
const opened = {{ hidden: nodes.loginModal.hidden, focus: document.activeElement.id }};

let prevented = false;
document.activeElement = nodes.loginSubmitButton;
app.handleLoginModalKeydown({{ key: "Tab", shiftKey: false, preventDefault: () => {{ prevented = true; }} }});
const wrapped = {{ focus: document.activeElement.id, prevented }};

document.activeElement = nodes.loginCloseButton;
app.handleLoginModalKeydown({{ key: "Tab", shiftKey: true, preventDefault: () => {{}} }});
const wrappedBack = document.activeElement.id;

app.handleLoginModalKeydown({{ key: "Escape", preventDefault: () => {{}} }});
const closed = {{ hidden: nodes.loginModal.hidden, focus: document.activeElement.id }};
console.log(JSON.stringify({{ opened, wrapped, wrappedBack, closed }}));
"""
    out = run_node(script)

    assert out["opened"] == {"hidden": False, "focus": "loginPassword"}
    assert out["wrapped"] == {"focus": "loginCloseButton", "prevented": True}
    assert out["wrappedBack"] == "loginSubmitButton"
    assert out["closed"] == {"hidden": True, "focus": "authButton"}


def test_keys_do_nothing_while_the_dialog_is_closed():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{FAKE_DOM}
let prevented = false;
app.handleLoginModalKeydown({{ key: "Tab", preventDefault: () => {{ prevented = true; }} }});
console.log(JSON.stringify({{ prevented, hidden: nodes.loginModal.hidden }}));
"""
    out = run_node(script)

    assert out == {"prevented": False, "hidden": True}
