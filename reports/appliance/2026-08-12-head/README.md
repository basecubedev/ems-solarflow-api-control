# Appliance release evidence — 2026-08-12-head

```
Revision            da377375b7bebf826223d41a75bbbac487102f4f
Tree                sha256:097a95759e3e688cd51e4ef2c688dd77918678fc0d144d9e64f9caade21d4759
Stale               False  (the checkout is the certified revision and tree)
physical_ready      True
physical_tested     False
Unmet invariants    none
```

Every file in this directory was written by the project's own tooling, and
this summary is generated from those files rather than from notes taken
while the run happened. The signing key is an ephemeral one generated in a
temporary GNUPGHOME for this run; its private half is in no artefact.

## The release gate

```
source-authority             NOT RUN (see /zfs/tmp/appliance-head/rpi5-2/gates/source-authority.log)
slot-mounts                  PASS
artefacts-rpi5               PASS
inspect-image-rpi5           PASS
sign-rpi5                    PASS
inspect-update-rpi5          PASS
verify-signature-rpi5        PASS
crosscheck-rpi5              PASS
source-bundle                PASS

RESULT: PASS (production release, 1 optional gate(s) NOT RUN)
```

`source-authority` is NOT RUN on this host on purpose: it asks a
generator checkout for the build dependencies (mmdebstrap, podman, a
qemu-aarch64 binfmt handler) that are deliberately not installed on a
developer workstation. The same gate is PASS inside the builder guest,
which is the only place it can run.

## Builds and what was read out of them

| Profile | Build | Image inspection | Update inspection | Sparse cross-check |
| --- | --- | --- | --- | --- |
| rpi5 | `20260811230225` | PASS (90/0/0) | PASS (18/0/0) | PASS (0/0/0) |
| rpi4 | `20260811233707` | PASS (90/0/0) | — | — |

Counts are pass/fail/not-run, read out of each report. No mandatory check
was skipped in either image inspection (rpi5: 0, rpi4: 0).

Three images were built from this one revision:

- `20260811222434` rpi5, the build the release gate ran inside
- `20260811230225` rpi5, independent, the artefacts this release is cut from
- `20260811233707` rpi4

## Runtime gates

| Gate | Required | Result | Reason |
| --- | --- | --- | --- |
| `arm64_guest` | no | **pass** | RESULT: PASS (booted aarch64 guest, verified input) |
| `docker_reconstruction` | yes | **pass** | RESULT: PASS |
| `networkmanager_fail_closed` | yes | **pass** | RESULT: PASS |
| `package_lifecycle` | yes | **pass** | RESULT: PASS |
| `sftp` | yes | **pass** | RESULT: PASS |

Roll-up: **pass** — every required gate passed.

No gate reports a case as NOT RUN. `arm64_guest` is optional and passes
for the first time: earlier runs failed with the guest's record going to
the console agetty revokes, and then on a probe that gave up at 8 seconds
on a reply that takes 22. Both are fixed. See
`docs/appliance/ab-hardware-validation.md`.

## Signature and kit

```
attestation      sha256:8f32c1b3e08e814a355edfcd57afcfa1e16e5385dc9a06deafb7977e59c1768e
signature        signed by D16E8DE0B133BD8F7BF1E6CDA5D4C295127CB181
verified         True   trusted signer True
files verified   22
checksum/private-key problems  0/0
```

Readiness invariants, all of which had to hold for `physical_ready`:

```
all_mandatory_inspections_pass               True
all_profiles_verified                        True
attestation_artefacts_rehashed               True
attestation_result_pass                      True
attestation_signature_present                True
attestation_signature_verified               True
hardware_kit_verified                        True
production_gate_pass                         True
release_not_stale                            True
runtime_required_gates_pass                  True
source_bundle_verified                       True
trusted_signer                               True
```

The kit was re-verified from a copy an operator would carry, not from the
run that produced it.

## What is still not proven

No physical Raspberry Pi has run this image. `physical_tested` is false and
stays false until a board does. The ARM64 generic guest is a cross-check on
a machine that is not the target and can never be that proof.

