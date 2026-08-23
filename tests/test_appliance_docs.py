# SPDX-License-Identifier: AGPL-3.0-or-later
"""Appliance Manager documentation contract.

The user documentation has to answer a fixed set of operator questions and must
not contradict the security model, so the required documents and the claims
they make are checked here.
"""

from pathlib import Path

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.documentation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "appliance"

REQUIRED = (
    "architecture.md",
    "ab-os-updates.md",
    "ab-hardware-validation.md",
    "installation.md",
    "admin-recovery.md",
    "os-updates.md",
    "ssh-backup-access.md",
    "network-recovery.md",
    "security-model.md",
    "troubleshooting.md",
)


def read(name):
    return (DOCS / name).read_text(encoding="utf-8")


def test_every_required_document_exists():
    for name in REQUIRED:
        assert (DOCS / name).is_file(), name


def test_the_documentation_index_links_the_appliance_documents():
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    for name in REQUIRED:
        assert f"appliance/{name}" in index, name


def test_product_boundaries_are_stated():
    architecture = read("architecture.md")
    assert "Appliance Manager" in architecture
    assert "EMS Admin Console" in architecture
    assert "Guided Setup" in architecture
    assert "never edits EMS configuration" in architecture or "never edit" in architecture


def test_the_two_services_and_the_socket_are_documented():
    architecture = read("architecture.md")
    assert "ems-appliance-web.service" in architecture
    assert "ems-appliance-agent.service" in architecture
    assert "/run/ems-appliance-manager/agent.sock" in architecture
    assert "never a network listener" in architecture


def test_supported_hardware_and_os_are_documented():
    installation = read("installation.md")
    assert "Raspberry Pi 4" in installation
    assert "Raspberry Pi 5" in installation
    assert "Raspberry Pi OS 64-bit" in installation
    assert "arm64" in installation


def test_the_unsupported_raspberry_pi_3_is_documented_where_platforms_are_declared():
    """A board that cannot boot the image must be named next to the ones that can.

    An operator holding a Pi 3 reads the supported table, sees "Raspberry Pi 4,
    Raspberry Pi 5", and reasonably wonders whether 3 is merely untested. It is
    not, and the reason is a boot chain rather than a benchmark.
    """

    installation = read("installation.md")
    assert "Raspberry Pi 3" in installation
    assert "adr/raspberry-pi-3-ab-support.md" in installation
    assert (DOCS / "adr" / "raspberry-pi-3-ab-support.md").is_file()


def test_the_pi3_decision_record_keeps_its_evidence():
    """The ADR is only useful if it still says which facts decided it."""

    adr = (DOCS / "adr" / "raspberry-pi-3-ab-support.md").read_text(encoding="utf-8")
    assert "regex:^(cm4|pi4|cm5|pi5)$" in adr
    assert "bootcode.bin" in adr
    assert "0xEE" in adr or "0xee" in adr
    # The decision has a stated trigger for being revisited rather than being
    # permanent by omission.
    assert "tests/test_appliance_pi3_support.py" in adr


def test_the_package_failure_contract_is_documented():
    installation = read("installation.md")
    assert "sudo ems-appliance verify-install" in installation
    assert "deferred, not failed" in installation
    assert "--offline" in installation
    # An optional feature must not read like a broken installation.
    assert "Optional host features never fail the package" in installation
    assert "migration conflict" in installation


def test_the_smoke_test_drivers_are_documented():
    installation = read("installation.md")
    assert "scripts/appliance-smoke-amd64.sh" in installation
    assert "scripts/appliance-smoke-arm64.sh" in installation
    assert "RESULT: NOT RUN" in installation
    assert "never mistaken for a pass" in installation


def test_the_local_password_reset_is_documented():
    installation = read("installation.md")
    assert "sudo ems-appliance password-reset" in installation
    assert "no unauthenticated network reset endpoint" in installation


def test_failed_admin_update_recovery_is_documented():
    recovery = read("admin-recovery.md")
    assert "rolled_back" in recovery
    assert "previous known-good" in recovery
    assert "Recovering a failed Admin update" in recovery


def test_installing_a_specific_version_is_documented():
    recovery = read("admin-recovery.md")
    assert "Exact release tag" in recovery
    assert "v0.8.0" in recovery


def test_os_update_documentation_refuses_unattended_distribution_upgrades():
    updates = read("os-updates.md")
    assert "Major OS upgrades" in updates
    assert "not supported" in updates
    assert "never removed" in updates


def test_ssh_and_file_transfer_instructions_match_the_supported_protocol():
    access = read("ssh-backup-access.md")
    assert "ssh-ed25519" in access
    assert "sftp -r ems-backup@" in access
    assert "ForceCommand internal-sftp" in access
    # The account cannot execute a remote command, so neither may be advertised.
    assert "rsync and scp do not work" in access
    assert "Never paste a private key" in access


def test_the_sftp_confinement_is_documented():
    access = read("ssh-backup-access.md")
    assert "ChrootDirectory /srv/ems-appliance-export" in access
    assert "read-only bind mount" in access
    # A doc that only promised ForceCommand would overstate the boundary.
    assert "removes the shell but not the filesystem" in access
    assert "ems-appliance-export.service" in access
    assert "ems-appliance-export.path" in access
    for state in ("configured", "pending", "degraded", "unknown"):
        assert f"`{state}`" in access, state


def test_the_state_split_and_socket_ownership_are_documented():
    security = read("security-model.md")
    assert "root:ems-appliance 0750" in security
    assert "/var/lib/ems-appliance-manager/web" in security
    assert "append-only" in security


def test_agent_state_is_documented_as_private_to_root():
    security = read("security-model.md")
    assert "root:root 0700" in security
    assert "neither write, read nor list" in security
    assert "InaccessiblePaths" in security
    # The replacement for the direct reads must be named, or the document
    # would describe a capability the operator silently lost.
    for operation in ("operations.list", "operations.get", "admin.get", "logs.read"):
        assert operation in security, operation


def test_the_agent_owned_audit_path_is_documented():
    security = read("security-model.md")
    assert "audit.record_web_event" in security
    assert "security_audit" in security
    assert "sudo ems-appliance password-reset" in security


def test_the_relaxed_sandbox_directives_are_justified():
    security = read("security-model.md")
    assert "AF_UNIX AF_INET AF_INET6" in security
    assert "RestrictSUIDSGID" in security
    assert "Address family not supported by protocol" in security
    assert "Operation not permitted" in security


def test_repair_result_states_are_documented():
    recovery = read("admin-recovery.md")
    for state in ("succeeded", "failed_recoverable", "manual_action_required", "failed_terminal"):
        assert state in recovery, state


def test_the_rollback_preflight_order_is_documented():
    recovery = read("admin-recovery.md")
    assert "the running Admin is never stopped" in recovery
    assert "admin_untouched" in recovery
    for code in ("invalid_known_good_record", "known_good_image_unavailable"):
        assert code in recovery, code
    # The order matters: the stop must be documented as coming after the write.
    assert recovery.index("write the rollback reference") < recovery.index(
        "only now: stop the running Admin"
    )


def test_lifecycle_verification_is_documented():
    recovery = read("admin-recovery.md")
    assert "without a Docker health check does not count as healthy" in recovery
    for failure in (
        "api_unreachable",
        "image_mismatch",
        "version_mismatch",
        "version_unreadable",
        "container_missing",
        "container_still_running",
    ):
        assert failure in recovery, failure


def test_digest_pinning_is_documented():
    recovery = read("admin-recovery.md")
    assert "repository@sha256:" in recovery
    assert "digest_unresolved" in recovery
    assert "tag is never resolved again" in recovery


def test_the_state_migration_is_documented():
    installation = read("installation.md")
    assert "ems-appliance migrate-state" in installation
    assert "symlinked source" in installation


def test_wlan_recovery_is_documented():
    network = read("network-recovery.md")
    assert "never deleted" in network
    assert "Recovering access after a WLAN change" in network
    assert "Ethernet" in network


def test_the_security_model_documents_the_privilege_boundary():
    security = read("security-model.md")
    assert "no root" in security
    assert "Docker socket is never exposed to the web process" in security
    assert "PBKDF2-SHA256" in security
    assert "SO_PEERCRED" in security


def test_the_security_model_lists_the_absent_capabilities():
    security = read("security-model.md")
    for absent in (
        "arbitrary shell execution",
        "a browser-based terminal",
        "unrestricted Docker container management",
        "an unauthenticated password-reset endpoint",
    ):
        assert absent in security, absent


def test_troubleshooting_answers_every_required_question():
    troubleshooting = read("troubleshooting.md")
    for question in (
        "What does the Appliance Manager manage?",
        "What does the EMS Admin Console manage?",
        "How do I recover a failed Admin update?",
        "How do I install a specific Admin version?",
        "How do I add an SSH key?",
        "How do I back up files with rsync?",
        "How do I install OS updates?",
        "How do I recover access after a WLAN change?",
        "How do I reset the Appliance Manager password locally?",
    ):
        assert question in troubleshooting, question


def test_documentation_does_not_promise_hardware_validation_it_lacks():
    for name in REQUIRED:
        text = read(name)
        assert "certified" not in text.lower(), name
        assert "guaranteed" not in text.lower(), name


# --- the ownership and ACL contract the code actually implements ------------


def test_the_legacy_ownership_states_are_documented():
    model = read("security-model.md")
    for state in (
        "current",
        "legacy_manual_migration_required",
        "ownership_conflict",
        "marker_missing",
        "marker_mismatch",
        "record_corrupt",
        "no_ownership_record",
    ):
        assert state in model, state


def test_the_explicit_migration_is_documented_as_the_only_adoption():
    model = read("security-model.md")
    assert "backup-account migrate-ownership" in model
    assert "no force-adopt flag" in model
    assert "never a sufficient one" in model


def test_a_schema_less_record_is_documented_as_unadoptable():
    model = read("security-model.md")
    assert "schema-less" in model.lower()
    assert "not adoptable" in model.lower()


def test_the_operator_document_names_the_migration_command():
    access = read("ssh-backup-access.md")
    assert "ems-appliance backup-account status" in access
    assert "ems-appliance backup-account migrate-ownership" in access
    assert "Reinstalling does" in access


def test_the_mandatory_and_optional_identity_halves_are_documented():
    model = read("security-model.md")
    assert "mandatory:" in model and "optional:" in model
    assert "lsattr" in model
    assert "strengthens" in model


def test_the_acl_transaction_states_are_documented():
    model = read("security-model.md")
    for state in (
        "staging",
        "rollback_required",
        "rollback_complete",
        "recovery_required",
        "committed",
    ):
        assert f"`{state}`" in model, state
    assert "acl-transaction.state" in model
    assert "acl-manifest.tsv.uncommitted" in model


def test_the_open_handle_rollback_authority_is_documented():
    model = read("security-model.md")
    assert "descriptor is held for the whole" in model
    assert "never the mutation authority" in model


def test_the_arm64_evidence_contract_is_documented():
    installation = read("installation.md")
    for artifact in (
        "result.txt",
        "inputs.txt",
        "run.txt",
        "environment.txt",
        "missing-requirements.txt",
    ):
        assert artifact in installation, artifact
    assert "evidence_complete" in installation
    assert "reason_code" in installation
    assert "latest.txt" in installation
    assert "never left empty" in installation


# --- A/B operating-system updates -------------------------------------------


def test_the_three_rollbacks_are_named_as_different_things():
    """Admin image, EMS data and OS slot recover different failures."""

    document = read("ab-os-updates.md")

    assert "Admin image rollback" in document
    assert "EMS backup / restore" in document
    assert "OS A/B rollback" in document
    assert "EEPROM" in document


def test_ab_is_never_claimed_for_docker_containers():
    document = read("ab-os-updates.md")

    assert "A/B **never** applies to Docker containers" in document
    assert "the word \"slot\" is not used for" in document


def test_the_supported_hardware_scope_is_stated_and_not_widened():
    """One image per board. "Pi 4 or later" is exactly the claim to avoid."""

    document = read("ab-os-updates.md")

    assert "Raspberry Pi 4 or later" not in document
    assert "Raspberry Pi 4 Model B and Raspberry Pi 5, as separate artefacts" in document
    assert "One image per board, and no image claims another's hardware" in document
    assert "arm64" in document
    assert "microSD, USB mass storage or NVMe" in document
    assert "never inferred from another" in document
    assert "hardware_not_supported" in document


def test_the_slot_model_and_the_selector_are_documented():
    document = read("ab-os-updates.md")

    assert "tryboot_a_b=1" in document
    assert "[tryboot]" in document
    assert "boot_partition" in document
    assert "never guessed at runtime" in document


def test_every_update_state_is_documented():
    document = read("ab-os-updates.md")

    for state in (
        "unsupported",
        "single_slot",
        "ready",
        "staging",
        "writing_inactive",
        "verifying_inactive",
        "ready_for_tryboot",
        "tryboot_requested",
        "booted_trial",
        "health_verifying",
        "committing",
        "committed",
        "fallback_observed",
        "failed_recoverable",
        "manual_action_required",
        "failed_terminal",
    ):
        assert state in document, state


def test_the_persistence_contract_names_what_is_shared_and_what_is_not():
    """The contract has one owning document; ab-os-updates.md links to it."""

    document = read("ab-persistence-contract.md")

    for shared in ("/opt/ems-solarflow", "/var/lib/ems-appliance-manager", "ssh_host_"):
        assert shared in document, shared
    for local in ("/var/lib/dpkg", "/var/lib/docker", "/lib/modules"):
        assert local in document, local
    assert "fails closed" in document
    assert "ab-persistence-contract.md" in read("ab-os-updates.md")


def test_the_docker_state_decision_is_explicit():
    document = read("ab-persistence-contract.md")

    assert "`/var/lib/docker` is slot-local by decision" in document
    assert "seeded onto the shared partition" in document


def test_the_persistence_contract_states_the_machine_and_ssh_decisions():
    document = read("ab-persistence-contract.md")

    assert "One physical appliance is one Linux machine" in document
    assert "`/etc/ssh` as a whole is **not** shared" in document


def test_no_in_place_conversion_is_offered_anywhere():
    for name in ("ab-os-updates.md", "installation.md", "os-updates.md", "security-model.md"):
        document = read(name)
        assert "repartition" in document.lower(), name
    assert "never converted in place" in read("installation.md")


def test_the_adr_exists_and_explains_why_native_tryboot():
    adr = (DOCS / "adr" / "ab-native-tryboot.md").read_text(encoding="utf-8")

    assert "Status: accepted" in adr
    assert "Do **not** add RAUC" in adr
    assert "No blocking limitation of native tryboot was found" in adr
    assert "Do not run two competing A/B mechanisms" in adr


def test_the_hardware_gate_claims_nothing_that_was_not_run():
    gate = read("ab-hardware-validation.md")

    assert "NOT RUN" in gate
    assert "microSD" in gate and "NVMe" in gate
    assert "A pass on one\nclass is never reported for another" in gate.replace(
        "**A pass on one", "A pass on one"
    ) or "never reported for another" in gate


def test_the_operator_documentation_states_both_update_modes():
    document = read("os-updates.md")

    assert "Single-slot" in document
    assert "A/B appliance image" in document
    assert "one-shot" in document
    assert "Nothing is retried\nautomatically" in document or "not retried" in document.lower()


def test_troubleshooting_covers_a_fallback_and_layout_drift():
    document = read("troubleshooting.md")

    assert "returned to the previous slot" in document
    assert "layout_drift" in document
    assert "manual action is required" in document


def test_the_deployment_authority_and_its_states_are_documented():
    document = read("ab-os-updates.md")

    assert "deployment fingerprint" in document
    for state in (
        "absent",
        "running",
        "stopped_clean",
        "failed",
        "restarting",
        "created",
        "unknown",
    ):
        assert state in document, state
    assert "compose" in document and ".env" in document
    assert "Create a new update plan before continuing" in document


def test_the_documentation_states_that_drift_is_never_auto_refreshed():
    document = read("ab-os-updates.md")

    assert "never re-recorded" in document
    assert "offered no way around it" in document


def test_os_rollback_is_documented_as_not_a_deployment_rollback():
    document = read("ab-os-updates.md")

    assert "Rolling the OS back is not rolling the deployment back" in document
    assert "OS slot rollback   ≠   EMS configuration rollback   ≠   database rollback" in (
        document
    )
    assert "does not restore an older compose file" in document


def test_reconstruction_is_documented_as_digest_and_platform_proven():
    document = read("ab-os-updates.md")

    assert "docker load" in document and "not evidence" in document
    assert "amd64" in document
    assert "repository@sha256" in document


def test_the_build_provenance_gate_is_documented():
    document = (
        ROOT / "packaging" / "appliance" / "image" / "README.md"
    ).read_text(encoding="utf-8")

    assert "build-authority.json" in document
    assert "provenance_unverified" in document
    assert "rpi_image_gen_source_modified" in document
    assert "appliance-check-source-bundle.sh" in document
    assert "appliance-release-gates.sh" in document
    for code in (
        "project_source_dirty",
        "project_source_untracked",
        "build_source_changed_during_build",
    ):
        assert code in document, code


def test_the_release_gate_exit_contract_is_documented():
    document = (
        ROOT / "packaging" / "appliance" / "image" / "README.md"
    ).read_text(encoding="utf-8")

    for outcome in ("RESULT: PASS", "RESULT: FAIL", "RESULT: NOT RUN", "RESULT: INCOMPLETE"):
        assert outcome in document, outcome
    assert "--allow-not-run" in document


def test_the_confirmed_authority_shape_is_documented():
    document = read("ab-os-updates.md")

    assert "os_write" in document
    assert "deployment_fingerprint" in document
    assert "deployment_schema" in document
    assert "replan_required" in document
    assert "*before* the target slot is invalidated" in document


def test_the_selector_reconciliation_rule_is_documented():
    document = read("ab-os-updates.md")

    assert "The selector is what the next boot reconciles from" in document
    assert "Only `commit` ever moves the default" in document


def test_the_compute_module_support_decision_is_documented():
    document = read("ab-os-updates.md")

    assert "hardware_has_no_build_profile" in document
    assert "CM4 and\nCM5" in document or "CM4 and CM5" in document


def test_the_hardware_gate_lists_the_new_authority_cases():
    gate = read("ab-hardware-validation.md")

    for case in (
        "deployment_authority_drift",
        "build_authority_mismatch",
        "host_identity_keypair_mismatch",
        "host_identity_not_durable",
        "rpi_image_gen_source_modified",
    ):
        assert case in gate, case


def test_the_media_requirement_appears_where_a_buyer_reads_it():
    """media_sizing.py enforces 30 GB while the hardware page said "a few GB".
    The number only existed in the maintainer gate, so a reader bought a card
    that cannot hold the image."""

    from appliance import media_sizing

    page = (ROOT / "docs/user/hardware-requirements.md").read_text(encoding="utf-8")

    assert media_sizing.SUPPORTED_MEDIA_LABEL in page
    assert "16 GB" in page, "the size that does not work is worth naming"


# --- what a first boot on real hardware leaves behind ------------------------
# The appliance ships no login account and the image is not confirmed on a
# physical board, so the first person to try one has exactly two channels when
# it does not come up: the serial line, and the partitions their computer can
# read. Both are promised in the user guide, and neither was checked against
# what the image actually is.

USER_DOCS = ROOT / "docs" / "user" / "appliance"


def user_doc(name):
    return (USER_DOCS / name).read_text(encoding="utf-8")


def flowed(name):
    """The guide as one line, so an assertion about a sentence survives rewrapping."""

    return " ".join(user_doc(name).split())


def test_the_serial_console_the_guide_sends_people_to_is_asserted_of_the_image():
    from appliance import ab_image

    guide = user_doc("recovery.md")

    assert "115200" in guide
    assert "SERIAL_CONSOLES" in dir(ab_image)
    assert "boot_console" in (ROOT / "appliance" / "ab_image.py").read_text(encoding="utf-8")


def test_the_firmware_speaks_before_the_kernel_and_the_image_is_held_to_it():
    """The failure a serial line answers that nothing else can is the one that
    happens before the kernel runs."""

    from appliance import ab_image

    assert ab_image.FIRMWARE_UART_SETTINGS
    assert "boot_firmware_uart" in (ROOT / "appliance" / "ab_image.py").read_text(encoding="utf-8")


def test_the_partitions_the_guide_tells_people_to_open_are_the_ones_that_are_fat():
    from appliance import ab_image

    readable = ("bootconfig", *ab_image.BOOT_PARTITIONS)
    every = (*readable, *ab_image.SYSTEM_ROOTS, "persistent")
    assert len(readable) == 3 and len(every) == 6
    assert "Three of its six partitions are FAT" in flowed("recovery.md")


def test_the_files_the_guide_asks_for_are_files_the_image_check_reads():
    from appliance import ab_image

    guide = user_doc("recovery.md")

    for name in (ab_image.AUTOBOOT_FILE, "cmdline.txt", "config.txt"):
        assert name in guide, f"the guide does not name {name}"


def test_the_first_hardware_report_asks_for_the_capture_that_cannot_be_redone():
    """A boot that fails leaves nothing behind, so the serial capture has to be
    started before the board is powered on -- the guide has to say so first."""

    index = flowed("index.md")

    assert "Before you power it on" in index
    assert "recovery.md#watch-it-boot" in index


# --- the FAQ is a third place the appliance is described ---------------------
# It answers for all three setup paths, so it repeats numbers and words that
# belong to something else. Repeating them is fine; nobody checking them is not.

FAQ = ROOT / "docs" / "user" / "faq.md"


def test_the_port_the_faq_tells_people_to_open_is_the_port_the_appliance_serves():
    from appliance.config import DEFAULT_WEB_PORT

    faq = FAQ.read_text(encoding="utf-8")

    assert f":{DEFAULT_WEB_PORT}" in faq


LANDING_PAGES = (
    Path("docs/README.md"),
    Path("docs/user/index.md"),
    Path("docs/user/faq.md"),
)


@pytest.mark.parametrize("page", LANDING_PAGES, ids=lambda page: page.name)
def test_every_page_a_reader_lands_on_says_the_same_thing_about_the_appliance(page):
    """Three pages introduce it. A reader who meets a different claim on each
    cannot tell which one is current."""

    text = " ".join((ROOT / page).read_text(encoding="utf-8").split())

    assert "not confirmed on physical hardware" in text.replace("**", "")


def test_that_claim_is_the_tier_the_support_table_assigns_it():
    setups = " ".join(
        (ROOT / "docs" / "user" / "supported-setups.md").read_text(encoding="utf-8").split()
    )

    assert "Appliance image (Pi 4 / Pi 5) | **Reverse-engineered**" in setups


def test_the_faq_does_not_offer_a_login_the_image_has_no_account_for():
    faq = " ".join(FAQ.read_text(encoding="utf-8").split())

    assert "ships no login account" in faq


def test_the_faq_names_every_connection_a_device_can_be_reached_on():
    """It described an address-only world after MQTT became a control transport,
    which made "each device needs a real IP address" simply wrong."""

    faq = FAQ.read_text(encoding="utf-8")
    setups = (ROOT / "docs" / "user" / "supported-setups.md").read_text(encoding="utf-8")

    for connection in ("Local API", "Local MQTT", "Zendure cloud MQTT"):
        assert connection in setups, f"supported-setups.md no longer names {connection}"
        assert connection in faq, f"the FAQ does not name {connection}"


def test_the_flashing_guide_names_the_files_the_build_produces():
    """The page frames the checksum step as the safety gate, so every command it
    gives has to work against artefacts that exist."""

    root = Path(__file__).resolve().parents[1]
    guide = (root / "docs" / "user" / "appliance" / "install.md").read_text(encoding="utf-8")
    build = (
        root / "scripts" / "appliance-build-rpi-ab-image.sh"
    ).read_text(encoding="utf-8")

    assert '"$NAME.img.xz" > "$NAME.img.xz.sha256"' in build
    assert "-arm64-ab.img.xz" in guide
    assert "-arm64-ab.img.xz.sha256" in guide
    # The digest has to cover the file that was downloaded: a checksum over the
    # raw image cannot verify the compressed one it was published as.
    assert ".img.sha256" not in guide.replace(".img.xz.sha256", "")


def test_the_security_model_does_not_claim_a_check_the_code_does_not_make():
    """An empty argv member is deliberately legitimate; the document said the
    opposite, so a reader auditing the boundary would look for enforcement that
    is not there -- and might restore it, breaking host key generation."""

    model = read("security-model.md")

    assert "must be a non-empty string" not in model
    assert "An empty member is legitimate" in model


def test_the_security_model_states_what_reaching_the_agent_is_worth():
    """"No shell" bounds the shape of the actions, not their privilege."""

    model = read("security-model.md")

    assert "appliance-takeover capability" in model


def test_the_page_that_erases_a_card_says_the_image_is_unconfirmed():
    """The caveat belongs where the destructive step is, not only on an index."""

    root = Path(__file__).resolve().parents[1]
    guide = (root / "docs" / "user" / "appliance" / "install.md").read_text(encoding="utf-8")

    assert "Not confirmed on physical hardware" in guide
    assert "what that means" in guide


def test_the_maintainer_platform_table_carries_the_support_tier():
    platforms = read("installation.md")

    assert "reverse-engineered" in platforms.lower()


def test_every_appliance_module_appears_in_the_module_map():
    """A map missing a third of its subsystems sends a reader looking in the
    wrong place, and nothing noticed as the branch grew."""

    root = Path(__file__).resolve().parents[1]
    document = read("architecture.md")
    modules = sorted(
        path.stem
        for path in (root / "appliance").glob("*.py")
        if path.stem not in ("__init__", "__main__")
    )

    missing = [name for name in modules if f"`{name}.py`" not in document]

    assert not missing, missing
