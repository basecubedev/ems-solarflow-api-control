# Appliance Manager documentation screenshots

Generated, never hand-edited. Each image is written by
`tests/e2e-appliance/capture-docs.spec.ts` against the deterministic test
server, so no capture contains a real host name, address, serial or key.

## Regenerating

```bash
EMS_APPLIANCE_CAPTURE_DOCS=1 npx playwright test \
    --config=playwright.appliance.config.ts capture-docs --project=chromium
```

The captures are excluded from the config unless that variable is set, so the
normal appliance suite -- which runs in CI on every change to `appliance/**`,
and in the RC tier ahead of a clean-tree check -- can never overwrite them.

## Capture IDs

| ID | Page | Used in |
| --- | --- | --- |
| `appliance-first-start-password` | The first-visit password gate, showing both fields | [first-start.md](../../../user/appliance/first-start.md) |
| `appliance-login` | The password gate on a claimed appliance, with no confirmation field | [first-start.md](../../../user/appliance/first-start.md) |
| `appliance-overview` | Status tiles and quick actions | [overview.md](../../../user/appliance/overview.md) |
| `appliance-update-plan` | A confirmed-before-acting plan dialog | [updates.md](../../../user/appliance/updates.md) |
| `appliance-update-running` | An operation in flight, with the stage banner | [updates.md](../../../user/appliance/updates.md) |
| `appliance-ab-slots` | Slot state, trial status and update readiness | [updates.md](../../../user/appliance/updates.md) |
| `appliance-network-wifi` | WLAN scan and the revert warning | [network.md](../../../user/appliance/network.md) |
| `appliance-backup-access` | Backup account state and export paths | [backup.md](../../../user/appliance/backup.md) |
| `appliance-recovery` | The Admin section, where most recovery starts | [recovery.md](../../../user/appliance/recovery.md) |

## Why these are deterministic

The server behind them is the same `EMS_APPLIANCE_TEST_MODE` fixture the
browser suite uses: a scripted host with fixed versions, a fixed hostname and a
fixed set of containers. Re-running the capture produces the same pages, so a
diff in an image means the UI changed and not that the data moved.
