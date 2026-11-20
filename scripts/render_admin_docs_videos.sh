#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Render the Admin console's two demo videos for the user docs from its
# documentation screenshots (scripts/capture_admin_docs.mjs), so a video shows
# the screens of the release the screenshots were taken from. Each screen of a
# workflow is held a few seconds, a screen taller than the frame scrolls down
# while it is shown, and screens fade into each other. No audio.
#
#   scripts/render_admin_docs_videos.sh            # both videos, both formats
#   scripts/render_admin_docs_videos.sh setup      # one of them
#
# Needs ffmpeg with libx264 and libvpx-vp9. Refresh the screenshots first.
set -euo pipefail
cd "$(dirname "$0")/.."

SHOTS=docs/assets/screenshots/admin
OUT=docs/assets/videos/admin
HOLD=3.5
FADE=0.4
WIDTH=1440
HEIGHT=900

declare -A VIDEOS=(
    [setup]="admin-guided-setup-begin admin-guided-setup-start admin-discovery-preview admin-guided-setup-config-preview admin-setup-deployment admin-setup-start"
    [upgrade]="admin-updates admin-upgrade-verifying admin-guided-upgrade-plan admin-upgrade-running admin-admin-update-reconnect admin-upgrade-completed"
)
declare -A NAMES=([setup]=admin-guided-setup-demo [upgrade]=admin-guided-upgrade-demo)

command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg is required" >&2; exit 1; }
work=$(mktemp -d "${TMPDIR:-/tmp}/admin-docs-video.XXXXXX")
trap 'rm -r -- "$work"' EXIT

clip() {
    local png=$1 out=$2 height crop
    height=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$png")
    crop="crop=${WIDTH}:${HEIGHT}:0:0"
    if [ "$height" -gt "$HEIGHT" ]; then
        crop="crop=${WIDTH}:${HEIGHT}:0:'min(ih-${HEIGHT},max(0,(ih-${HEIGHT})*(t-0.8)/(${HOLD}-1.6)))'"
    fi
    ffmpeg -loglevel error -y -loop 1 -framerate 25 -t "$HOLD" -i "$png" \
        -vf "${crop},scale=1280:800:flags=lanczos,fade=t=in:st=0:d=${FADE},fade=t=out:st=$(echo "$HOLD - $FADE" | bc):d=${FADE},format=yuv420p" \
        -c:v libx264 -crf 10 -preset veryfast "$out"
}

render() {
    local key=$1 name=${NAMES[$1]} index=0 list="$work/$1.txt"
    : > "$list"
    for shot in ${VIDEOS[$key]}; do
        [ -f "$SHOTS/$shot.png" ] || { echo "missing screenshot: $SHOTS/$shot.png" >&2; exit 1; }
        clip "$SHOTS/$shot.png" "$work/$key-$index.mp4"
        printf "file '%s'\n" "$work/$key-$index.mp4" >> "$list"
        index=$((index + 1))
    done
    ffmpeg -loglevel error -y -f concat -safe 0 -i "$list" -an \
        -c:v libx264 -pix_fmt yuv420p -crf 26 -preset slow -movflags +faststart "$OUT/$name.mp4"
    ffmpeg -loglevel error -y -i "$OUT/$name.mp4" -an \
        -c:v libvpx-vp9 -b:v 0 -crf 40 -row-mt 1 "$OUT/$name.webm"
    echo "wrote $OUT/$name.mp4 and $OUT/$name.webm"
}

wanted=("$@")
[ ${#wanted[@]} -gt 0 ] || wanted=(setup upgrade)
for key in "${wanted[@]}"; do
    [ -n "${NAMES[$key]:-}" ] || { echo "usage: $0 [setup] [upgrade]" >&2; exit 2; }
    render "$key"
done
