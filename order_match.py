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

The live table also has a (hidden) registration column. When the record has a plate and
the table has that column, ONLY a row showing the same plate counts (model naming differs
between booking and shop, so the plate alone is enough; a row with an empty registration
never counts). The model is used only without a plate or without the column.

Several unverified records are resolved together (resolve_orders): rows are never claimed
while matching, records that share candidate rows form a group, and only a group of ONE
record with ONE row is verified. Never guesses: otherwise "ambiguous" or "waiting".
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

MODEL_MAX = 80
NUMBERS_MAX = 120   # same limit as orders_state.NUMBERS_MAX (verified_numbers)


@dataclass(frozen=True)
class MatchResult:
    status: str                       # verified | ambiguous | waiting | not_found | no_model
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


def row_date(row: dict) -> date | None:
    """A row's "Datum" as a date; None when it is missing or not YYYY-MM-DD."""
    return _row_date(row)


def numbers_text(row: dict) -> str:
    """What is stored as verified_numbers: "<supplier no> · <portal order numbers>", cut at a
    number boundary to NUMBERS_MAX characters. claim_tokens() reads the same text back, so
    a cut never leaves half a number behind."""
    text = str(row.get("supplier_no") or "").strip()
    numbers = [n for n in re.split(r"[\s/,]+", str(row.get("order_numbers") or "")) if n]
    for i in range(len(numbers), 0, -1):
        candidate = f"{text} · {'/'.join(numbers[:i])}"
        if len(candidate) <= NUMBERS_MAX:
            return candidate
    return text[:NUMBERS_MAX]


def claim_tokens(verified_numbers) -> set[str]:
    """The supplier and portal order numbers in a stored verified_numbers text."""
    return set(re.findall(r"[0-9A-Za-z-]+", str(verified_numbers or "")))


def _has_reg_column(rows) -> bool:
    return any(row.get("reg_nr") is not None for row in rows or [])


def candidates(rows: list[dict], vehicle_model, order_date: date, claimed: set,
               reg_nr=None) -> list[dict] | None:
    """The rows that could be this order (None = nothing to match by: no model, no plate).

    Every candidate is dated on or after `order_date`, has a supplier number and is not
    `claimed` (its supplier or a portal order number is stored on another record). Then:
    with a plate and a registration column, the row's registration must equal it; else the
    row must have a model matching the booking's model token (see module doc)."""
    token = model_token(vehicle_model)
    reg = _norm_reg(reg_nr)
    by_plate = bool(reg) and _has_reg_column(rows)
    if not by_plate and not token:
        return None
    claimed = {str(c) for c in (claimed or ())}
    out = []
    for row in rows or []:
        day = _row_date(row)
        supplier = str(row.get("supplier_no") or "").strip()
        if day is None or day < order_date or not supplier:
            continue
        if supplier in claimed or _numbers(row) & claimed:
            continue
        if by_plate:
            if _norm_reg(row.get("reg_nr")) != reg:
                continue
        else:
            model = str(row.get("model") or "").strip()
            if not model or model == "-" or not _model_matches(token, model):
                continue
        out.append(row)
    return out


def match_sent_order(rows: list[dict], vehicle_model, order_date: date, claimed: set,
                     reg_nr=None) -> MatchResult:
    """One record on its own: exactly one candidate -> verified, more -> ambiguous."""
    return resolve_orders({"_": (vehicle_model, order_date, reg_nr)}, rows, claimed)["_"]


def local_date(when: datetime) -> date:
    """The Swedish calendar date of `when` (the "Datum" of Skickade Beställningar). Without
    time zone data the UTC date is used: never later than the Swedish one, so no row of
    the order's own day is ever skipped."""
    try:
        from zoneinfo import ZoneInfo
        return when.astimezone(ZoneInfo("Europe/Stockholm")).date()
    except Exception:
        return when.astimezone(timezone.utc).date()


def order_facts(record: dict) -> tuple[str, str | None, datetime | None]:
    """(vehicle_model, reg_nr, placed_at) of an 'ordered' record, as the order checker reads
    them: the stored car model ("" if none), the plan's plate (None if not a string) and
    updated_at (None if missing, unreadable or without time zone)."""
    plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
    reg = plan.get("reg_nr") if isinstance(plan.get("reg_nr"), str) else None
    try:
        since = datetime.fromisoformat(record["updated_at"])
        since = since if since.tzinfo is not None else None
    except Exception:
        since = None
    return record.get("vehicle_model") or "", reg, since


def match_key(record: dict) -> tuple | None:
    """The (vehicle_model, order_date, reg_nr) resolve_orders takes for a record, or None
    when it cannot be checked (no time, or neither a model nor a plate)."""
    model, reg, since = order_facts(record)
    if since is None or not (model or reg):
        return None
    return model, local_date(since), reg


def resolve_orders(orders: dict, rows: list[dict], claimed: set) -> dict:
    """{booking_id: (vehicle_model, order_date, reg_nr)} -> {booking_id: MatchResult}.

    Candidates are computed for every record first, without claiming anything. Records
    whose candidate rows overlap form a group (rows are identified by supplier number):
      * one record, one row       -> verified
      * one record, several rows  -> ambiguous
      * several records, fewer rows than records -> waiting (not all rows shown yet)
      * several records, at least as many rows    -> ambiguous for each (cannot be paired)
    No candidates -> not_found; nothing to match by -> no_model."""
    found = {bid: candidates(rows, model, day, claimed, reg)
             for bid, (model, day, reg) in orders.items()}
    results = {bid: MatchResult("no_model") for bid, rows_ in found.items() if rows_ is None}
    results.update({bid: MatchResult("not_found") for bid, rows_ in found.items()
                    if rows_ is not None and not rows_})
    pending = [bid for bid in orders if bid not in results]
    keys = {bid: {str(r["supplier_no"]).strip() for r in found[bid]} for bid in pending}
    while pending:
        group = [pending.pop(0)]
        group_keys = set(keys[group[0]])
        grew = True
        while grew:
            grew = False
            for bid in list(pending):
                if keys[bid] & group_keys:
                    group.append(bid)
                    group_keys |= keys[bid]
                    pending.remove(bid)
                    grew = True
        if len(group) == 1:
            bid = group[0]
            status = "verified" if len(found[bid]) == 1 else "ambiguous"
            results[bid] = MatchResult(status, list(found[bid]))
        else:
            status = "waiting" if len(group_keys) < len(group) else "ambiguous"
            for bid in group:
                results[bid] = MatchResult(status, list(found[bid]))
    return results
