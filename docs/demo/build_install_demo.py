# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build the Docker-first install demo GIF/WebM from synthetic frames."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "docs" / "assets"
OUTPUT_GIF = ASSETS / "install-demo.gif"
OUTPUT_WEBM = ASSETS / "install-demo.webm"
WIDTH = 900
HEIGHT = 506
TERMINAL_HEIGHT = 362
DASHBOARD_HEIGHT = 320
FPS = 5


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for name in names:
        if name and Path(name).exists():
            return ImageFont.truetype(name, size=size)
    return ImageFont.load_default()


FONT_TITLE = load_font(24, bold=True)
FONT_BODY = load_font(18)
FONT_SMALL = load_font(15)
FONT_MONO = load_font(15)
FONT_MONO_BOLD = load_font(15, bold=True)


def text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def draw_panel(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], fill: str, outline: str) -> None:
    draw.rounded_rectangle(xy, radius=8, fill=fill, outline=outline, width=1)


def base_frame(title: str, subtitle: str) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), "#f5f7fb")
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, WIDTH, 70), fill="#18212f")
    draw.text((30, 18), title, font=FONT_TITLE, fill="#f8fafc")
    draw.text((30, 46), subtitle, font=FONT_SMALL, fill="#b8c2d2")
    return img


def terminal_frame(title: str, subtitle: str, lines: list[tuple[str, str]]) -> Image.Image:
    img = base_frame(title, subtitle)
    draw = ImageDraw.Draw(img)
    draw_panel(draw, (32, 92, WIDTH - 32, 92 + TERMINAL_HEIGHT), "#101620", "#273346")
    draw.rectangle((32, 92, WIDTH - 32, 122), fill="#1f2937")
    for i, color in enumerate(("#ef4444", "#f59e0b", "#22c55e")):
        draw.ellipse((50 + i * 22, 102, 62 + i * 22, 114), fill=color)
    draw.text((132, 101), "demo shell - empty folder install", font=FONT_SMALL, fill="#d1d5db")

    y = 142
    for kind, text in lines:
        color = {
            "cmd": "#93c5fd",
            "out": "#d1d5db",
            "ok": "#86efac",
            "warn": "#facc15",
            "dim": "#94a3b8",
        }[kind]
        font = FONT_MONO_BOLD if kind == "cmd" else FONT_MONO
        draw.text((52, y), text, font=font, fill=color)
        y += 24
    return img


def dashboard_frame(view: str, title: str, subtitle: str) -> Image.Image:
    img = base_frame(title, subtitle)
    draw = ImageDraw.Draw(img)
    source = ASSETS / f"preview-{view}.jpg"
    preview = Image.open(source).convert("RGB")

    crop_ratio = WIDTH / DASHBOARD_HEIGHT
    source_ratio = preview.width / preview.height
    if source_ratio > crop_ratio:
        new_width = int(preview.height * crop_ratio)
        left = max((preview.width - new_width) // 2, 0)
        preview = preview.crop((left, 0, left + new_width, preview.height))
    else:
        new_height = int(preview.width / crop_ratio)
        top = 0
        preview = preview.crop((0, top, preview.width, min(top + new_height, preview.height)))

    preview = preview.resize((WIDTH - 64, DASHBOARD_HEIGHT), Image.Resampling.LANCZOS)
    draw_panel(draw, (32, 92, WIDTH - 32, HEIGHT - 38), "#ffffff", "#d7dde8")
    img.paste(preview, (32, 92))
    draw.rounded_rectangle((620, 408, 862, 438), radius=6, fill="#18212f")
    draw.text((636, 416), "http://localhost:8080", font=FONT_SMALL, fill="#f8fafc")
    return img


def build_frames() -> list[tuple[Image.Image, int]]:
    prompt = "demo@ems-demo:~/ems-demo$"
    return [
        (
            terminal_frame(
                "Docker-first install preview",
                "Start in an empty folder; no repository clone required.",
                [
                    ("cmd", "demo@ems-demo:~$ mkdir ems-demo && cd ems-demo"),
                    ("out", "folder is empty"),
                    ("cmd", f"{prompt} ls -A"),
                    ("dim", "(no files yet)"),
                ],
            ),
            3200,
        ),
        (
            terminal_frame(
                "Download the installer",
                "The installer writes the Compose file and local folders.",
                [
                    ("cmd", f"{prompt} curl -fsSLo install-docker.sh \\"),
                    ("cmd", "  https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/install-docker.sh"),
                    ("out", "install-docker.sh saved"),
                    ("cmd", f"{prompt} sh install-docker.sh"),
                    ("ok", "created docker-compose.yml, config/, data/"),
                    ("ok", "starting EMS service ... done"),
                ],
            ),
            5000,
        ),
        (
            terminal_frame(
                "Guided setup with demo values",
                "Only documentation-safe dummy values are shown.",
                [
                    ("cmd", f"{prompt} docker compose exec ems python3 emsctl.py config init"),
                    ("out", "Shelly IP: 192.0.2.10"),
                    ("out", "Inverter serial: DEMO-SF800P2-1"),
                    ("out", "Second inverter serial: DEMO-SF800P2-2"),
                    ("out", "System limit: 800 W"),
                    ("out", "Min/Max SoC: 15 / 90"),
                    ("ok", "config/config.json updated"),
                ],
            ),
            6200,
        ),
        (
            terminal_frame(
                "Start and check status",
                "The dashboard is published on port 8080.",
                [
                    ("cmd", f"{prompt} docker compose up -d"),
                    ("ok", "Container ems  Started"),
                    ("cmd", f"{prompt} docker compose ps"),
                    ("out", "NAME   SERVICE   STATUS   PORTS"),
                    ("ok", "ems    ems       running  0.0.0.0:8080->8080/tcp"),
                    ("cmd", f"{prompt} docker compose exec ems python3 emsctl.py diagnose"),
                    ("ok", "diagnose completed - dashboard ready: http://localhost:8080"),
                ],
            ),
            6200,
        ),
        (
            dashboard_frame(
                "aggregated",
                "Dashboard overview",
                "Open http://localhost:8080 and confirm live status at a glance.",
            ),
            6200,
        ),
        (
            dashboard_frame(
                "devices",
                "Device view",
                "Synthetic devices show where inverter state and limits appear.",
            ),
            5200,
        ),
        (
            dashboard_frame(
                "energy",
                "Energy view",
                "Analytics preview uses demo history, not private runtime data.",
            ),
            5200,
        ),
    ]


def write_gif(frames: list[tuple[Image.Image, int]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    images = [frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=128) for frame, _ in frames]
    durations = [duration for _, duration in frames]
    images[0].save(
        output,
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )


def write_webm(frames: list[tuple[Image.Image, int]], output: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("ffmpeg not found; skipped WebM generation")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="install-demo-frames-") as tmp:
        tmp_path = Path(tmp)
        concat_path = tmp_path / "frames.txt"
        concat_lines = []
        last_frame = None
        for index, (frame, duration_ms) in enumerate(frames):
            path = tmp_path / f"frame-{index:03d}.png"
            frame.save(path)
            concat_lines.append(f"file '{path.as_posix()}'\n")
            concat_lines.append(f"duration {duration_ms / 1000:.3f}\n")
            last_frame = path
        if last_frame:
            concat_lines.append(f"file '{last_frame.as_posix()}'\n")
        concat_path.write_text("".join(concat_lines), encoding="utf-8")
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-vf",
                f"fps={FPS},format=yuv420p",
                "-c:v",
                "libvpx-vp9",
                "-b:v",
                "0",
                "-crf",
                "38",
                str(output),
            ],
            check=True,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gif", default=OUTPUT_GIF, type=Path)
    parser.add_argument("--webm", default=OUTPUT_WEBM, type=Path)
    parser.add_argument("--skip-webm", action="store_true")
    return parser.parse_args()


def main() -> None:
    os.chdir(ROOT)
    args = parse_args()
    frames = build_frames()
    write_gif(frames, args.gif)
    print(f"wrote {args.gif.relative_to(ROOT)}")
    if not args.skip_webm:
        write_webm(frames, args.webm)
        if args.webm.exists():
            print(f"wrote {args.webm.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
