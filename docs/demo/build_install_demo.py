# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build the Docker-first Analytics bootstrap demo GIF/WebM."""

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
OUTPUT_MP4 = ASSETS / "install-demo.mp4"

WIDTH = 900
HEIGHT = 506
CONTENT_X = 32
CONTENT_TOP = 24
CONTENT_WIDTH = WIDTH - (CONTENT_X * 2)
HEADER_HEIGHT = 72
BODY_TOP = CONTENT_TOP + HEADER_HEIGHT
BODY_BOTTOM = HEIGHT - 28
DASHBOARD_HEIGHT = BODY_BOTTOM - BODY_TOP
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


FONT_TITLE = load_font(23, bold=True)
FONT_SMALL = load_font(15)
FONT_MONO = load_font(15)
FONT_MONO_BOLD = load_font(15, bold=True)


def draw_panel(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], fill: str, outline: str) -> None:
    draw.rounded_rectangle(xy, radius=8, fill=fill, outline=outline, width=1)


def base_frame(title: str, subtitle: str) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), "#0b111b")
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (CONTENT_X, CONTENT_TOP, CONTENT_X + CONTENT_WIDTH, BODY_TOP + 8),
        radius=10,
        fill="#172033",
    )
    draw.rectangle(
        (CONTENT_X, BODY_TOP - 8, CONTENT_X + CONTENT_WIDTH, BODY_TOP + 8),
        fill="#172033",
    )
    draw.text((CONTENT_X + 22, CONTENT_TOP + 14), title, font=FONT_TITLE, fill="#f8fafc")
    draw.text((CONTENT_X + 22, CONTENT_TOP + 43), subtitle, font=FONT_SMALL, fill="#b8c2d2")
    return img


def terminal_frame(title: str, subtitle: str, lines: list[tuple[str, str]]) -> Image.Image:
    img = base_frame(title, subtitle)
    draw = ImageDraw.Draw(img)
    draw_panel(draw, (CONTENT_X, BODY_TOP, CONTENT_X + CONTENT_WIDTH, BODY_BOTTOM), "#101620", "#293548")
    draw.rectangle((CONTENT_X, BODY_TOP, CONTENT_X + CONTENT_WIDTH, BODY_TOP + 30), fill="#202b3b")
    for index, color in enumerate(("#ef4444", "#f59e0b", "#22c55e")):
        draw.ellipse(
            (
                CONTENT_X + 18 + index * 22,
                BODY_TOP + 10,
                CONTENT_X + 30 + index * 22,
                BODY_TOP + 22,
            ),
            fill=color,
        )
    draw.text(
        (CONTENT_X + 100, BODY_TOP + 9),
        "demo shell",
        font=FONT_SMALL,
        fill="#d1d5db",
    )
    draw.rounded_rectangle(
        (CONTENT_X + CONTENT_WIDTH - 322, BODY_TOP + 6, CONTENT_X + CONTENT_WIDTH - 196, BODY_TOP + 24),
        radius=4,
        fill="#102a4d",
    )
    draw.text((CONTENT_X + CONTENT_WIDTH - 314, BODY_TOP + 8), "YOU RUN", font=FONT_SMALL, fill="#bfdbfe")
    draw.rounded_rectangle(
        (CONTENT_X + CONTENT_WIDTH - 184, BODY_TOP + 6, CONTENT_X + CONTENT_WIDTH - 16, BODY_TOP + 24),
        radius=4,
        fill="#123421",
    )
    draw.text((CONTENT_X + CONTENT_WIDTH - 176, BODY_TOP + 8), "SCRIPT OUTPUT", font=FONT_SMALL, fill="#bbf7d0")

    y = BODY_TOP + 50
    colors = {
        "cmd": "#93c5fd",
        "out": "#d1d5db",
        "ok": "#86efac",
        "warn": "#facc15",
        "dim": "#94a3b8",
        "ask": "#94a3b8",
        "answer": "#fbbf24",
    }
    for line in lines:
        kind, text = line[0], line[1]
        answer = line[2] if len(line) > 2 else None
        font = FONT_MONO_BOLD if kind == "cmd" else FONT_MONO
        draw.text((CONTENT_X + 20, y), text, font=font, fill=colors[kind])
        if answer is not None:
            prompt_width = draw.textlength(text, font=font)
            draw.text(
                (CONTENT_X + 20 + prompt_width, y),
                answer,
                font=FONT_MONO_BOLD,
                fill=colors["answer"],
            )
        y += 24
    return img


def dashboard_frame(view: str, title: str, subtitle: str) -> Image.Image:
    img = base_frame(title, subtitle)
    draw = ImageDraw.Draw(img)
    preview = Image.open(ASSETS / f"preview-{view}.jpg").convert("RGB")

    crop_ratio = CONTENT_WIDTH / DASHBOARD_HEIGHT
    source_ratio = preview.width / preview.height
    if source_ratio > crop_ratio:
        new_width = int(preview.height * crop_ratio)
        left = max((preview.width - new_width) // 2, 0)
        preview = preview.crop((left, 0, left + new_width, preview.height))
    else:
        new_height = int(preview.width / crop_ratio)
        preview = preview.crop((0, 0, preview.width, min(new_height, preview.height)))

    preview = preview.resize((CONTENT_WIDTH, DASHBOARD_HEIGHT), Image.Resampling.LANCZOS)
    draw_panel(draw, (CONTENT_X, BODY_TOP, CONTENT_X + CONTENT_WIDTH, BODY_BOTTOM), "#101620", "#293548")
    img.paste(preview, (CONTENT_X, BODY_TOP))
    draw.rounded_rectangle(
        (
            CONTENT_X + CONTENT_WIDTH - 248,
            BODY_BOTTOM - 46,
            CONTENT_X + CONTENT_WIDTH - 12,
            BODY_BOTTOM - 16,
        ),
        radius=6,
        fill="#172033",
    )
    draw.text(
        (CONTENT_X + CONTENT_WIDTH - 232, BODY_BOTTOM - 38),
        "http://localhost:8080",
        font=FONT_SMALL,
        fill="#f8fafc",
    )
    return img


def build_frames() -> list[tuple[Image.Image, int]]:
    prompt = "$"
    return [
        (
            terminal_frame(
                "Minimal Analytics bootstrap",
                "These are the only shell commands for a fresh install.",
                [
                    ("cmd", f"{prompt} mkdir ems-demo && cd ems-demo"),
                    ("cmd", f"{prompt} curl -fsSLo install-docker.sh https://raw.githubusercontent.com/.../install-docker.sh"),
                    ("cmd", f"{prompt} sh install-docker.sh --analytics"),
                ],
            ),
            5200,
        ),
        (
            terminal_frame(
                "Installer bootstrap output",
                "Everything below is done by the installer script.",
                [
                    ("ok", "created config/, data/, data/influxdb/"),
                    ("ok", "wrote docker-compose.yml"),
                    ("ok", "generated config/influxdb.env"),
                    ("ok", "config init --analytics --yes --no-backup"),
                    ("ok", "influx init --no-start"),
                    ("ok", "enabled COMPOSE_PROFILES=with-analytics"),
                    ("ok", "docker compose up -d"),
                    ("ok", "Container ems       Started"),
                    ("ok", "Container influxdb  Started"),
                    ("out", "dashboard: http://localhost:8080"),
                    ("out", "analytics: http://localhost:8086"),
                ],
            ),
            6800,
        ),
        (
            terminal_frame(
                "Add your meter and devices",
                "Guided setup with example values; most prompts accept the default.",
                [
                    ("cmd", f"{prompt} docker compose exec ems python3 emsctl.py config init"),
                    ("dim", "Which grid meter do you use?"),
                    ("dim", "  1) Shelly   2) EcoTracker   3) Tasmota"),
                    ("ask", "Choice [1]: ", "1"),
                    ("ask", "Grid meter IP address: ", "192.0.2.20"),
                    ("ask", "How many Zendure inverters? [1]: ", "1"),
                    ("ask", "Device 1 IP: ", "192.0.2.10"),
                    ("ask", "Device 1 serial number: ", "DEMO-SF800P2-1"),
                    ("ask", "Device 1 max output power [800]: ", "800"),
                    ("ask", "Battery size in kWh [1.92]: ", "1.92"),
                    ("ask", "Minimum SOC [15]: ", "10"),
                    ("ok", "wrote config/config.json"),
                ],
            ),
            8000,
        ),
        (
            dashboard_frame(
                "aggregated",
                "Open the dashboard",
                "The installer prints the URL; open http://localhost:8080.",
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


def _encode_video(
    frames: list[tuple[Image.Image, int]],
    output: Path,
    codec_args: list[str],
    label: str,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print(f"ffmpeg not found; skipped {label} generation")
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
                *codec_args,
                str(output),
            ],
            check=True,
        )


def write_webm(frames: list[tuple[Image.Image, int]], output: Path) -> None:
    _encode_video(
        frames,
        output,
        ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "38"],
        "WebM",
    )


def write_mp4(frames: list[tuple[Image.Image, int]], output: Path) -> None:
    # H.264 / yuv420p with +faststart embeds inline on most forums (Discourse,
    # phpBB) where WebM playback is unreliable.
    _encode_video(
        frames,
        output,
        ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26", "-movflags", "+faststart"],
        "MP4",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gif", default=OUTPUT_GIF, type=Path)
    parser.add_argument("--webm", default=OUTPUT_WEBM, type=Path)
    parser.add_argument("--mp4", default=OUTPUT_MP4, type=Path)
    parser.add_argument("--skip-webm", action="store_true")
    parser.add_argument("--skip-mp4", action="store_true")
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
    if not args.skip_mp4:
        write_mp4(frames, args.mp4)
        if args.mp4.exists():
            print(f"wrote {args.mp4.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
