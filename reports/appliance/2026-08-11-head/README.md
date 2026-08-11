# Appliance release evidence — 2026-08-11-head

```
Revision            50645a3b69ebe345f701efb747969084326ec4a2
Tree                sha256:f7ebc6c2c617f8dd7bdfd7d99ed8fd682a501c9a8a006fb9298a0d4132553d4a
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
which is the only place it can run. `slot-mounts` is PASS here for the
first time; it reported NOT RUN in every earlier run.

## Builds and what was read out of them

| Profile | Build | Image inspection | Update inspection | Sparse cross-check |
| --- | --- | --- | --- | --- |
| rpi5 | `20260811155758` | PASS (90/0/0) | PASS (18/0/0) | PASS (0/0/0) |
| rpi4 | `20260811163217` | PASS (90/0/0) | — | — |

Counts are pass/fail/not-run, read out of each report. No mandatory check
was skipped in either image inspection (rpi5: 0, rpi4: 0).

Three images were built from this one revision:

- `20260811152312` rpi5, the build the release gate ran inside
- `20260811155758` rpi5, independent, the artefacts this release is cut from
- `20260811163217` rpi4

## Runtime gates

| Gate | Required | Result | Reason |
| --- | --- | --- | --- |
| `arm64_guest` | no | **fail** | RESULT: FAIL (the guest smoke test exited non-zero (APPLIANCE_SMOKE_EXIT: 1)) |
| `docker_reconstruction` | yes | **pass** | RESULT: PASS |
| `networkmanager_fail_closed` | yes | **pass** | RESULT: PASS (1 case(s) NOT RUN; the refusal was proven, the control held) |
| `package_lifecycle` | yes | **pass** | RESULT: PASS |
| `sftp` | yes | **pass** | RESULT: PASS |

Roll-up: **pass** — every required gate passed.

`arm64_guest` is optional and failed. It is not softened here: a real
aarch64 guest booted twice and neither run reached a successful result.
The same guest script passes end to end on amd64 under KVM, which is what
separates the package from the emulation. See
`docs/appliance/ab-hardware-validation.md`.

## Signature and kit

```
attestation      sha256:51d315d15c5fe0e8e02d8e3eb28bdf6b6dae1fd2690ce87c0856e0b587d16c15
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

