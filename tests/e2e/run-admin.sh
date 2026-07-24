#!/usr/bin/env bash
# Launch the deterministic browser-test Admin server on an isolated state root.
# Never touches the developer's real config/ or data/.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
export EMS_ADMIN_TEST_MODE=1
export EMS_ADMIN_DATA_DIR="$TMP/admin-data"
export EMS_INSTALL_DIR="$TMP/install"
mkdir -p "$EMS_ADMIN_DATA_DIR" "$EMS_INSTALL_DIR"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
exec "$PY" -m admin --host 127.0.0.1 --port "${EMS_ADMIN_E2E_PORT:-8123}"
