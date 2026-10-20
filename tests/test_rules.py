from pathlib import Path

import pytest

from rules import Rules, load_rules

REPO_RULES = Path(__file__).resolve().parent.parent / "parts_rules.yaml"


def write_rules(tmp_path, **overrides):
    values = {
        "brand_prefer": "[Bosch]",
        "brand_forbid": "[ProMeister]",
        "ceiling_sek": "10000",
        "mode": "approve",
    }
    values.update(overrides)
    path = tmp_path / "rules.yaml"
    path.write_text("\n".join(f"{k}: {v}" for k, v in values.items()) + "\n")
    return path


def test_defaults_from_repo_file():
    rules = load_rules(REPO_RULES)
    assert rules == Rules(
        brand_prefer=("Bosch",),
        brand_forbid=("ProMeister",),
        ceiling_sek=10000,
        mode="approve",
    )


def test_unknown_mode_rejected(tmp_path):
    with pytest.raises(ValueError, match="mode"):
        load_rules(write_rules(tmp_path, mode="yolo"))


def test_zero_ceiling_rejected(tmp_path):
    with pytest.raises(ValueError, match="ceiling_sek"):
        load_rules(write_rules(tmp_path, ceiling_sek="0"))


def test_missing_key_rejected_by_name(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("brand_prefer: [Bosch]\nbrand_forbid: []\nmode: approve\n")
    with pytest.raises(ValueError, match="ceiling_sek"):
        load_rules(path)


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_rules(tmp_path / "nope.yaml")
