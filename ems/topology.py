# SPDX-License-Identifier: AGPL-3.0-or-later
"""Topology parsing and resolution for logical inverter branches."""

from __future__ import annotations

from dataclasses import dataclass


VALID_ROOT_MODES = ("parallel",)
VALID_LINK_MODES = ("single", "parallel")


class TopologyValidationError(ValueError):
    """Raised when an enabled topology config is invalid."""


@dataclass(frozen=True)
class TopologyLink:
    sources: tuple[str, ...]
    target: str
    mode: str


@dataclass(frozen=True)
class TopologyNode:
    device_id: str
    sources: tuple["TopologyNode", ...]
    source_mode: str | None = None


@dataclass(frozen=True)
class ResolvedTopology:
    enabled: bool
    root_mode: str
    root_devices: tuple[str, ...]
    links: tuple[TopologyLink, ...]
    root_nodes: tuple[TopologyNode, ...]
    branch_members: dict[str, tuple[str, ...]]
    warnings: tuple[str, ...] = ()


def normalize_topology_config(raw_config, defaults):
    """Return topology config with defaults without strict disabled validation."""

    if not isinstance(raw_config, dict):
        raw_config = {}

    return {
        **defaults,
        **raw_config,
    }


def resolve_topology(raw_config, device_ids, defaults=None):
    """Validate and resolve the flat topology config into branches.

    Branch traversal order is deterministic: target first, then each configured
    source in link order, recursively including nested sources before siblings.
    The synthetic ``root`` branch contains only the configured root devices.
    """

    defaults = defaults or {
        "enabled": False,
        "root_mode": "parallel",
        "root_devices": [],
        "links": [],
    }
    config = normalize_topology_config(raw_config, defaults)
    enabled = bool(config.get("enabled", False))

    if not enabled:
        return ResolvedTopology(
            enabled=False,
            root_mode=str(config.get("root_mode", defaults["root_mode"])),
            root_devices=(),
            links=(),
            root_nodes=(),
            branch_members={},
        )

    device_set = set(str(device_id) for device_id in device_ids)
    root_mode = _validate_root_mode(config.get("root_mode"))
    root_devices = _validate_device_list(
        config.get("root_devices"),
        "topology.root_devices",
        device_set,
        require_non_empty=True,
    )

    links = _validate_links(config.get("links"), device_set)
    root_set = set(root_devices)
    source_parent = {}

    for link in links:
        for source in link.sources:
            if source in source_parent:
                raise TopologyValidationError(
                    f"topology source appears in more than one link: {source}"
                )
            if source in root_set:
                raise TopologyValidationError(
                    f"topology source must not also be a root device: {source}"
                )
            source_parent[source] = link.target

    _validate_source_paths(source_parent, root_set)

    children_by_target = {}
    mode_by_target = {}
    for link in links:
        if link.target in children_by_target:
            raise TopologyValidationError(
                f"topology target may only appear once in links: {link.target}"
            )
        children_by_target[link.target] = link.sources
        mode_by_target[link.target] = link.mode

    root_nodes = tuple(
        _build_node(root_device, children_by_target, mode_by_target, ())
        for root_device in root_devices
    )
    branch_members = {
        node.device_id: _collect_members(node)
        for node in _walk_nodes(root_nodes)
        if node.sources
    }
    branch_members["root"] = root_devices

    return ResolvedTopology(
        enabled=True,
        root_mode=root_mode,
        root_devices=root_devices,
        links=links,
        root_nodes=root_nodes,
        branch_members=branch_members,
    )


def topology_to_dict(topology):
    return {
        "enabled": topology.enabled,
        "root_mode": topology.root_mode,
        "root_devices": list(topology.root_devices),
        "links": [
            {
                "sources": list(link.sources),
                "target": link.target,
                "mode": link.mode,
            }
            for link in topology.links
        ],
        "resolved_tree": [
            topology_node_to_dict(node)
            for node in topology.root_nodes
        ],
        "branch_members": {
            branch: list(members)
            for branch, members in topology.branch_members.items()
        },
        "warnings": list(topology.warnings),
    }


def topology_node_to_dict(node):
    return {
        "device_id": node.device_id,
        "source_mode": node.source_mode,
        "sources": [
            topology_node_to_dict(source)
            for source in node.sources
        ],
    }


def topology_tree_lines(topology):
    lines = []
    for node in topology.root_nodes:
        lines.append(node.device_id)
        for index, child in enumerate(node.sources):
            lines.extend(_node_tree_lines(child, "", index == len(node.sources) - 1))
    return lines


def topology_text(topology):
    if not topology.enabled:
        return "Topology: disabled\n"

    lines = [
        "Topology: enabled",
        f"Root mode: {topology.root_mode}",
        "",
        "Root devices:",
    ]
    lines.extend(f"- {device_id}" for device_id in topology.root_devices)
    lines.extend(["", "Links:"])
    if topology.links:
        lines.extend(
            f"- {', '.join(link.sources)} -> {link.target} [{link.mode}]"
            for link in topology.links
        )
    else:
        lines.append("- none")

    lines.extend(["", "Resolved tree:"])
    lines.extend(topology_tree_lines(topology) or ["- none"])
    lines.extend(["", "Branches:"])
    lines.extend(
        f"- {branch}: {', '.join(members)}"
        for branch, members in topology.branch_members.items()
    )
    return "\n".join(lines) + "\n"


def _validate_root_mode(value):
    if value not in VALID_ROOT_MODES:
        raise TopologyValidationError(
            "topology.root_mode must currently be 'parallel'"
        )
    return value


def _validate_device_list(value, path, device_set, require_non_empty=False):
    if not isinstance(value, list):
        raise TopologyValidationError(f"{path} must be a list")
    if require_non_empty and not value:
        raise TopologyValidationError(f"{path} must not be empty")

    normalized = []
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise TopologyValidationError(f"{path}.{index} must be a non-empty string")
        device_id = item.strip()
        if device_id in seen:
            raise TopologyValidationError(f"{path} contains duplicate device id: {device_id}")
        if device_id not in device_set:
            raise TopologyValidationError(f"{path}.{index} references unknown device: {device_id}")
        normalized.append(device_id)
        seen.add(device_id)

    return tuple(normalized)


def _validate_links(value, device_set):
    if not isinstance(value, list):
        raise TopologyValidationError("topology.links must be a list")

    links = []
    for index, item in enumerate(value):
        path = f"topology.links.{index}"
        if not isinstance(item, dict):
            raise TopologyValidationError(f"{path} must be an object")
        sources = _validate_device_list(
            item.get("sources"),
            f"{path}.sources",
            device_set,
            require_non_empty=True,
        )
        target = item.get("target")
        if not isinstance(target, str) or not target.strip():
            raise TopologyValidationError(f"{path}.target must be a non-empty string")
        target = target.strip()
        if target not in device_set:
            raise TopologyValidationError(f"{path}.target references unknown device: {target}")
        if target in sources:
            raise TopologyValidationError(f"{path} must not link a device to itself: {target}")
        mode = item.get("mode")
        if mode not in VALID_LINK_MODES:
            raise TopologyValidationError(f"{path}.mode must be 'single' or 'parallel'")
        if mode == "single" and len(sources) != 1:
            raise TopologyValidationError(f"{path}.mode single must have exactly one source")
        if mode == "parallel" and not sources:
            raise TopologyValidationError(f"{path}.mode parallel must have at least one source")
        links.append(TopologyLink(sources=sources, target=target, mode=mode))
    return tuple(links)


def _validate_source_paths(source_parent, root_set):
    for source in source_parent:
        current = source
        path = []
        while current not in root_set:
            if current in path:
                cycle = " -> ".join((*path, current))
                raise TopologyValidationError(f"topology cycle detected: {cycle}")
            path.append(current)
            parent = source_parent.get(current)
            if parent is None:
                raise TopologyValidationError(
                    f"topology source does not resolve into a root device: {source}"
                )
            current = parent


def _build_node(device_id, children_by_target, mode_by_target, stack):
    if device_id in stack:
        cycle = " -> ".join((*stack, device_id))
        raise TopologyValidationError(f"topology cycle detected: {cycle}")

    children = children_by_target.get(device_id, ())
    return TopologyNode(
        device_id=device_id,
        source_mode=mode_by_target.get(device_id) if children else None,
        sources=tuple(
            _build_node(child, children_by_target, mode_by_target, (*stack, device_id))
            for child in children
        ),
    )


def _collect_members(node):
    members = [node.device_id]
    for child in node.sources:
        members.extend(_collect_members(child))
    return tuple(members)


def _walk_nodes(nodes):
    for node in nodes:
        yield node
        yield from _walk_nodes(node.sources)


def _node_tree_lines(node, prefix, is_last):
    connector = "└─ " if is_last else "├─ "
    current = f"{prefix}{connector}{node.device_id}"
    lines = [current]
    child_prefix = prefix + ("   " if is_last else "│  ")
    for index, child in enumerate(node.sources):
        lines.extend(_node_tree_lines(
            child,
            child_prefix,
            index == len(node.sources) - 1,
        ))
    return lines
