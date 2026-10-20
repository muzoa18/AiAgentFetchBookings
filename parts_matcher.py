"""Pure part-matching logic for parts ordering. Fails closed: when in doubt, return None."""
import json
import logging
import math
import re

from rules import Rules

log = logging.getLogger(__name__)

PICK_MODEL = "claude-sonnet-5-5"
MIN_CONFIDENCE = 0.8

# Hard-coded in addition to rules.brand_forbid, so clearing the rules file can never allow it.
ALWAYS_FORBIDDEN = ("promeister",)

# Lowercase substrings marking fluids/consumables we never auto-order. Extend as needed.
FLUID_KEYWORDS = (
    "olja", "oil", "bromsvätska", "brake fluid", "kylarvätska", "coolant", "frostskydd",
    "antifreeze", "glykol", "spolarvätska", "washer fluid", "adblue", "vätska", "fluid",
)
# Words containing "filter" (oljefilter, oil filter, pollenfilter) are parts, not fluids.
_FILTER_WORD = re.compile(r"(?:\boil\s+)?\S*filter\S*")

PICK_SCHEMA = {
    "type": "object",
    "properties": {"index": {"type": "integer"}, "confidence": {"type": "number"}},
    "required": ["index", "confidence"],
    "additionalProperties": False,
}

PICK_SYSTEM = (
    "You match a workshop booking line to a supplier catalogue. Pick the single best matching "
    "candidate. If none truly matches, give a confidence below 0.5. Never guess."
)


def is_fluid(name: str) -> bool:
    text = _FILTER_WORD.sub(" ", str(name).lower())
    return any(k in text for k in FLUID_KEYWORDS)


def _norm(value) -> str:
    return str(value or "").strip().lower()


def _is_forbidden(cand: dict, rules: Rules) -> bool:
    brand = _norm(cand.get("brand"))
    forbidden = {_norm(b) for b in rules.brand_forbid} | set(ALWAYS_FORBIDDEN)
    if brand in forbidden:
        return True
    return any(f in _norm(cand.get("title")) for f in ALWAYS_FORBIDDEN)


def rank_candidates(candidates: list[dict], rules: Rules) -> list[dict]:
    allowed = [c for c in candidates if not _is_forbidden(c, rules)]
    prefer = [_norm(b) for b in rules.brand_prefer]

    def rank(cand: dict) -> int:
        brand = _norm(cand.get("brand"))
        return prefer.index(brand) if brand in prefer else len(prefer)

    return sorted(allowed, key=rank)  # sorted() is stable and returns a new list


def choose(line: dict, candidates: list[dict], rules: Rules, picker) -> dict | None:
    if is_fluid(line.get("name", "")):
        return None
    ranked = rank_candidates(candidates, rules)
    if not ranked:
        return None
    try:
        index, confidence = picker(line, ranked)
    except Exception:
        log.exception("part picker failed for %r", line.get("name"))
        return None
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(ranked):
        return None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    if not math.isfinite(confidence) or confidence < MIN_CONFIDENCE:
        return None
    return ranked[index]


def make_claude_picker(api_key: str, client=None):
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

    def picker(line: dict, ranked: list[dict]) -> tuple[int, float]:
        listing = "\n".join(
            f"{i}. {c.get('title')} | brand: {c.get('brand')} | price: {c.get('price_sek')} SEK"
            for i, c in enumerate(ranked)
        )
        prompt = (
            f"Booking line: {line.get('name')} (qty {line.get('qty')})\n\n"
            f"Candidates:\n{listing}\n\n"
            "Return the index of the best match and your confidence (0 to 1)."
        )
        response = client.messages.create(
            model=PICK_MODEL,
            max_tokens=200,
            system=PICK_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": PICK_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            return -1, 0.0
        try:
            text = next(b.text for b in response.content if b.type == "text")
            data = json.loads(text)
            index, confidence = data["index"], data["confidence"]
        except (StopIteration, ValueError, KeyError, TypeError):
            return -1, 0.0
        if isinstance(index, bool) or not isinstance(index, int) or isinstance(confidence, bool) \
                or not isinstance(confidence, (int, float)):
            return -1, 0.0
        return index, float(confidence)

    return picker
