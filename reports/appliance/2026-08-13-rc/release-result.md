# Appliance release result

Run: `unnamed`  
Project: `01a61335c35b3ffabca1121983f8f161c0e36f75`  
Tree: `sha256:695881c540e1b651e0b1c6f72ba4f54a09d13f00cc104440358b6bb5df990622`

| Profile | Build | Image inspection | Update inspection | Sparse cross-check |
| --- | --- | --- | --- | --- |
| rpi5 | `20260813200255` | PASS (90/0/0) | PASS (18/0/0) | PASS (0/0/0) |

Counts are pass/fail/not-run, read out of each report rather than copied.

- Release gate: **RESULT: PASS (production release, 1 optional gate(s) NOT RUN)**
- Source bundle: 1221 tracked objects, 6 symlinks
- Package: `ems-appliance-manager 0.1.0 arm64`
- Attestation signature: **signed by D16E8DE0B133BD8F7BF1E6CDA5D4C295127CB181** (verified: true, trusted signer: true)
- Runtime gates: **pass** (arm64_guest=pass, docker_reconstruction=pass, networkmanager_fail_closed=pass, package_lifecycle=pass, sftp=pass)
- Source binding: **true**
- Stale: **false**
- Minimum supported medium: **32 GB**
- Physical ready: **true**
- Physical tested: **false**
