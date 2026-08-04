# SPDX-License-Identifier: AGPL-3.0-or-later
import logging
import threading

import pytest

from ems.log_buffer import RingBufferLogHandler

pytestmark = [
    pytest.mark.integration,
]


def make_logger(handler, name):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers = [handler]
    logger.propagate = False
    return logger


def test_handler_captures_records_with_increasing_seq():
    handler = RingBufferLogHandler(capacity=10)
    logger = make_logger(handler, "ring.basic")
    logger.info("first")
    logger.warning("second")

    result = handler.get_lines()
    seqs = [line["seq"] for line in result["lines"]]
    assert seqs == [1, 2]
    assert [line["message"] for line in result["lines"]] == ["first", "second"]
    assert result["lines"][1]["level"] == "WARNING"
    assert result["cursor"] == 2


def test_buffer_is_bounded_and_drops_oldest():
    handler = RingBufferLogHandler(capacity=3)
    logger = make_logger(handler, "ring.bound")
    for i in range(5):
        logger.info("line%d", i)

    result = handler.get_lines()
    seqs = [line["seq"] for line in result["lines"]]
    assert seqs == [3, 4, 5]  # oldest two evicted by maxlen


def test_after_cursor_returns_only_newer_and_flags_dropped():
    handler = RingBufferLogHandler(capacity=3)
    logger = make_logger(handler, "ring.cursor")
    for i in range(5):
        logger.info("line%d", i)

    # buffer holds seq 3,4,5; asking for after=1 means we missed seq 2 -> dropped
    result = handler.get_lines(after=1)
    assert result["dropped"] is True
    assert [line["seq"] for line in result["lines"]] == [3, 4, 5]

    # asking for the latest seq yields nothing new and is not "dropped"
    result = handler.get_lines(after=5)
    assert result["lines"] == []
    assert result["dropped"] is False
    assert result["cursor"] == 5


def test_initial_fetch_with_limit_returns_newest_lines():
    handler = RingBufferLogHandler(capacity=50)
    logger = make_logger(handler, "ring.limit")
    for i in range(5):
        logger.info("line%d", i)

    first = handler.get_lines(after=0, limit=2)
    assert [line["seq"] for line in first["lines"]] == [4, 5]
    assert first["cursor"] == 5


def test_incremental_fetch_after_cursor_returns_only_newer():
    handler = RingBufferLogHandler(capacity=50)
    logger = make_logger(handler, "ring.incremental")
    for i in range(5):
        logger.info("line%d", i)

    first = handler.get_lines(after=0, limit=2)
    logger.info("line5")

    second = handler.get_lines(after=first["cursor"], limit=2)
    assert [line["seq"] for line in second["lines"]] == [6]
    assert second["cursor"] == 6


def test_incremental_limit_truncates_without_skips():
    handler = RingBufferLogHandler(capacity=50)
    logger = make_logger(handler, "ring.limit.incremental")
    for i in range(5):
        logger.info("line%d", i)

    result = handler.get_lines(after=2, limit=2)
    assert [line["seq"] for line in result["lines"]] == [3, 4]
    assert result["cursor"] == 4


def test_level_filtering():
    handler = RingBufferLogHandler(capacity=10)
    logger = make_logger(handler, "ring.level")
    logger.debug("d")
    logger.info("i")
    logger.warning("w")
    logger.error("e")

    result = handler.get_lines(min_levelno=logging.WARNING)
    assert [line["message"] for line in result["lines"]] == ["w", "e"]


def test_control_chars_and_newlines_are_stripped():
    handler = RingBufferLogHandler(capacity=10)
    logger = make_logger(handler, "ring.sanitize")
    logger.info("line1\nline2\r\tcol\x00end")

    message = handler.get_lines()["lines"][0]["message"]
    assert "\n" not in message
    assert "\r" not in message
    assert "\x00" not in message
    assert "\t" not in message
    assert message == "line1 line2  col end"


def test_dashboard_access_logger_is_excluded():
    handler = RingBufferLogHandler(capacity=10)
    logger = make_logger(handler, "ring.exclude")
    logger.info("dashboard_http GET /api/logs")
    logger.info("real event")

    messages = [line["message"] for line in handler.get_lines()["lines"]]
    assert messages == ["real event"]


def test_concurrent_emit_is_thread_safe():
    handler = RingBufferLogHandler(capacity=10000)
    logger = make_logger(handler, "ring.threads")

    def worker():
        for _ in range(200):
            logger.info("x")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = handler.get_lines(limit=None)["lines"]
    assert len(lines) == 8 * 200
    seqs = [line["seq"] for line in lines]
    assert len(set(seqs)) == len(seqs)  # no duplicate seq under concurrency
    assert seqs == sorted(seqs)
