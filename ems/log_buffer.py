# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-memory ring buffer for the dashboard log viewer.

The stock service logs to stderr only (no guaranteed log file), so the dashboard
reads recent log lines from a process-wide ring buffer that the entry script
attaches to the root logger. The buffer is count-bounded (a deque with maxlen),
so memory can never grow without limit; at default settings it holds well over
15 minutes of INFO output for a typical multi-device system.
"""

import logging
import re
import threading
from collections import deque


DEFAULT_CAPACITY = 5000

# Lines emitted by the dashboard's own HTTP access logging start with this
# prefix; excluding them prevents a feedback loop (the act of viewing logs would
# otherwise generate more log lines that fill the buffer).
EXCLUDED_MESSAGE_PREFIXES = ("dashboard_http",)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

_BUFFER = None
_BUFFER_LOCK = threading.Lock()


def _sanitize(message):
    # Replace CR/LF and other control characters with spaces so a crafted log
    # line cannot inject extra rows or break the response stream.
    return _CONTROL_CHARS.sub(" ", message)


class RingBufferLogHandler(logging.Handler):
    def __init__(self, capacity=DEFAULT_CAPACITY, excluded_message_prefixes=EXCLUDED_MESSAGE_PREFIXES):
        super().__init__()
        self.capacity = max(1, int(capacity))
        self._buffer = deque(maxlen=self.capacity)
        self._seq = 0
        self._lock = threading.Lock()
        self._excluded = tuple(excluded_message_prefixes or ())

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive, mirrors logging.Handler
            return

        if any(message.startswith(prefix) for prefix in self._excluded):
            return

        entry = {
            "ts": record.created,
            "levelno": record.levelno,
            "level": record.levelname,
            "logger": record.name,
            "message": _sanitize(message),
        }
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._buffer.append(entry)

    def get_lines(self, after=None, limit=None, min_levelno=None):
        """Return log lines newer than ``after`` (oldest first).

        ``dropped`` is True when ``after`` points before the oldest retained
        line, i.e. the buffer rolled over and the caller missed some lines.
        ``cursor`` is the seq the caller should pass as ``after`` next time.
        """
        with self._lock:
            entries = list(self._buffer)
            max_seq = self._seq

        oldest_seq = entries[0]["seq"] if entries else None
        dropped = bool(
            after is not None
            and oldest_seq is not None
            and after + 1 < oldest_seq
        )

        selected = []
        for entry in entries:
            if after is not None and entry["seq"] <= after:
                continue
            if min_levelno is not None and entry["levelno"] < min_levelno:
                continue
            selected.append(entry)

        truncated = False
        if limit is not None and limit >= 0 and len(selected) > limit:
            selected = selected[:limit]
            truncated = True

        if selected:
            cursor = selected[-1]["seq"]
        elif after is not None and not truncated:
            cursor = max(after, max_seq)
        else:
            cursor = max_seq

        lines = [
            {
                "seq": entry["seq"],
                "ts": entry["ts"],
                "level": entry["level"],
                "logger": entry["logger"],
                "message": entry["message"],
            }
            for entry in selected
        ]
        return {"lines": lines, "cursor": cursor, "dropped": dropped}


def get_log_buffer(capacity=DEFAULT_CAPACITY):
    """Return the process-wide ring buffer handler, creating it once."""
    global _BUFFER
    with _BUFFER_LOCK:
        if _BUFFER is None:
            _BUFFER = RingBufferLogHandler(capacity=capacity)
        return _BUFFER


def install_log_buffer(capacity=DEFAULT_CAPACITY, level=logging.NOTSET):
    """Create the singleton buffer and attach it to the root logger."""
    handler = get_log_buffer(capacity=capacity)
    handler.setLevel(level)
    root = logging.getLogger()
    if handler not in root.handlers:
        root.addHandler(handler)
    return handler


def reset_log_buffer_for_tests():
    """Drop the singleton so tests start from a clean buffer."""
    global _BUFFER
    with _BUFFER_LOCK:
        if _BUFFER is not None:
            logging.getLogger().removeHandler(_BUFFER)
        _BUFFER = None
