# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stage the runtime MQTT credentials a config's broker profiles reference.

Setup and Maintenance apply share this orchestration so a config can never
become active referencing a runtime credential record the EMS cannot use:
every referenced record must resolve through the Core resolver to its
complete per-source contract before ``config.json`` is written. Zendure cloud
MQTT credentials are provisioned through the cloud discovery service and
local discovery credentials are promoted into runtime storage. Failures raise
``CredentialStoreError`` with an operator-safe message and leave no newly
created record behind.
"""

from admin.credential_store import (
    CredentialProvisioningError,
    CredentialStoreError,
    MqttCredentialSourceConflictError,
    MqttCredentialsRefInvalidError,
)
from ems.mqtt_credentials import (
    MqttCredentialError,
    collect_mqtt_credential_consumers,
    validate_mqtt_credentials_ref,
)
from ems.zendure_mqtt.config_entries import (
    SOURCE_LOCAL_MQTT,
    SOURCE_ZENDURE_CLOUD_MQTT,
    SUPPORTED_BROKER_SOURCES,
)


def validate_config_credential_references(config):
    """Reject configured credentials_ref values that cannot stage safely.

    Derives every MQTT credential consumer from the complete config — Zendure
    broker profiles and MQTT grid meters alike — through the one shared Core
    extractor, then runs before any credential snapshot, save, cloud round-trip
    or config write to enforce two integrity rules a later staging step could
    not undo cleanly:

    * every referenced reference must already be canonical, so Admin never
      normalizes a configured reference to a different identifier than the
      Core resolver looks up at runtime;
    * a reference resolves to a single credential file, so it must belong to
      exactly one credential source — a ref shared by a local and a cloud
      broker would have local staging write a local record only for cloud
      staging to overwrite it, leaving one broker unusable.
    """

    sources_by_ref = {}
    components_by_ref = {}
    for consumer in collect_mqtt_credential_consumers(config):
        try:
            validate_mqtt_credentials_ref(consumer.credentials_ref)
        except MqttCredentialError as exc:
            raise MqttCredentialsRefInvalidError(
                credentials_ref=consumer.credentials_ref
            ) from exc
        if consumer.source in SUPPORTED_BROKER_SOURCES:
            sources_by_ref.setdefault(consumer.credentials_ref, set()).add(
                consumer.source
            )
            components_by_ref.setdefault(consumer.credentials_ref, set()).add(
                consumer.component
            )
    for ref, sources in sources_by_ref.items():
        if len(sources) > 1:
            raise MqttCredentialSourceConflictError(
                credentials_ref=ref,
                sources=sorted(sources),
                consumers=sorted(components_by_ref.get(ref, ())),
            )


def runtime_credential_requirements(config):
    """Runtime credential refs the config needs, keyed by credential source.

    Returns ``{"cloud": set, "local": set}`` covering every MQTT credential
    consumer discovered in the complete config — Zendure broker profiles and
    direct MQTT grid meters alike — through the one shared Core extractor.
    Legacy single-broker configs keep their inline credentials and require
    nothing here, and anonymous consumers (no ``credentials_ref``) surface no
    requirement.
    """

    requirements = {"cloud": set(), "local": set()}
    for consumer in collect_mqtt_credential_consumers(config):
        if consumer.source == SOURCE_ZENDURE_CLOUD_MQTT:
            requirements["cloud"].add(consumer.credentials_ref)
        elif consumer.source == SOURCE_LOCAL_MQTT:
            requirements["local"].add(consumer.credentials_ref)
    return requirements


def validate_all_runtime_credentials(config, *, credential_store):
    """Re-resolve every referenced runtime credential after staging.

    The final transaction boundary: individual staging helpers each verify the
    record they touch, but a later operation can overwrite or invalidate a
    record an earlier one already validated. This re-derives the requirements
    from the target config and re-validates every referenced credential through
    the Core resolver plus its source-specific completeness contract, returning
    the affected references (never a secret) for any that no longer resolve.
    """

    requirements = runtime_credential_requirements(config)
    affected = []
    for expected_source, refs in (
        (SOURCE_LOCAL_MQTT, sorted(requirements["local"])),
        (SOURCE_ZENDURE_CLOUD_MQTT, sorted(requirements["cloud"])),
    ):
        for ref in refs:
            result = credential_store.validate_runtime_credential(
                ref, expected_source=expected_source
            )
            if result.status != "valid":
                affected.append(ref)
    return affected


def stage_runtime_credentials_for_config(
    config, *, credential_store, cloud_discovery, trusted_local_credentials=None
):
    """Ensure every runtime credential the config references will resolve.

    Every referenced record is validated through the Core resolver plus the
    per-source completeness contract (all four cloud fields, a full local
    username/password pair) — file existence is never treated as usable.
    Valid records are reused untouched,
    so a no-op apply performs no write and no network call. Missing or broken
    local refs are (re)provisioned from their trusted source — a
    request-supplied pair in ``trusted_local_credentials`` (``{ref:
    (username, password)}``, e.g. the manual broker form) or otherwise the
    discovery credential pool; missing
    or broken cloud refs are (re)provisioned (fetched, persisted, verified)
    through the cloud discovery service. A broken record without a trusted
    replacement blocks the apply. Returns the staged :class:`CredentialChange`
    list for rollback after a later apply failure; on a staging failure the
    records this call touched are already rolled back before
    ``CredentialStoreError`` propagates.
    """

    validate_config_credential_references(config)
    requirements = runtime_credential_requirements(config)
    changes = []
    try:
        _stage_local_runtime_credentials(
            sorted(requirements["local"]),
            changes,
            credential_store,
            trusted=trusted_local_credentials,
        )
        for ref in sorted(requirements["cloud"]):
            _stage_cloud_runtime_credential(
                ref, changes, credential_store, cloud_discovery
            )
        # Final transaction boundary: never trust the individual staging helpers
        # as the last word — re-verify every referenced record so a later
        # operation that broke an earlier-validated record fails the apply here,
        # before config.json is written, and rolls every staged change back.
        affected = validate_all_runtime_credentials(
            config, credential_store=credential_store
        )
        if affected:
            raise CredentialStoreError(
                "Runtime MQTT credential verification failed after staging for "
                f"reference(s): {', '.join(affected)}. No config was written."
            )
    except Exception as exc:
        # A rollback failure must never mask the original staging error, but it
        # must not be silent either: combine refs the provisioning step already
        # reported with refs this rollback could not undo.
        inherited = tuple(getattr(exc, "rollback_failed_refs", ()))
        rollback_failed = credential_store.rollback_credential_changes(changes)
        failed = [*inherited, *(r for r in rollback_failed if r not in inherited)]
        if failed:
            raise CredentialProvisioningError(
                str(exc) or exc.__class__.__name__,
                credentials_ref=getattr(exc, "credentials_ref", None),
                rollback_failed_refs=failed,
            ) from exc
        raise
    return changes


def _stage_local_runtime_credentials(refs, changes, credential_store, trusted=None):
    """Validate/(re)provision local runtime records from their trusted source.

    Shared by Setup and Maintenance (both stage the refs their generated
    target config references) so both flows apply the same contract: a
    Core-resolvable complete record matching the trusted credentials is
    reused without a rewrite; a missing, unusable or outdated record is
    created/rotated from the trusted source behind a pre-change snapshot; an
    irreplaceable broken record blocks the apply before any config write.

    Per ref the trusted source is the request-supplied pair in ``trusted``
    (authoritative: the operator just typed it) or otherwise the discovery
    credential pool. A pool entry without a complete username/password pair —
    a label-only save, or one that no longer decrypts — is no replacement at
    all: rotating from it would write the empty anonymous record the
    completeness contract forbids.
    """

    trusted = trusted or {}
    seen = set()
    for raw_ref in refs or []:
        ref = credential_store.normalize_ref(raw_ref)
        if ref in seen:
            continue
        seen.add(ref)
        pair = trusted.get(ref)
        if pair is None:
            source = credential_store.load_mqtt_discovery_secret(ref)
            if source is not None and source.username and source.password:
                pair = (source.username, source.password)
        result = credential_store.validate_runtime_credential(
            ref,
            expected_source=SOURCE_LOCAL_MQTT,
            expected_username=pair[0] if pair else None,
            expected_password=pair[1] if pair else None,
        )
        if result.status == "valid":
            continue
        if pair is None:
            if result.status == "missing":
                raise CredentialStoreError(
                    f"Discovery credential reference '{ref}' was not found or "
                    "carries no complete username/password pair."
                )
            raise CredentialStoreError(
                f"Runtime MQTT credential '{ref}' exists but cannot be used "
                f"({result.reason}), and no complete discovery credential is "
                "available to replace it. Re-run MQTT discovery with working "
                "broker credentials, or restore the record under config/secrets."
            )
        changes.append(credential_store.snapshot_mqtt_credential_change(ref))
        credential_store.save_mqtt_broker_secret(ref, pair[0], pair[1])


def _stage_cloud_runtime_credential(ref, changes, credential_store, cloud_discovery):
    """Reuse a valid cloud runtime record or reprovision it transactionally."""

    result = credential_store.validate_runtime_credential(
        ref, expected_source=SOURCE_ZENDURE_CLOUD_MQTT
    )
    if result.status == "valid":
        return
    try:
        cloud_discovery.provision_runtime_credentials(
            credential_store, ref=ref, transaction=changes
        )
    except CredentialProvisioningError:
        # The structured rollback report (credentials_ref plus the refs whose
        # rollback failed) must reach the HTTP layer intact; wrapping it in a
        # contextual message would drop exactly the metadata the operator
        # needs for manual cleanup.
        raise
    except CredentialStoreError as exc:
        if result.status != "invalid":
            raise
        raise CredentialStoreError(
            f"Runtime MQTT credential '{ref}' exists but cannot be used "
            f"({result.reason}), and reprovisioning it failed: {exc}"
        ) from exc


def stage_setup_runtime_credentials(
    config, manual_broker, changes, *, credential_store, cloud_discovery
):
    """Stage every runtime credential a setup write/apply depends on.

    Runs against the generated target config so Setup shares one credential
    decision path with Maintenance (:func:`stage_runtime_credentials_for_config`):
    valid records are reused without a write or network call, broken ones are
    rotated/reprovisioned transactionally, irreparable ones block the apply.
    The manual broker's secret exists only in the request body, so it is
    persisted first and pinned as the trusted credential for its ref — the
    generated config then references a record that already resolves, and a
    same-named discovery-pool entry can never rotate it back to a stale value.
    The manual-broker change lands in ``changes`` immediately; the
    config-driven changes are rolled back internally on a staging failure and
    join ``changes`` only when staging succeeds, so the caller must roll
    ``changes`` back exactly once on any later failure.
    """

    validate_config_credential_references(config)
    manual = _stage_manual_broker_credential(manual_broker, changes, credential_store)
    changes.extend(
        stage_runtime_credentials_for_config(
            config,
            credential_store=credential_store,
            cloud_discovery=cloud_discovery,
            trusted_local_credentials=(
                {manual[0]: (manual[1], manual[2])} if manual else None
            ),
        )
    )


def _stage_manual_broker_credential(broker, changes, credential_store):
    """Persist a manual broker's username/password to the credential store.

    The generated config carries only the profile's ``credentials_ref``; the
    secret is written here so it never lands in config.json. Idempotent: an
    identical existing record is reused without a rewrite. A record under the
    same ref with different credentials is rotated to the newly entered value
    behind the same pre-change snapshot every staged change uses, so a later
    apply failure restores the previous secret byte for byte. Returns
    ``(ref, username, password)`` when the request carried a complete pair —
    the trusted credential for that ref during config-driven staging — and
    ``None`` otherwise.
    """

    from admin.config_preview import manual_broker_credentials_ref

    if not isinstance(broker, dict):
        return None
    password = broker.get("password")
    if not (isinstance(password, str) and password):
        return None
    username = str(broker.get("username") or "").strip()
    if not username:
        return None
    ref = manual_broker_credentials_ref(broker)
    existing = credential_store.load_mqtt_broker_secret(ref)
    if existing is not None and (existing.username, existing.password) == (
        username,
        password,
    ):
        return ref, username, password
    changes.append(credential_store.snapshot_mqtt_credential_change(ref))
    credential_store.save_mqtt_broker_secret(ref, username, password)
    return ref, username, password


__all__ = [
    "runtime_credential_requirements",
    "stage_runtime_credentials_for_config",
    "stage_setup_runtime_credentials",
    "validate_all_runtime_credentials",
    "validate_config_credential_references",
]
