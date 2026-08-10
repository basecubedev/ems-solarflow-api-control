# Appliance release result

Run: `2026-08-10-rc2`  
Project: `a84723d15d5089566196e5d440c2382325c0f26a`  
Tree: `sha256:d58c4631cab1585b8968a453b1caf2feabc9ef4fae907df76651d349c683cd27`

| Profile | Build | Image inspection | Update inspection | Sparse cross-check |
| --- | --- | --- | --- | --- |
| rpi5 | `20260810183207` | PASS (90/0/0) | PASS (18/0/0) | PASS (0/0/0) |

Counts are pass/fail/not-run, read out of each report rather than copied.

- Release gate: **RESULT: PASS (production release, 2 optional gate(s) NOT RUN)**
- Source bundle: 1157 tracked objects, 6 symlinks
- Package: `ems-appliance-manager 0.1.0 arm64`
- Attestation signature: **signed by 3BBFB52A6C856EA78C3DC47527FE69546B62D118** (verified: true, trusted signer: true)
- Runtime gates: **fail** (arm64_guest=not_run, docker_reconstruction=fail, networkmanager_fail_closed=pass, package_lifecycle=pass, sftp=fail)
- Source binding: **true**
- Stale: **false**
- Minimum supported medium: **32 GB**
- Physical ready: **false**
- Physical tested: **false**

Unmet readiness invariants: `runtime_required_gates_pass`, `hardware_kit_verified`
