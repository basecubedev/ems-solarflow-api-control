# Appliance release result

Run: `unnamed`  
Project: `da377375b7bebf826223d41a75bbbac487102f4f`  
Tree: `sha256:097a95759e3e688cd51e4ef2c688dd77918678fc0d144d9e64f9caade21d4759`

| Profile | Build | Image inspection | Update inspection | Sparse cross-check |
| --- | --- | --- | --- | --- |
| rpi5 | `20260811230225` | PASS (90/0/0) | PASS (18/0/0) | PASS (0/0/0) |

Counts are pass/fail/not-run, read out of each report rather than copied.

- Release gate: **RESULT: PASS (production release, 1 optional gate(s) NOT RUN)**
- Source bundle: 1201 tracked objects, 6 symlinks
- Package: `ems-appliance-manager 0.1.0 arm64`
- Attestation signature: **signed by D16E8DE0B133BD8F7BF1E6CDA5D4C295127CB181** (verified: true, trusted signer: true)
- Runtime gates: **pass** (arm64_guest=pass, docker_reconstruction=pass, networkmanager_fail_closed=pass, package_lifecycle=pass, sftp=pass)
- Source binding: **true**
- Stale: **false**
- Minimum supported medium: **32 GB**
- Physical ready: **true**
- Physical tested: **false**
