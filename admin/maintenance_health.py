# SPDX-License-Identifier: AGPL-3.0-or-later
"""One ranked answer to "what is wrong" for the Maintenance status page.

This is a projection of the Maintenance overview, not a second authority. Every
finding is derived from the payload ``run_maintenance_overview`` already
assembled from Docker, the install-state detector and the filesystem; nothing
here probes anything itself, and dropping the block and recomputing it from the
rest of the payload must give the same result.

The severities are the EMS diagnostics vocabulary (``info``/``warning``/
``error``) rather than a second one, and a payload that cannot be read fails
closed as an error instead of reporting a healthy system.
"""

from datetime import datetime, timezone

EMS_RUNNING_IDENTITY_UNKNOWN_WARNING = (
    "EMS is running but its image identity could not be verified; the installed "
    "release is unknown. The Compose or last-known-good release is not shown as "
    "the running one."
)

PARTIAL_INSTALL_WARNING = (
    "This looks like a partial EMS installation. Maintenance can inspect it, "
    "but repair actions are not part of this read-only overview yet."
)

# Worst first. Mirrors the EMS diagnostics root-cause severities.
SEVERITIES = ("error", "warning", "info")

_HEALTHY_STATES = frozenset({"standard_install", "admin_prepared_install"})


def _finding(code, severity, title, message, next_step):
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "message": message,
        "next_step": next_step,
    }


def _parse_timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _install_findings(install, paths):
    state = install.get("state")
    if state in _HEALTHY_STATES:
        return []
    missing = [
        name
        for name in ("config", "compose", "data")
        if not _mapping(paths.get(name)).get("exists")
    ]
    detail = install.get("message") or "This installation is not complete."
    reasons = [str(reason) for reason in install.get("reasons") or []]
    if reasons:
        detail = detail + " " + " ".join(reasons)
    if missing:
        next_step = (
            "Missing here: " + ", ".join(missing) + ". Guided setup writes the "
            "standard layout; restoring a backup puts an earlier one back."
        )
    else:
        next_step = (
            "Guided setup writes the standard layout; restoring a backup puts an "
            "earlier one back."
        )
    return [
        _finding(
            "installation_incomplete",
            "error",
            install.get("label") or "This installation is not complete",
            detail,
            next_step,
        )
    ]


def _container_findings(install, containers):
    if install.get("state") not in _HEALTHY_STATES:
        # The installation finding already says why nothing is running.
        return []
    ems = _mapping(containers.get("ems"))
    if not ems.get("found"):
        return [
            _finding(
                "ems_missing",
                "error",
                "The EMS container does not exist",
                "A complete installation was found, but no EMS container has been "
                "created from it.",
                "Open Guided upgrade to deploy the System Build; it creates the "
                "container.",
            )
        ]
    if not ems.get("running"):
        status = ems.get("status") or "stopped"
        return [
            _finding(
                "ems_stopped",
                "error",
                "EMS is not running",
                "The EMS container exists but is " + str(status) + ", so nothing "
                "is reading your meter or writing to your inverters.",
                "Open EMS services below and start it, or run the diagnostics to "
                "see why it stopped.",
            )
        ]
    return []


def _restart_findings(paths, containers):
    ems = _mapping(containers.get("ems"))
    if not ems.get("running"):
        return []
    saved = _parse_timestamp(_mapping(paths.get("config")).get("modified_at"))
    started = _parse_timestamp(ems.get("started_at"))
    if saved is None or started is None or saved <= started:
        return []
    return [
        _finding(
            "settings_await_restart",
            "info",
            "Saved settings are newer than the running EMS",
            "Your settings file was written after EMS started, so some of what you "
            "saved is not in effect yet.",
            "Restart EMS from EMS services below to apply the saved settings.",
        )
    ]


# Warning strings the structured findings above already account for, so the same
# cause is never reported twice in different words.
_STRUCTURED_WARNINGS = {
    PARTIAL_INSTALL_WARNING: None,
    EMS_RUNNING_IDENTITY_UNKNOWN_WARNING: _finding(
        "ems_image_unverifiable",
        "warning",
        "The running EMS image cannot be identified",
        EMS_RUNNING_IDENTITY_UNKNOWN_WARNING,
        "Open Guided upgrade and deploy a System Build to get a known image back.",
    ),
}


def _warning_findings(warnings):
    findings = []
    for warning in warnings or []:
        text = str(warning)
        if text in _STRUCTURED_WARNINGS:
            mapped = _STRUCTURED_WARNINGS[text]
            if mapped is not None:
                findings.append(dict(mapped))
            continue
        findings.append(
            _finding(
                "installation_warning",
                "warning",
                "This installation needs a look",
                text,
                "Run the diagnostics below for the full picture.",
            )
        )
    return findings


def build_maintenance_health(overview):
    """Rank what the Maintenance overview proves about this installation."""

    if not isinstance(overview, dict) or not overview:
        return {
            "status": "error",
            "findings": [
                _finding(
                    "overview_unreadable",
                    "error",
                    "This installation could not be read",
                    "The Maintenance overview did not return a usable answer, so "
                    "nothing here can be trusted as healthy.",
                    "Refresh the page; if it stays this way, check that the Admin "
                    "Console can reach the install directory and Docker.",
                )
            ],
        }

    docker = _mapping(overview.get("docker"))
    install = _mapping(overview.get("install_state"))
    paths = _mapping(overview.get("paths"))
    containers = _mapping(overview.get("containers"))

    findings = []
    if not docker.get("available"):
        findings.append(
            _finding(
                "docker_unavailable",
                "error",
                "Docker is not reachable",
                "Container status could not be read, so this page cannot say "
                "whether EMS is running."
                + (" Docker reported: " + str(docker["error"]) if docker.get("error") else ""),
                "Check that the Docker daemon is running and that the Admin "
                "Console may reach its socket.",
            )
        )
    findings.extend(_install_findings(install, paths))
    findings.extend(_container_findings(install, containers))
    findings.extend(_warning_findings(overview.get("warnings")))
    findings.extend(_restart_findings(paths, containers))

    findings.sort(key=lambda finding: SEVERITIES.index(finding["severity"]))
    status = findings[0]["severity"] if findings else "ok"
    return {"status": status, "findings": findings}
