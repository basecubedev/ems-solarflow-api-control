import logging


def setup_logging(log_level):
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s"
    )


def log_event(level, event, **fields):
    """Write simple structured key=value log lines."""

    parts = [f"event={event}"]

    for key in sorted(fields):
        value = fields[key]
        parts.append(f"{key}={value}")

    logging.log(level, " ".join(parts))

