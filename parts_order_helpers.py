"""
parts_order_helpers.py
----------------------
Pure helpers for parts_order.py: quantity parsing, cart checks and message text.
No I/O. Every check returns a short Swedish problem string, or None when OK.
"""

import math
import unicodedata
from collections import Counter

from parts_matcher import has_non_latin, is_fluid, is_forbidden
from parts_portal_parse import PartsPortalError, parse_sek
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
    "search_timeout": "sökresultatet visades inte i tid",
    "vehicle_unknown": "pro.meca.se känner inte igen registreringsnumret",
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


# Portal error codes that are worth an automatic retry while the cart is untouched.
TRANSIENT_CODES = ("vehicle_lost", "login", "cart_update", "search_timeout", None)


def is_transient(exc: BaseException) -> bool:
    """True for a pro.meca.se hiccup (slow page, lost session, timeout, network) rather than
    a real problem: an unclassified PartsPortalError, the codes in TRANSIENT_CODES, any
    Playwright error (timeouts, net::ERR_...), TimeoutError and ConnectionError."""
    if isinstance(exc, PartsPortalError):
        return getattr(exc, "code", None) in TRANSIENT_CODES
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return type(exc).__module__.split(".")[0] == "playwright"


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


def order_body(parts: list[dict], total, fluids: list[dict], notes: bool = True) -> list[str]:
    """Per chosen part: the booking's line name -> title (brand) qty unit price, plus a stock
    note when the catalogue says 0 in stock; then the cart total and fluids to prepare.
    A part that replaced an out-of-stock pick says so: "Bytt från <original title> (slut)"
    (approval and Beställd messages alike), unless `notes` is False (the approval text's
    length guard)."""
    lines = ["Delar:"]
    for p in parts:
        name = str(p.get("name") or "").strip()
        head = f"{name} -> {p.get('title')}" if name else str(p.get("title"))
        stock = " – OBS: 0 i lager" if p.get("in_stock") is False else ""
        replaced = p.get("replaced_from")
        swapped = (f" – Bytt från {replaced.get('title')} (slut)"
                   if notes and isinstance(replaced, dict) else "")
        lines.append(f"- {head} ({p.get('brand') or '–'}) x{p.get('qty')} "
                     f"à {sek(p.get('price_sek'))}{stock}{swapped}")
    lines.append(f"Totalt: {sek(total)}")
    return lines + prepare_section(fluids)


# Exactly the candidate fields stored for an alternative: what PartsPortal.add_to_cart reads
# (url_or_id, and search_term to bring its result page back), what the message shows,
# article_nr, and the shop's placement (axle/side): a swap re-runs the placement filter on
# the stored alternative, and the axle is often only in that field, not in the title.
# Never page text or anything else the search result carried.
ALTERNATIVE_FIELDS = ("title", "brand", "price_sek", "url_or_id", "in_stock", "search_term",
                      "article_nr", "placement")
_OPTIONAL_ALT_FIELDS = ("search_term", "article_nr", "placement")
PLACEMENT_MAX = 80


def clean_placement(value) -> str | None:
    """The shop's placement text made safe to store: a string, control/format characters
    removed, single spaces, at most PLACEMENT_MAX characters; None when empty."""
    if not isinstance(value, str):
        return None
    kept = "".join(" " if unicodedata.category(c) == "Cc" else c for c in value
                   if unicodedata.category(c) != "Cf")
    return " ".join(kept.split())[:PLACEMENT_MAX] or None


def alternative_entry(alt: dict) -> dict:
    """The stored plan["parts"][i]["alternative"]: ALTERNATIVE_FIELDS (search_term,
    article_nr and placement only when present) plus the matcher's alt_reason and
    saving_sek."""
    entry = {k: alt.get(k) for k in ALTERNATIVE_FIELDS
             if k not in _OPTIONAL_ALT_FIELDS or alt.get(k) is not None}
    placement = clean_placement(alt.get("placement"))
    entry.pop("placement", None)
    if placement is not None:
        entry["placement"] = placement
    entry["alt_reason"] = alt.get("alt_reason")
    entry["saving_sek"] = alt.get("saving_sek")
    return entry


def replaced_entry(original: dict) -> dict:
    """The stored plan["parts"][i]["replaced_from"]: the out-of-stock pick, for the message."""
    return {k: original.get(k) for k in ("title", "brand", "price_sek")}


def alternative_lines(parts: list[dict]) -> list[str]:
    """One "Alternativ för <name>: ..." line per part that has an alternative. The saving of
    a "dyr" alternative is per unit ("sparar N SEK/st"), as the prices are."""
    lines = []
    for p in parts:
        alt = p.get("alternative")
        if not isinstance(alt, dict):
            continue
        name = str(p.get("name") or "").strip() or str(p.get("title"))
        stock = " – i lager" if alt.get("in_stock") is True else ""
        why = (f"dyr: sparar {sek(alt.get('saving_sek'))}/st" if alt.get("alt_reason") == "dyr"
               else "slut")
        lines.append(f"Alternativ för {name}: {alt.get('title')} ({alt.get('brand') or '–'}) "
                     f"à {sek(alt.get('price_sek'))}{stock} ({why})")
    return lines


UNVERIFIED_PREFIX = "UNVERIFIED-"
REFERENCE_LATER = "Ordernummer: bekräftas senare (kolla Skickade Beställningar i pro.meca.se)"


def reference_line(ref) -> str:
    """The "Ordernummer" line of the Beställd message. An UNVERIFIED-<UTC> placeholder
    (the page left checkout but showed no number) is not a number the owner can use: the
    order checker in approve.py finds the real one later in "Skickade Beställningar"."""
    if isinstance(ref, str) and ref.startswith(UNVERIFIED_PREFIX):
        return REFERENCE_LATER
    return f"Ordernummer: {ref}"


# The MECA booking page lists prices EXCLUDING VAT ("Totalt exklusive moms"; fixture
# booking_delar.html: 6 285,64 ex vs "Att betala" 7 857 = x 1.25), and a part row's `price` is
# that row's TOTAL (unit x qty, e.g. 4 x 143,90 = 575,60). The pro.meca.se cart total is
# INCLUDING 25 % Swedish VAT, so it is divided by VAT_FACTOR before it is compared.
VAT_FACTOR = 1.25


def _booking_parts_price(parts) -> float | None:
    """Sum of the booking's part row totals (ex VAT), or None when any row is unusable."""
    if not isinstance(parts, list) or not parts:
        return None
    total = 0.0
    for row in parts:
        if not isinstance(row, dict) or parse_qty(row.get("qty")) is None:
            return None
        price = row.get("price")
        if not isinstance(price, str):
            return None
        value = parse_sek(price)
        if value is None or not value > 0:
            return None
        total += value
    return round(total, 2)


def margin_line(booking: dict, cart_total, rules: Rules) -> str | None:
    """Informational warning for the approval message, or None. Compares the purchase (cart
    total incl. VAT, converted to ex VAT) with what the booking charges for the parts (ex
    VAT). A loss gives "OBS: inköp exkl. moms X SEK över bokningens delpris Y SEK"; else a
    margin under rules.min_margin_pct (when > 0) gives "OBS: marginal N % (under M %)".
    Anything unknown (a missing or unparsable price on any row, other currency, bad total)
    gives None: never a false alarm. Never blocks anything."""
    if isinstance(cart_total, bool) or not isinstance(cart_total, (int, float)) \
            or not math.isfinite(cart_total) or cart_total < 0:
        return None
    customer = _booking_parts_price(booking.get("parts") if isinstance(booking, dict) else None)
    if customer is None:
        return None
    purchase = round(cart_total / VAT_FACTOR, 2)
    if purchase > customer:
        return (f"OBS: inköp exkl. moms {purchase:.2f} SEK över bokningens delpris "
                f"{customer:.2f} SEK")
    minimum = rules.min_margin_pct
    margin = (customer - purchase) / customer * 100
    if minimum > 0 and margin < minimum:
        return f"OBS: marginal {margin:.0f} % (under {minimum} %)"
    return None


def build_summary(booking: dict, cart: dict, fluids: list[dict], parts: list[dict],
                  alternatives: bool = True, notes: bool = True,
                  margin: str | None = None) -> str:
    """Approval text: booking header, chosen parts (with any in-stock replacement), cart
    total, fluids, then, after a blank line, one line per alternative. Plain text.
    `alternatives` / `notes` False leave out the alternative block / the "Bytt från" notes
    (the length guard in parts_order); both False is the pre-package-2 summary. `margin` is
    the optional margin_line text, shown after the total."""
    reg = str(booking.get("reg_nr") or "–").strip()
    label = booking.get("booking_nr") or booking.get("id") or "okänd bokning"
    alt = alternative_lines(parts) if alternatives else []
    body = order_body(parts, cart.get("total_sek"), fluids, notes=notes)
    if margin:  # right after the "Totalt:" line, before the fluids
        body.insert(1 + len(parts) + 1, margin)
    return "\n".join([
        f"Delbeställning {label}",
        f"Kund: {booking.get('driver_name') or booking.get('customer_line') or '–'}",
        f"Fordon: {reg} {booking.get('vehicle') or ''}".rstrip(),
    ] + body + ([""] + alt if alt else []))
