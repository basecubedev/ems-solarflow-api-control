# Admin Console demo videos

Short Admin Console workflow demos (720p, no audio), embedded in
[docs/user/admin-console.md](../../../user/admin-console.md). They use
**deterministic demo data** (fake devices, IPs, serials, image refs and backup
names) and do **not** show a real installation.

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
| Guided Setup (fresh install with hardware discovery, feature settings and starting EMS) | `admin-guided-setup-demo.mp4` | `admin-guided-setup-demo.webm` | ~28s |
| Guided Upgrade (EMS software upgrade with the live "Upgrade validation" box ticking off each step) | `admin-guided-upgrade-demo.mp4` | `admin-guided-upgrade-demo.webm` | ~26s |

Planned optional future video (not committed yet):

| File | Workflow |
| --- | --- |
| `admin-backup-restore-demo.webm` | Backup creation and restore preview |

Only add the Backup/Restore video (and its embed/link in
[docs/user/admin-console.md](../../../user/admin-console.md)) once the file
actually exists — do not link to it before then.

## How to refresh

The videos are rendered from the **real** Admin static UI (`admin/static/`)
driven by the docs-preview server with deterministic demo API responses from
[`tests/fixtures/admin_docs/`](../../../../tests/fixtures/admin_docs). Each
workflow step is captured as a full-page frame with headless Firefox and the
frames are stitched into a 1280x720 `.webm` (VP9) with gentle vertical pans, so
tall pages stay readable. No hardware, Docker, discovery, MQTT, `config.json` or
password is involved, and nothing is written to config/runtime state.

Requirements: `firefox` (headless), ImageMagick `convert` and `ffmpeg`.
(MP4-only conversion needs just `ffmpeg` — it reuses the committed WebM.)

Render both videos in both formats (WebM + MP4, the default):

```bash
python3 scripts/render_admin_docs_video.py
```

Render one workflow, or pick formats with `--format {webm,mp4,all}`:

```bash
python3 scripts/render_admin_docs_video.py --videos admin-guided-setup-demo.webm
python3 scripts/render_admin_docs_video.py --format webm    # WebM only
python3 scripts/render_admin_docs_video.py --format mp4     # MP4 from existing WebM
```

The MP4 is a straight H.264 transcode of the rendered WebM. To make one by hand
(same encoding the script uses — forum/mobile-friendly, no audio):

```bash
ffmpeg -i admin-guided-setup-demo.webm \
  -c:v libx264 -pix_fmt yuv420p -movflags +faststart \
  -crf 23 -preset medium -an \
  admin-guided-setup-demo.mp4
```

If an MP4 comes out too large, retry with `-crf 26`; if quality looks too low,
`-crf 20`. Keep 1280x720 — do not upscale.

To change the steps, durations or which screens appear, edit `VIDEOS` in
`scripts/render_admin_docs_video.py`; to change what a screen shows, edit the
matching fixture in `tests/fixtures/admin_docs/`.

## Rules

- Keep the demo data fake — no real serial numbers, IP addresses, passwords,
  tokens or personal hostnames.
- Keep the files small. If a future video becomes too large for Git, host it as
  a GitHub release asset and link it from
  [docs/user/admin-console.md](../../../user/admin-console.md) instead of
  committing the media here.
