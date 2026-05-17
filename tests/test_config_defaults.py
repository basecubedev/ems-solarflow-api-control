import json
from pathlib import Path

from ems.config import OUTPUT_CONTROL_DEFAULTS, WINTER_DEFAULTS


def without_comment_keys(values):
    return {
        key: value
        for key, value in values.items()
        if not key.startswith("_comment")
    }


def test_config_template_output_control_matches_code_defaults():
    template = json.loads(Path("config.template.json").read_text())

    assert (
        without_comment_keys(template["system"]["output_control"])
        == OUTPUT_CONTROL_DEFAULTS
    )


def test_config_template_winter_matches_code_defaults():
    template = json.loads(Path("config.template.json").read_text())

    assert without_comment_keys(template["winter"]) == WINTER_DEFAULTS
