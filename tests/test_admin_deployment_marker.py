# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from pathlib import Path

from admin.deployment import DeploymentService


def test_deployment_marker_images_are_not_duplicated(tmp_path):
    service = DeploymentService(None, None, workspace_dir=tmp_path)
    images = [
        {"service": "ems", "image": "ems:v1"},
        {"service": "influxdb", "image": "influxdb:2.7"},
    ]

    marker = service._write_marker({"tag": "v1"}, {"sha256": "abc"}, images, 1000, 1000)

    expected = [
        {"service": "ems", "image": "ems:v1"},
        {"service": "influxdb", "image": "influxdb:2.7"},
    ]
    assert marker["images"] == expected
    written = json.loads(Path(service.marker_path).read_text(encoding="utf-8"))
    assert written["images"] == expected
