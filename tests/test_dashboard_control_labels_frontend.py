# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Control view names what the EMS decided instead of printing its codes.

The control explanation reaches the browser as codes: the strategy, why a device
was limited or not written, the firmware signals behind its capability, and
the limits the target passed through. The dashboard keeps one list of the codes
it has words for (``CONTROL_CODES`` and ``DECISION_SENTENCES`` in
``frontend/dashboard-modern/src/lib/control.js``), and the codes live in the
EMS, so the two are a paired registry: a code without words reaches the
operator as ``pv_first`` or ``runtime role ac input``, and words kept for a
code the EMS does not send never show at all.

The EMS side of that pairing is read by walking the syntax tree of ems/. The
walk covers the forms ems/ uses to put a code into an explanation, and fails on
the binding forms it knows it cannot follow unless ``UNREADABLE`` names them. It
does not prove completeness against every Python construct: a code passed in a
form the walk does not know can still go unseen.
"""

import ast
import dataclasses
import functools
from pathlib import Path

import pytest

from ems.runtime_intents import DeviceRuntimeRole
from ems.target_control import detect_capabilities
from ems.winter_reserve import SOLAR_CHARGE_REASON
from tests.modern_frontend import js_export
from tests.test_write_gates import state

pytestmark = [
    pytest.mark.contract,
]


ROOT = Path(__file__).resolve().parents[1]
CONTROL_JS = ROOT / "frontend" / "dashboard-modern" / "src" / "lib" / "control.js"
EMS = ROOT / "ems"
REASON_FIELDS = ("decision_reason", "limiting_reason", "write_reason", "capability_reason")

CALL_SITES = {
    "ControlExplanation": [(0, "mode", "mode")],
    "DeviceCapabilities": [(4, "reason", "capability")],
    "replace": [(None, "reason", "capability"), (None, "mode", "mode")],
    "add_limit": [(0, "name", "limit")],
    "ControlLimitExplanation": [(0, "name", "limit")],
    "_set_limiting_reason": [(1, "reason", "limiting_reason")],
}

NOT_THE_EXPLANATION = {
    "log_event": "its keywords are a log record's fields, not the control explanation",
}

UNREADABLE = {
    ("ems/controller.py", "runtime_role_block_reason", "f'runtime_role_{intent.role.value}'"):
        "one code per DeviceRuntimeRole member, which the walk adds from the enum",
    ("ems/controller.py", "run_once", "capabilities[i].reason"):
        "a capability's reason, which DeviceCapabilities, frozen, takes where one is made or "
        "replaced; every DeviceCapabilities(...) and replace(..., reason=...) call in ems/ is walked",
    ("ems/target_control.py", "calculate_targets", "cap.reason"):
        "a capability's reason, which DeviceCapabilities, frozen, takes where one is made or "
        "replaced; every DeviceCapabilities(...) and replace(..., reason=...) call in ems/ is walked",
    ("ems/target_control.py", "detect_capabilities", "','.join(reasons)"):
        "the signals detect_capabilities puts into reasons; every use of the name reasons in it, "
        "or in a function nested in it, is walked, and one that is not a method call, a plain "
        "assignment, the join or a truth test in its own scope is reported",
    ("ems/target_control.py", "_set_limiting_reason", "reason"):
        "the reason a caller passes, which the walk reads at every call",
    ("ems/target_control.py", "add_limit", "name"):
        "the limit a caller names, which the walk reads at every call",
}

REPLACED_BEFORE_SENT = {
    ("ems/target_control.py", "decision_reason", "mode"):
        "the strategy, which calculate_targets replaces with the allocation's own "
        "decision for every device before it returns; "
        "test_the_strategy_never_reaches_a_device_as_its_decision runs both branches",
}


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _argument(call, position, keyword):
    """The value ``call`` passes for one parameter, by position or by keyword."""
    if position is not None and len(call.args) > position:
        return call.args[position]
    return next((item.value for item in call.keywords if item.arg == keyword), None)


def _parameters(function):
    return {argument.arg for argument in ast.walk(function.args) if isinstance(argument, ast.arg)}


class _Module:
    """One ems/ module, indexed so a value can be followed to the strings it holds.

    ``assigned`` holds the values of every plain ``name = value`` in a scope.
    ``opaque`` holds every other way the scope binds a name -- a tuple target,
    a loop or ``with`` target, ``:=``, ``+=``, ``except ... as``, ``global`` --
    so a name bound that way is reported rather than read as its plain
    assignments alone.
    """

    def __init__(self, path):
        self.rel = path.relative_to(ROOT).as_posix()
        self.package = ".".join(path.relative_to(ROOT).with_suffix("").parts[:-1])
        self.tree = ast.parse(path.read_text(encoding="utf-8"))
        self.scope_of = {}
        self.parent = {}
        self.outer = {}
        self.functions = {}
        self.assigned = {}
        self.opaque = set()
        self.imports = {}
        self._index(self.tree, None, in_class=False)

    def _index(self, node, scope, *, in_class):
        for child in ast.iter_child_nodes(node):
            self.scope_of[child] = scope
            self.parent[child] = node
            inner, inner_class = scope, in_class
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.setdefault(child.name, []).append(child)
                self.outer[child] = scope
                inner, inner_class = child, False
            elif isinstance(child, ast.ClassDef):
                inner_class = True
            elif isinstance(child, ast.ImportFrom):
                base = child.module or ""
                if child.level:
                    parts = self.package.split(".")
                    parts = parts[: len(parts) - child.level + 1]
                    base = ".".join(part for part in [*parts, base] if part)
                for alias in child.names:
                    self.imports[alias.asname or alias.name] = (base, alias.name)
            elif not in_class:
                self._bind(node, child, scope)
            self._index(child, inner, in_class=inner_class)

    def _bind(self, parent, child, scope):
        if isinstance(child, (ast.Assign, ast.AnnAssign)) and child.value:
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    self.assigned.setdefault((scope, target.id), []).append(child.value)
        elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            if not isinstance(parent, (ast.Assign, ast.AnnAssign)):
                self.opaque.add((scope, child.id))
        elif isinstance(child, ast.ExceptHandler) and child.name:
            self.opaque.add((scope, child.name))
        elif isinstance(child, ast.Global):
            self.opaque.update((None, name) for name in child.names)
        elif isinstance(child, ast.Nonlocal):
            self.opaque.update((self.outer[scope], name) for name in child.names)


class _ControlVocabulary:
    """Follows every value the EMS puts into a control explanation to its codes."""

    def __init__(self):
        self.modules = {
            ".".join(path.relative_to(ROOT).with_suffix("").parts): _Module(path)
            for path in sorted(EMS.rglob("*.py"))
        }
        self.unreadable = set()
        self.replaced_seen = set()

    def module(self, dotted):
        return self.modules.get(dotted) or self.modules.get(f"{dotted}.__init__")

    def strings(self, node, module, scope, seen=frozenset()):
        if isinstance(node, ast.Constant):
            return {node.value} if isinstance(node.value, str) else set()
        if isinstance(node, ast.IfExp):
            return self.strings(node.body, module, scope, seen) | self.strings(
                node.orelse, module, scope, seen
            )
        if isinstance(node, ast.BoolOp):
            return set().union(*(self.strings(value, module, scope, seen) for value in node.values))
        if isinstance(node, ast.Name):
            return self._name_strings(node, module, scope, seen)
        if isinstance(node, ast.Call):
            called = self._called_function(node, module)
            if called is not None:
                return set().union(*(
                    self.strings(returned.value, module, called, seen)
                    for returned in ast.walk(called)
                    if isinstance(returned, ast.Return)
                    and returned.value is not None
                    and module.scope_of.get(returned) is called
                ))
        return self._unreadable(node, module, scope)

    def _name_strings(self, node, module, scope, seen):
        key = (module.rel, id(scope), node.id)
        if key in seen:
            return set()
        seen = seen | {key}
        current = scope
        while True:
            values = module.assigned.get((current, node.id), [])
            found = set().union(*(self.strings(value, module, current, seen) for value in values))
            bound_otherwise = (current, node.id) in module.opaque or (
                current is not None and node.id in _parameters(current)
            )
            if bound_otherwise:
                self._unreadable(node, module, current)
            if values or bound_otherwise:
                return found
            if current is None:
                break
            current = module.outer[current]
        source = module.imports.get(node.id)
        imported = self.module(source[0]) if source else None
        if imported is not None:
            return self._name_strings(ast.Name(id=source[1]), imported, None, seen)
        return self._unreadable(node, module, scope)

    def _called_function(self, call, module):
        func = call.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute) and _name(func.value) == "self":
            name = func.attr
        else:
            return None
        matches = module.functions.get(name, [])
        return matches[0] if len(matches) == 1 else None

    def _unreadable(self, node, module, scope):
        self.unreadable.add((module.rel, scope.name if scope else "<module>", ast.unparse(node)))
        return set()

    def report(self, node, module):
        """Report a use the walk cannot follow."""
        self._unreadable(node, module, module.scope_of.get(node))

    def site(self, field, node, module):
        """The codes one place in the EMS sends, unless it is known not to reach the browser."""
        replaced = (module.rel, field, ast.unparse(node))
        if replaced in REPLACED_BEFORE_SENT:
            self.replaced_seen.add(replaced)
            return set()
        return self.strings(node, module, module.scope_of.get(node))


def _assigned_value(target, module):
    """The value a plain ``target = value`` assigns.

    For any other binding -- a tuple target, ``+=``, a loop target -- the
    target itself, which the walk cannot read and so reports.
    """
    parent = module.parent[target]
    if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is not None:
        return parent.value
    return target


def _items(node):
    """The items of a literal list, tuple or set, or ``node`` itself."""
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return list(node.elts)
    return [node]


def _within(scope, name, module):
    """Whether ``scope`` is the function ``name`` or a function nested in it."""
    while scope is not None:
        if scope.name == name:
            return True
        scope = module.outer[scope]
    return False


def _signal_values(name, module):
    """What one use of ``reasons`` in ``detect_capabilities`` puts into the list.

    A method call on it puts its arguments, and a plain ``reasons = [...]`` the
    list's items. The join and a truth test put nothing. Any other use -- ``+=``,
    an item assignment, a second name for the list, passing it on, a use in a
    function nested in ``detect_capabilities`` -- is None, and the walk
    reports it.
    """
    if getattr(module.scope_of.get(name), "name", None) != "detect_capabilities":
        return None
    parent = module.parent[name]
    caller = module.parent.get(parent)
    if isinstance(parent, ast.Attribute) and isinstance(caller, ast.Call) and caller.func is parent:
        return [item for argument in caller.args for item in _items(argument)]
    if isinstance(parent, ast.Assign) and parent.targets == [name]:
        return _items(parent.value)
    if isinstance(parent, ast.Call) and _name(parent.func) == "join" and parent.args == [name]:
        return []
    if isinstance(parent, (ast.IfExp, ast.If, ast.While)) and parent.test is name:
        return []
    return None


@functools.cache
def emitted_control_codes():
    """Every code the EMS writes into a control explanation, by where it shows.

    ``decision`` reasons become the sentence above a device card; everything
    else is shown as a short value: the strategy, a limiting or write reason, a
    capability signal and a limit name. A code is a reason field's value, or
    an argument ``CALL_SITES`` names for a call, passed by position or by
    keyword, a ``.mode`` assigned on any object, and what ``detect_capabilities``
    puts into its ``reasons``. Every ems/ module is walked, and a value is
    followed through local variables, module constants, imports and the
    functions that return it; a value in a form the walk knows it cannot follow
    lands in ``unreadable``, and so does a name or field bound other than by a
    plain ``target = value``.
    """
    vocabulary = _ControlVocabulary()
    decisions, modes, short = set(), set(), set()

    def record(field, value, module):
        found = {"decision_reason": decisions, "mode": modes}.get(field, short)
        found.update(vocabulary.site(field, value, module))

    for module in vocabulary.modules.values():
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call):
                callee = _name(node.func)
                if callee in NOT_THE_EXPLANATION:
                    continue
                for keyword in node.keywords:
                    if keyword.arg in REASON_FIELDS:
                        record(keyword.arg, keyword.value, module)
                for position, keyword, field in CALL_SITES.get(callee, ()):
                    value = _argument(node, position, keyword)
                    if value is not None:
                        record(field, value, module)
            elif (
                isinstance(node, ast.Name)
                and node.id == "reasons"
                and _within(module.scope_of.get(node), "detect_capabilities", module)
            ):
                values = _signal_values(node, module)
                if values is None:
                    vocabulary.report(module.parent[node], module)
                for value in values or ():
                    record("capability", value, module)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                if node.attr in (*REASON_FIELDS, "mode"):
                    record(node.attr, _assigned_value(node, module), module)

    short |= modes | {f"runtime_role_{role.value}" for role in DeviceRuntimeRole}
    return {
        "decision": decisions,
        "short": short,
        "modes": modes,
        "unreadable": vocabulary.unreadable,
        "replaced": vocabulary.replaced_seen,
    }


def frontend_control_labels():
    """The codes the dashboard has words for, read from its control module."""
    short = js_export(CONTROL_JS, "CONTROL_CODES")
    decision = js_export(CONTROL_JS, "DECISION_SENTENCES")
    if short is None or decision is None:
        pytest.skip("node is required to read the dashboard's control vocabulary")
    return {"short": set(short), "decision": set(decision)}


def test_the_vocabulary_walk_finds_the_codes_it_is_meant_to_find():
    """An extraction that silently found nothing would pass the pairing below."""
    codes = emitted_control_codes()
    assert {"pv_first", "battery_discharge", "ac_charge", "night_min_soc_idle"} <= codes["modes"]
    assert {
        "pv_first_allocation",
        "solar_plus_battery_discharge",
        "night_min_soc_idle",
        "ac_charge_allocation",
    } <= codes["decision"]
    assert not {"pv_first", "battery_discharge"} & codes["decision"]
    assert {
        "runtime_role_ac_input",
        "runtime_role_blocked",
        "deadband",
        "offline",
        "control_disabled",
        "not_controllable",
        "cannot_export",
        "pv_evidence",
        "normal",
        "ac_discharge_disabled",
        "max_total_power",
        SOLAR_CHARGE_REASON,
    } <= codes["short"]


def test_every_value_the_walk_cannot_follow_is_a_named_exception():
    codes = emitted_control_codes()
    assert sorted(codes["unreadable"] - UNREADABLE.keys()) == []
    assert sorted(UNREADABLE.keys() - codes["unreadable"]) == []
    assert codes["replaced"] == REPLACED_BEFORE_SENT.keys()


def test_a_capability_keeps_the_reason_it_was_made_with():
    """What ``UNREADABLE`` says of ``capabilities[i].reason`` and ``cap.reason`` rests on this."""
    capability = detect_capabilities(state(solar=500))
    with pytest.raises(dataclasses.FrozenInstanceError):
        capability.reason = "assist_export_hold"


def test_the_strategy_never_reaches_a_device_as_its_decision():
    from ems.target_control import calculate_targets, detect_capabilities
    from tests.test_write_gates import device, state

    strategies = set()
    for requested, states in (
        (300, [state(solar=500), state(solar=400)]),
        (1200, [state(solar=100), state(solar=0)]),
    ):
        _targets, _current, _new, explanation = calculate_targets(
            0,
            states,
            1600,
            device_configs=[device("WR1"), device("WR2")],
            capabilities=[detect_capabilities(report) for report in states],
            requested_total=requested,
            explain=True,
        )
        explained = explanation.to_dict()
        decisions = {entry["decision_reason"] for entry in explained["devices"].values()}
        strategies.add(explained["mode"])
        assert explained["mode"] not in decisions, decisions

    assert strategies == {"pv_first", "battery_discharge"}


def test_every_code_the_control_view_shows_has_a_label():
    codes = emitted_control_codes()
    labels = frontend_control_labels()
    assert sorted(codes["short"] - labels["short"]) == []
    assert sorted(codes["decision"] - labels["decision"]) == []


def test_no_label_waits_for_a_code_the_ems_never_sends():
    codes = emitted_control_codes()
    labels = frontend_control_labels()
    assert sorted(labels["short"] - codes["short"]) == []
    assert sorted(labels["decision"] - codes["decision"]) == []


def _may_hold_an_output_role(call):
    """Whether ``call`` can build an ac_output intent that is not allowed output control.

    A ``replace(...)`` that sets neither field keeps what its source intent had.
    """
    callee = _name(call.func)
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in call.keywords}
    if callee == "replace" and not {"role", "output_control_allowed", None} & keywords.keys():
        return False
    return (
        callee in ("DeviceRuntimeIntent", "replace")
        and keywords.get("role") != "DeviceRuntimeRole.AC_INPUT"
        and keywords.get("output_control_allowed") != "True"
    )


def test_only_the_full_charge_assist_holds_a_device_in_its_output_role():
    """The label for runtime_role_ac_output names the assist; no other claim may send it."""
    holders = set()
    for path in sorted(EMS.rglob("*.py")):
        module = _Module(path)
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call) and _may_hold_an_output_role(node):
                holders.add((module.rel, getattr(module.scope_of.get(node), "name", None)))
    assert holders == {("ems/controller.py", "full_charge_assist_intent")}
