# SPDX-License-Identifier: AGPL-3.0-or-later
"""admin.js declares every top-level function once.

A classic script keeps the last of two declarations, while the node extraction
helpers in the frontend tests take the first; a duplicate name tests dead code.
"""

import collections
import os
import re

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.contract,
]

ADMIN_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static", "admin.js")


def test_admin_js_declares_every_function_once():
    with open(ADMIN_JS, encoding="utf-8") as handle:
        source = handle.read()
    names = re.findall(r"^(?:async )?function ([A-Za-z0-9_]+)\(", source, re.M)
    assert [name for name, count in collections.Counter(names).items() if count > 1] == []
