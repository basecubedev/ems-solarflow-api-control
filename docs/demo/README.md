# Install Demo Asset

`docs/assets/install-demo.gif` and `docs/assets/install-demo.webm` are generated
from deterministic, documentation-safe frames. The terminal flow is synthetic
and the dashboard segment reuses the existing preview screenshots under
`docs/assets/preview-*.jpg`.

Regenerate the assets from the repository root:

```bash
python3 docs/demo/build_install_demo.py
```

Requirements:

- Python with Pillow
- `ffmpeg` for `install-demo.webm`

The demo intentionally uses only dummy values such as `192.0.2.10` and
`DEMO-SF800P2-1`. Do not replace these with real device IPs, serial numbers,
tokens, dashboard credentials, or host-specific shell prompts.
