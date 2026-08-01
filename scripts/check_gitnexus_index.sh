#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel)
CONFIG=$REPO_ROOT/.gitnexusrc
META=$REPO_ROOT/.gitnexus/meta.json
TARGET=admin/static/admin.js

[ -x "$SCRIPT_DIR/gitnexus-project" ] || {
  printf '%s\n' 'GitNexus project launcher is not executable.' >&2
  exit 1
}
[ -f "$CONFIG" ] && [ -f "$META" ] || {
  printf '%s\n' 'GitNexus configuration or index metadata is missing.' >&2
  exit 1
}

NODE_BIN=${GITNEXUS_NODE:-$(command -v node)}
THRESHOLD_KB=$(
  "$NODE_BIN" -e '
    const fs = require("fs");
    const config = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
    const value = Number(config.analyze?.maxFileSize ?? config.maxFileSize);
    if (!Number.isInteger(value) || value <= 0) process.exit(1);
    process.stdout.write(String(value));
  ' "$CONFIG"
)
TARGET_BYTES=$(wc -c <"$REPO_ROOT/$TARGET")
THRESHOLD_BYTES=$((THRESHOLD_KB * 1024))
[ "$TARGET_BYTES" -le "$THRESHOLD_BYTES" ] || {
  printf '%s is %s bytes, above the configured %s-byte threshold.\n' \
    "$TARGET" "$TARGET_BYTES" "$THRESHOLD_BYTES" >&2
  exit 1
}

INDEXED_HASH=$(jq -er --arg path "$TARGET" '.fileHashes[$path]' "$META")
CURRENT_HASH=$(sha256sum "$REPO_ROOT/$TARGET" | awk '{print $1}')
[ "$INDEXED_HASH" = "$CURRENT_HASH" ] || {
  printf '%s is listed in metadata but its indexed hash is stale.\n' "$TARGET" >&2
  exit 1
}

INDEXED_COMMIT=$(jq -er '.lastCommit' "$META")
CURRENT_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD)
[ "$INDEXED_COMMIT" = "$CURRENT_COMMIT" ] || {
  printf 'GitNexus index commit %s does not match HEAD %s.\n' \
    "$INDEXED_COMMIT" "$CURRENT_COMMIT" >&2
  exit 1
}

VERSION=$(
  GITNEXUS_EXECUTABLE=${GITNEXUS_EXECUTABLE:-} \
    "$SCRIPT_DIR/gitnexus-project" --version
)
printf 'GitNexus %s: %s indexed at %s bytes with threshold %sKB; index matches HEAD.\n' \
  "$VERSION" "$TARGET" "$TARGET_BYTES" "$THRESHOLD_KB"
