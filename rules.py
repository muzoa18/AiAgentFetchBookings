"""Loads the owner-editable parts-ordering rules (parts_rules.yaml). Fails closed."""
from dataclasses import dataclass
from pathlib import Path

import yaml

MODES = ("approve", "auto")


@dataclass(frozen=True)
class Rules:
    brand_prefer: tuple[str, ...]
    brand_forbid: tuple[str, ...]
    ceiling_sek: int
    mode: str


def _brands(data: dict, key: str) -> tuple[str, ...]:
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(b, str) for b in value):
        raise ValueError(f"{key} must be a list of brand names")
    return tuple(value)


def load_rules(path: Path = Path("parts_rules.yaml")) -> Rules:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping of rules")
    for key in ("brand_prefer", "brand_forbid", "ceiling_sek", "mode"):
        if key not in data:
            raise ValueError(f"{path}: missing required key '{key}'")
    ceiling = data["ceiling_sek"]
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= 0:
        raise ValueError(f"ceiling_sek must be a positive integer, got {ceiling!r}")
    mode = data["mode"]
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    return Rules(_brands(data, "brand_prefer"), _brands(data, "brand_forbid"), ceiling, mode)
