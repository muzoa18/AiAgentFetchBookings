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


def test_shipped_file_has_new_defaults():
    rules = load_rules(REPO_RULES)
    assert rules.brand_prefer_by_part == ()
    assert rules.prefer_in_stock is True
    assert rules.expensive_ratio == 1.5
    assert rules.expensive_min_diff_sek == 100
    assert rules.max_alternatives == 1
    assert rules.mode == "approve"


def test_old_four_keys_get_defaults(tmp_path):
    rules = load_rules(write_rules(tmp_path))
    assert rules == Rules(("Bosch",), ("ProMeister",), 10000, "approve",
                          (), True, 1.5, 100, 1)


def test_all_new_keys_load(tmp_path):
    path = write_rules(
        tmp_path,
        brand_prefer_by_part="{Oljefilter: [Mann, Bosch], Bromsskiva: [Brembo]}",
        prefer_in_stock="false",
        expensive_ratio="2",
        expensive_min_diff_sek="0",
        max_alternatives="3",
    )
    rules = load_rules(path)
    assert rules.brand_prefer_by_part == (
        ("Oljefilter", ("Mann", "Bosch")), ("Bromsskiva", ("Brembo",)))
    assert rules.prefer_in_stock is False
    assert rules.expensive_ratio == 2
    assert rules.expensive_min_diff_sek == 0
    assert rules.max_alternatives == 3


def test_max_alternatives_zero_allowed(tmp_path):
    assert load_rules(write_rules(tmp_path, max_alternatives="0")).max_alternatives == 0


@pytest.mark.parametrize("key,value", [
    ("brand_prefer_by_part", "[Mann]"),
    ("brand_prefer_by_part", "{Oljefilter: Mann}"),
    ("brand_prefer_by_part", "{Oljefilter: [Mann, 3]}"),
    ("brand_prefer_by_part", "{Oljefilter: [\"\"]}"),
    ("brand_prefer_by_part", "{\"\": [Mann]}"),
    ("brand_prefer_by_part", "{3: [Mann]}"),
    ("prefer_in_stock", "yes please"),
    ("prefer_in_stock", "1"),
    ("expensive_ratio", "1"),
    ("expensive_ratio", "0.5"),
    ("expensive_ratio", "high"),
    ("expensive_ratio", "true"),
    ("expensive_min_diff_sek", "-1"),
    ("expensive_min_diff_sek", "1.5"),
    ("expensive_min_diff_sek", "true"),
    ("max_alternatives", "4"),
    ("max_alternatives", "-1"),
    ("max_alternatives", "true"),
    ("max_alternatives", "1.0"),
])
def test_invalid_new_values_rejected_by_name(tmp_path, key, value):
    with pytest.raises(ValueError, match=key):
        load_rules(write_rules(tmp_path, **{key: value}))
