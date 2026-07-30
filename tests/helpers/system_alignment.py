# SPDX-License-Identifier: AGPL-3.0-or-later
"""Explicit paired-build preconditions for route-focused test harnesses."""


class SetupReadySystemAlignment:
    """Model a Fresh Setup whose embedded resources were already verified.

    Route tests that focus on config serialization, credentials, or deployment
    start below the pairing workflow. They must opt into this precondition so
    production's no-transition hard gate remains exercised by dedicated tests.
    ``is_transition_pending`` is false because these harnesses also compare
    unrelated Maintenance behavior and do not model a real unresolved system.
    """

    @staticmethod
    def _build(tag="v0.6.0"):
        return {
            "requested_tag": tag,
            "canonical_tag": tag,
            "channel": "stable",
            "revision": "f7265fc747c2223f126f0ee7801e030c6226edf4",
            "build_id": f"{tag}-f7265fc",
            "release_tag": tag,
            "admin_image": f"ghcr.io/basecubedev/ems-solarflow-admin:{tag}",
            "admin_digest": "sha256:admin",
            "ems_image": f"ghcr.io/basecubedev/ems-solarflow-api-control:{tag}",
            "ems_digest": "sha256:ems",
        }

    def status(self, *, operation_active=None):
        del operation_active
        return {
            "ok": True,
            "active": True,
            "transition": {
                "operation_id": "route-test-op",
                "mode": "fresh_install",
                "stage": "resources_verified",
            },
            "known_good": None,
        }

    @staticmethod
    def resources_verified():
        return True

    @staticmethod
    def is_transition_pending():
        return False

    def start(
        self, *, requested_tag, mode, development_risk_acknowledged=False
    ):
        del mode
        return {
            "ok": True,
            "status": "ready_for_ems",
            "stage": "resources_verified",
            "operation_id": "route-test-op",
            "system_build": self._build(requested_tag),
            "reconnect": False,
        }

    def prepare_setup_resources(
        self, *, requested_tag, mode, development_risk_acknowledged=False
    ):
        del mode
        return {
            "ok": True,
            "status": "ready_for_ems",
            "stage": "resources_verified",
            "operation_id": "route-test-op",
            "resources_verified": True,
            "next_allowed": True,
            "system_build": self._build(requested_tag),
            "reconnect": False,
        }

    @staticmethod
    def begin_ems_operation(*, operation_id):
        return {"operation_id": operation_id, "stage": "ems_operation_pending"}

    @staticmethod
    def claim_ems_operation(*, operation_id):
        del operation_id
        return True

    @staticmethod
    def finish_ems_operation(*, operation_id, succeeded, **_kwargs):
        del succeeded
        return {"operation_id": operation_id, "stage": "healthcheck_pending"}

    @staticmethod
    def finish_healthcheck(*, operation_id, passed, **_kwargs):
        del passed
        return {"operation_id": operation_id, "stage": "completed"}

    @staticmethod
    def cancel(*, operation_id, coordinator=None):
        del coordinator
        return {"ok": True, "operation_id": operation_id, "stage": "cancelled"}
