# Admin Console demo videos

This directory is reserved for short Admin Console workflow demos. No videos are
committed yet — the [screenshots](../../screenshots/admin/) show the current
workflow layout, and videos are planned for the first public Admin Console
release.

## Recommended videos

| File | Workflow | Target length |
| --- | --- | --- |
| `admin-guided-setup-demo.webm` | Fresh install / Guided Setup | 30–90s |
| `admin-backup-restore-demo.webm` | Backup creation and restore preview | 30–90s |
| `admin-guided-upgrade-demo.webm` | Guided Upgrade and Admin reconnect | 30–90s |

## Recording notes

- Format: `.webm`, 1280x720 or 1440x900, no audio required.
- Use a clean demo environment only. Record the same demo data as the
  screenshots (see [the capture guide](../../screenshots/admin/README.md) and
  the `EMS_ADMIN_DEMO_DOCS` note there is not required — the docs-preview server
  already serves demo data).
- Stop before any destructive step unless you are in a throwaway demo
  environment (do not apply config, restore live data or replace containers on a
  real host).

### Guided Setup demo

1. Open the Admin Console and select **Guided setup**.
2. Choose a demo grid meter and add a demo inverter/device.
3. Show the generated config preview.
4. Stop before applying (or apply only in a demo environment).

### Backup / Restore demo

1. Open **Maintenance** → **Backup / Restore**.
2. Show backup creation options and the backup list.
3. Show a restore preview/diff. Do not restore live data.

### Guided Upgrade demo

1. Select a target release and show the upgrade plan.
2. Show the Admin update requirement / reconnect if applicable.
3. Show the backup and config-check steps.
4. Stop before real container replacement if no demo image is available.

## Do not commit

Do not commit videos that contain real serial numbers, IP addresses, passwords,
tokens or personal hostnames. If a clean video is too large for Git, host it as a
GitHub release asset and link it from
[docs/user/admin-console.md](../../../user/admin-console.md) instead of
committing the media here.
