# Install Demo Asset

`docs/assets/install-demo.gif`, `docs/assets/install-demo.webm`, and
`docs/assets/install-demo.mp4` are generated from deterministic,
documentation-safe frames. The terminal flow is synthetic and follows the
Docker-first Analytics bootstrap:

1. the only shell commands for a fresh install,
2. the installer bootstrap output,
3. a guided `config init` session with example values entered,
4. the running dashboard.

The dashboard segment reuses the existing aggregated preview screenshot under
`docs/assets/preview-aggregated.jpg`.

The GIF is for inline embedding in the docs; the WebM/MP4 are for places that
play real video (e.g. forum posts). MP4 (H.264) embeds inline on more forums
than WebM, so prefer it when posting externally.

Regenerate the assets from the repository root:

```bash
python3 docs/demo/build_install_demo.py
```

Requirements:

- Python with Pillow
- `ffmpeg` for `install-demo.webm` and `install-demo.mp4`

The demo intentionally uses only dummy values such as `192.0.2.10`,
`192.0.2.20`, and `DEMO-SF800P2-1`, and it never prints generated InfluxDB
secrets. Do not replace these with real device IPs, serial numbers, tokens,
dashboard credentials, or host-specific shell prompts.
