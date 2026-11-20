# Admin Console demo videos

Short Admin Console workflow demos (720p, no audio), embedded in
[docs/user/admin-console.md](../../../user/admin-console.md). They use
**deterministic demo data** (fake devices, IPs, serials, image refs and backup
names) and do **not** show a real installation.

Each video is made from the console's documentation screenshots in
[`../../screenshots/admin/`](../../screenshots/admin/): every screen of the
workflow is shown for a few seconds, a screen taller than the frame scrolls
while it is shown, and the screens fade into each other.

## Formats

Each committed demo ships in two formats:

- **MP4/H.264** (`yuv420p`, `+faststart`): best compatibility for forums and
  mobile browsers. Use MP4 for forum posts (e.g. the Zendure forum) if only one
  format can be attached.
- **WebM/VP9**: compact open web format, kept as an inline fallback and download.

The user docs prefer the MP4 source first and fall back to WebM.

## Files

Currently committed videos (two, in both formats):

| Workflow | MP4 | WebM | Length |
| --- | --- | --- | --- |
| Guided Setup (fresh install with hardware discovery, feature settings and starting EMS) | `admin-guided-setup-demo.mp4` | `admin-guided-setup-demo.webm` | ~21s |
| Guided Upgrade (the version checked, the plan, the run, the Admin console reconnecting, the end) | `admin-guided-upgrade-demo.mp4` | `admin-guided-upgrade-demo.webm` | ~21s |

Planned optional future video (not committed yet):

| File | Workflow |
| --- | --- |
| `admin-backup-restore-demo.webm` | Backup creation and restore preview |

Only add the Backup/Restore video (and its embed/link in
[docs/user/admin-console.md](../../../user/admin-console.md)) once the file
actually exists — do not link to it before then.

## How to refresh

Refresh the screenshots first ([capture guide](../../screenshots/admin/README.md)),
then render the videos from them:

```bash
scripts/render_admin_docs_videos.sh            # both videos, MP4 and WebM
scripts/render_admin_docs_videos.sh upgrade    # one of them
```

The script lists the screens of each video in order. It needs `ffmpeg` with
`libx264` and `libvpx-vp9`.

## Rules

- Keep the demo data fake — no real serial numbers, IP addresses, passwords,
  tokens or personal hostnames.
- Keep the files small. If a future video becomes too large for Git, host it as
  a GitHub release asset and link it from
  [docs/user/admin-console.md](../../../user/admin-console.md) instead of
  committing the media here.
