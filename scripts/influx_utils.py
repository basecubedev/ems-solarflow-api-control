import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests


def load_env_file(path):
    values = {}

    with open(path) as handle:
        for raw_line in handle:
            line = raw_line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()

    return values


def normalize_negative_option_args(argv, option_names):
    normalized = [argv[0]]
    index = 1
    option_names = set(option_names)

    while index < len(argv):
        current = argv[index]

        if (
            current in option_names
            and index + 1 < len(argv)
            and argv[index + 1].startswith("-")
            and not argv[index + 1].startswith("--")
        ):
            normalized.append(f"{current}={argv[index + 1]}")
            index += 2
            continue

        normalized.append(current)
        index += 1

    return normalized


def require_env(values, *keys):
    missing = [key for key in keys if not values.get(key)]

    if missing:
        raise ValueError(
            "Missing required environment values: "
            + ", ".join(sorted(missing))
        )


def escape_measurement(value):
    return str(value).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,")


def escape_tag(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(" ", "\\ ")
        .replace(",", "\\,")
        .replace("=", "\\=")
    )


def format_field_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value}i"

    if isinstance(value, float):
        return json.dumps(round(value, 6))

    if value is None:
        return None

    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_line_protocol(measurement, tags, fields, timestamp_ns):
    clean_fields = []

    for key, value in sorted(fields.items()):
        formatted = format_field_value(value)

        if formatted is None:
            continue

        clean_fields.append(f"{escape_tag(key)}={formatted}")

    if not clean_fields:
        return None

    tag_suffix = ""

    if tags:
        clean_tags = [
            f"{escape_tag(key)}={escape_tag(value)}"
            for key, value in sorted(tags.items())
            if value not in (None, "")
        ]

        if clean_tags:
            tag_suffix = "," + ",".join(clean_tags)

    return (
        f"{escape_measurement(measurement)}"
        f"{tag_suffix} "
        f"{','.join(clean_fields)} "
        f"{int(timestamp_ns)}"
    )


class InfluxHTTPClient:
    def __init__(self, base_url, org, token, session=None, timeout=15):
        self.base_url = base_url.rstrip("/")
        self.org = org
        self.token = token
        self.timeout = timeout
        self.session = session or requests.Session()

    @property
    def headers(self):
        return {
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/csv"
        }

    def write_lines(self, bucket, lines, precision="ns"):
        payload = "\n".join(line for line in lines if line)

        if not payload:
            return

        response = self.session.post(
            f"{self.base_url}/api/v2/write",
            params={
                "org": self.org,
                "bucket": bucket,
                "precision": precision
            },
            data=payload.encode("utf-8"),
            headers={
                "Authorization": f"Token {self.token}",
                "Content-Type": "text/plain; charset=utf-8"
            },
            timeout=self.timeout
        )
        response.raise_for_status()

    def query_csv(self, flux):
        response = self.session.post(
            f"{self.base_url}/api/v2/query",
            params={"org": self.org},
            json={"query": flux},
            headers=self.headers,
            timeout=self.timeout
        )
        response.raise_for_status()
        return response.text

    def query_raw(self, flux, accept="application/csv"):
        response = self.session.post(
            f"{self.base_url}/api/v2/query",
            params={"org": self.org},
            json={"query": flux},
            headers={
                "Authorization": f"Token {self.token}",
                "Content-Type": "application/json",
                "Accept": accept
            },
            timeout=self.timeout
        )
        response.raise_for_status()
        return response.text

    def get_org_id(self):
        response = self.session.get(
            f"{self.base_url}/api/v2/orgs",
            params={"org": self.org},
            headers={
                "Authorization": f"Token {self.token}",
                "Accept": "application/json"
            },
            timeout=self.timeout
        )
        response.raise_for_status()
        payload = response.json()

        for org in payload.get("orgs", []):
            if org.get("name") == self.org:
                return org.get("id")

        raise ValueError(f"InfluxDB org not found: {self.org}")

    def find_bucket(self, bucket_name):
        response = self.session.get(
            f"{self.base_url}/api/v2/buckets",
            params={"name": bucket_name},
            headers={
                "Authorization": f"Token {self.token}",
                "Accept": "application/json"
            },
            timeout=self.timeout
        )
        response.raise_for_status()
        payload = response.json()

        for bucket in payload.get("buckets", []):
            if bucket.get("name") == bucket_name:
                return bucket

        return None

    def ensure_bucket(self, bucket_name, retention_seconds=0):
        existing = self.find_bucket(bucket_name)

        if existing:
            return existing, False

        org_id = self.get_org_id()
        every_seconds = int(retention_seconds or 0)
        retention_rules = []

        if every_seconds > 0:
            retention_rules.append({"type": "expire", "everySeconds": every_seconds})

        response = self.session.post(
            f"{self.base_url}/api/v2/buckets",
            json={
                "orgID": org_id,
                "name": bucket_name,
                "retentionRules": retention_rules
            },
            headers={
                "Authorization": f"Token {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            timeout=self.timeout
        )
        response.raise_for_status()
        return response.json(), True


def parse_influx_csv(raw_text):
    rows = []
    header = None

    for line in raw_text.splitlines():
        if not line or line.startswith("#"):
            continue

        if header is None:
            header = next(csv.reader([line]))
            continue

        values = next(csv.reader([line]))

        if values == header:
            continue

        row = dict(zip(header, values))
        rows.append(row)

    return rows


def coerce_value(value):
    if value in ("", None):
        return None

    if value in ("true", "false"):
        return value == "true"

    try:
        if any(char in value for char in (".", "e", "E")):
            return float(value)

        return int(value)
    except (TypeError, ValueError):
        return value


def flux_time_literal(value):
    value = str(value).strip()

    if not value:
        raise ValueError("Time value must not be empty")

    if value == "now":
        return "now()"

    if value.startswith("-"):
        return value

    if value.endswith("Z"):
        return f"time(v: \"{value}\")"

    return f"time(v: \"{value}\")"


def parse_time_value(value, now=None):
    now = now or datetime.now(timezone.utc)
    value = str(value).strip()

    if value == "now":
        return now

    if value.startswith("-"):
        return now - parse_duration(value[1:])

    if value.endswith("Z"):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    return datetime.fromisoformat(value)


def parse_duration(value):
    units = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400
    }

    unit = value[-1]

    if unit not in units:
        raise ValueError(f"Unsupported duration: {value}")

    amount = float(value[:-1])
    return timedelta(seconds=amount * units[unit])


def ensure_parent_dir(path):
    parent = os.path.dirname(path)

    if parent:
        os.makedirs(parent, exist_ok=True)
