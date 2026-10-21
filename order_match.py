"""
order_match.py
--------------
Pure matching of a placed order to a row of pro.meca.se "Orderöversikt -> Skickade
Beställningar" (the order checker in approve.py). No I/O, stdlib only.

The booking's vehicle string is brand and model glued together ("KIARIO (III)(2014)",
"VOLKSWAGEN PASSAT"); the shop row says "KIA, Rio Kombi-sedan (III), 12-16". Both are
normalised (case, accents, every non-alphanumeric character incl. zero-width marks dropped)
and the booking's leading token (the text before the first "(") must equal the row's
brand words followed by at least one whole model word: "kiario" = "kia" + "rio". So a bare
brand ("KIA") never matches, and "KIARION" does not match "KIA, Rio".

The live table also has a (hidden) registration column. When both sides have a
registration a mismatch excludes the row; without a usable model the registration alone
can identify the row.

Never guesses: two or more candidate rows -> "ambiguous".
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

MODEL_MAX = 80


@dataclass(frozen=True)
class MatchResult:
    status: str                       # verified | ambiguous | not_found | no_model
    rows: list = field(default_factory=list)   # the candidate rows (one when verified)

    @property
    def row(self) -> dict | None:
        return self.rows[0] if self.status == "verified" else None


def _fold(text) -> str:
    """casefold + accents removed (NFKD, combining marks dropped)."""
    text = unicodedata.normalize("NFKD", str(text or "")).casefold()
    return "".join(c for c in text if not unicodedata.combining(c))


def normalise_model(text) -> str:
    """Lowercase, accents removed, every non-alphanumeric character dropped."""
    return "".join(c for c in _fold(text) if c.isalnum())


def _words(text) -> list[str]:
    return [w for w in re.split(r"[^0-9a-z]+", _fold(text)) if w]


def model_token(vehicle_model) -> str:
    """The normalised text before the first "(" of a booking's vehicle string."""
    return normalise_model(str(vehicle_model or "").split("(", 1)[0])


def clean_vehicle_model(text) -> str:
    """A booking's vehicle string made safe to store: no control/format characters,
    single spaces, at most MODEL_MAX characters; "" for anything that is not a string."""
    if not isinstance(text, str):
        return ""
    kept = "".join(" " if unicodedata.category(c) == "Cc" else c for c in text
                   if unicodedata.category(c) != "Cf")
    return " ".join(kept.split())[:MODEL_MAX]


def _model_matches(token: str, row_model: str) -> bool:
    brand, _, rest = row_model.partition(",")
    brand_words = _words(brand) if rest else _words(row_model)[:1]
    words = _words(row_model)
    joined = ""
    for i, word in enumerate(words):
        joined += word
        if i >= len(brand_words) and joined == token:
            return True
        if len(joined) >= len(token):
            break
    return False


def _norm_reg(reg) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(reg or "").upper())


def _numbers(row: dict) -> set[str]:
    return {n for n in re.split(r"[\s/,]+", str(row.get("order_numbers") or "")) if n}


def _row_date(row: dict) -> date | None:
    value = row.get("date")
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def match_sent_order(rows: list[dict], vehicle_model, order_date: date, claimed: set,
                     reg_nr=None) -> MatchResult:
    """Find the one sent-orders row that is this order.

    A candidate row is dated on or after `order_date`, has a supplier number and a car model
    ("" = not filled in yet), is not `claimed` (its supplier or portal order number is
    stored on another record), and matches the booking's model token (see module doc).
    `reg_nr`: when given, a row showing another registration is never a candidate, and
    with no usable model a row showing this registration is.
    """
    token = model_token(vehicle_model)
    reg = _norm_reg(reg_nr)
    if not token and not reg:
        return MatchResult("no_model")
    claimed = {str(c) for c in (claimed or ())}
    candidates = []
    for row in rows or []:
        day = _row_date(row)
        supplier = str(row.get("supplier_no") or "").strip()
        model = str(row.get("model") or "").strip()
        if day is None or day < order_date or not supplier or not model or model == "-":
            continue
        if supplier in claimed or _numbers(row) & claimed:
            continue
        row_reg = _norm_reg(row.get("reg_nr"))
        if reg and row_reg and row_reg != reg:
            continue
        if token:
            if not _model_matches(token, model):
                continue
        elif row_reg != reg:
            continue
        candidates.append(row)
    if not candidates:
        return MatchResult("not_found")
    if len(candidates) > 1:
        return MatchResult("ambiguous", candidates)
    return MatchResult("verified", candidates)
