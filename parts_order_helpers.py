"""
parts_order_helpers.py
----------------------
Pure helpers for parts_order.py: quantity parsing, cart checks and message text.
No I/O. Every check returns a short Swedish problem string, or None when OK.
"""

import math
from collections import Counter

from parts_matcher import has_non_latin, is_fluid, is_forbidden
from parts_portal_parse import PartsPortalError
from rules import Rules

PREPARE_HEADER = "Förbered (finns på lager):"

TECHNICAL = "tekniskt fel (se loggen)"
# PartsPortalError.code -> short Swedish text for the owner. The raw exception text
# (page text, English internals) only ever goes to the log.
OWNER_REASONS = {
    "not_linked": "delen finns inte bland delarna som passar bilen",
    "in_cart": "delen låg redan i varukorgen",
    "cart_update": "varukorgen uppdaterades inte",
    "vehicle_lost": "fordonet försvann från sidan",
    "login": "inloggningen på pro.meca.se misslyckades",
}
# Fallback only, for a PartsPortalError raised without a code.
_FALLBACK_CODES = (
    (("not found among linked parts", "not on the current page"), "not_linked"),
    (("already in the cart",), "in_cart"),
    (("no cart update seen", "did not reach", "cart update for", "quantity of"), "cart_update"),
    (("no longer loaded",), "vehicle_lost"),
    (("login", "not logged in"), "login"),
)


def error_code(exc: BaseException) -> str | None:
    """The PartsPortalError code (or, for one without a code, a guess from its text)."""
    if not isinstance(exc, PartsPortalError):
        return None
    code = getattr(exc, "code", None)
    if code:
        return code
    text = str(exc)
    for fragments, guess in _FALLBACK_CODES:
        if any(f in text for f in fragments):
            return guess
    return None


def owner_reason(exc: BaseException) -> str:
    """A short Swedish summary of a failure, safe to show the owner."""
    return OWNER_REASONS.get(error_code(exc), TECHNICAL)


def parse_qty(raw) -> int | None:
    """'x4' -> 4, ''/None -> 1; non-numeric or <= 0 -> None (caller blocks)."""
    if raw is None:
        return 1
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    text = str(raw).strip()
    if text[:1] in ("x", "X"):
        text = text[1:].strip()
    if not text:
        return 1
    if not text.isdigit():
        return None
    qty = int(text)
    return qty if qty > 0 else None


def as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def norm_title(title) -> str:
    return " ".join(str(title or "").casefold().split())


def clean_fluids(fluids: list[dict]) -> list[dict]:
    """Fluids as {name, qty} only, so nothing else from the booking is persisted."""
    return [{"name": str(f.get("name", "")), "qty": str(f.get("qty") or "")} for f in fluids]


def total_problem(total, ceiling: int) -> str | None:
    if isinstance(total, bool) or not isinstance(total, (int, float)) \
            or not math.isfinite(total) or total < 0:
        return "varukorgens totalsumma saknas eller kan inte läsas"
    if total > ceiling:
        return f"totalen {total:.2f} SEK överstiger taket {ceiling} SEK"
    return None


def cart_problem(lines: list[dict], expected: list[dict], rules: Rules) -> str | None:
    """Cart lines must be exactly the expected (chosen) lines, and never a forbidden brand,
    a fluid or a title/brand with non-Latin letters (even if such a line was expected)."""
    for line in lines:
        title, brand = line.get("title"), line.get("brand")
        if is_forbidden({"title": title, "brand": brand}, rules):
            return f"förbjudet märke i varukorgen: {title}"
        if is_fluid(title or "") or is_fluid(brand or ""):
            return f"olja/vätska i varukorgen (beställs aldrig automatiskt): {title}"
        if has_non_latin(title) or has_non_latin(brand):
            return f"okända tecken (ej latinska) i varukorgen: {title}"
    if len(lines) != len(expected):
        return f"varukorgen har {len(lines)} rader men {len(expected)} delar valdes"
    use_ids = all(x.get("url_or_id") for x in lines) and all(x.get("url_or_id") for x in expected)

    def key(x):
        ident = str(x.get("url_or_id")) if use_ids else norm_title(x.get("title"))
        return ident, as_int(x.get("qty"))

    got, want = Counter(map(key, lines)), Counter(map(key, expected))
    if got == want:
        return None
    missing = "; ".join(f"{k[0]} x{k[1]}" for k in (want - got))
    extra = "; ".join(f"{k[0]} x{k[1]}" for k in (got - want))
    return f"varukorgen stämmer inte med de valda delarna (saknas: {missing}; oväntat: {extra})"


def sek(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.2f} SEK"
    return "pris saknas"


def fluid_text(line: dict) -> str:
    name = str(line.get("name", "")).strip()
    qty = str(line.get("qty") or "").strip().lstrip("xX").strip()
    return f"{name} x{qty}" if qty else name


def prepare_section(fluids: list[dict]) -> list[str]:
    if not fluids:
        return []
    return [PREPARE_HEADER] + [f"- {fluid_text(f)}" for f in fluids]


def order_body(parts: list[dict], total, fluids: list[dict]) -> list[str]:
    """Per chosen part: the booking's line name -> title (brand) qty unit price, plus a stock
    note when the catalogue says 0 in stock; then the cart total and fluids to prepare."""
    lines = ["Delar:"]
    for p in parts:
        name = str(p.get("name") or "").strip()
        head = f"{name} -> {p.get('title')}" if name else str(p.get("title"))
        stock = " – OBS: 0 i lager" if p.get("in_stock") is False else ""
        lines.append(f"- {head} ({p.get('brand') or '–'}) x{p.get('qty')} "
                     f"à {sek(p.get('price_sek'))}{stock}")
    lines.append(f"Totalt: {sek(total)}")
    return lines + prepare_section(fluids)


def build_summary(booking: dict, cart: dict, fluids: list[dict], parts: list[dict]) -> str:
    """Approval text: booking header, chosen parts, cart total, fluids. Plain text."""
    reg = str(booking.get("reg_nr") or "–").strip()
    label = booking.get("booking_nr") or booking.get("id") or "okänd bokning"
    return "\n".join([
        f"Delbeställning {label}",
        f"Kund: {booking.get('driver_name') or '–'}",
        f"Fordon: {reg} {booking.get('vehicle') or ''}".rstrip(),
    ] + order_body(parts, cart.get("total_sek"), fluids))
