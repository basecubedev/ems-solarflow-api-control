# Appliance release result

Run: `unnamed`  
Project: `50645a3b69ebe345f701efb747969084326ec4a2`  
Tree: `sha256:f7ebc6c2c617f8dd7bdfd7d99ed8fd682a501c9a8a006fb9298a0d4132553d4a`

| Profile | Build | Image inspection | Update inspection | Sparse cross-check |
| --- | --- | --- | --- | --- |
| rpi5 | `20260811155758` | PASS (90/0/0) | PASS (18/0/0) | PASS (0/0/0) |

Counts are pass/fail/not-run, read out of each report rather than copied.

- Release gate: **RESULT: PASS (production release, 1 optional gate(s) NOT RUN)**
- Source bundle: 1178 tracked objects, 6 symlinks
- Package: `ems-appliance-manager 0.1.0 arm64`
- Attestation signature: **signed by D16E8DE0B133BD8F7BF1E6CDA5D4C295127CB181** (verified: true, trusted signer: true)
- Runtime gates: **pass** (arm64_guest=fail, docker_reconstruction=pass, networkmanager_fail_closed=pass, package_lifecycle=pass, sftp=pass)
- Source binding: **true**
- Stale: **false**
- Minimum supported medium: **32 GB**
- Physical ready: **true**
- Physical tested: **false**
