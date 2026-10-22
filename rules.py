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
    brand_prefer_by_part: tuple[tuple[str, tuple[str, ...]], ...] = ()
    prefer_in_stock: bool = True
    expensive_ratio: float = 1.5
    expensive_min_diff_sek: int = 100
    max_alternatives: int = 1


def _brands(data: dict, key: str) -> tuple[str, ...]:
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(b, str) for b in value):
        raise ValueError(f"{key} must be a list of brand names")
    return tuple(value)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _by_part(value) -> tuple[tuple[str, tuple[str, ...]], ...]:
    key = "brand_prefer_by_part"
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a mapping of part word -> list of brand names")
    out = []
    for part, brands in value.items():
        if not isinstance(part, str) or not part.strip():
            raise ValueError(f"{key}: part names must be non-empty strings, got {part!r}")
        if (not isinstance(brands, list)
                or not all(isinstance(b, str) and b.strip() for b in brands)):
            raise ValueError(f"{key}: '{part}' must be a list of non-empty brand names")
        out.append((part, tuple(brands)))
    return tuple(out)


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
    by_part = _by_part(data["brand_prefer_by_part"]) if "brand_prefer_by_part" in data else ()
    prefer_in_stock = data.get("prefer_in_stock", True)
    if not isinstance(prefer_in_stock, bool):
        raise ValueError(f"prefer_in_stock must be true or false, got {prefer_in_stock!r}")
    ratio = data.get("expensive_ratio", 1.5)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not ratio > 1:
        raise ValueError(f"expensive_ratio must be a number greater than 1, got {ratio!r}")
    min_diff = data.get("expensive_min_diff_sek", 100)
    if not _is_int(min_diff) or min_diff < 0:
        raise ValueError(
            f"expensive_min_diff_sek must be an integer >= 0, got {min_diff!r}")
    max_alt = data.get("max_alternatives", 1)
    if not _is_int(max_alt) or not 0 <= max_alt <= 3:
        raise ValueError(f"max_alternatives must be an integer 0-3, got {max_alt!r}")
    return Rules(_brands(data, "brand_prefer"), _brands(data, "brand_forbid"), ceiling, mode,
                 by_part, prefer_in_stock, ratio, min_diff, max_alt)
