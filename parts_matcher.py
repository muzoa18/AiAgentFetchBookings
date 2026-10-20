"""Pure part-matching logic for parts ordering. Fails closed: when in doubt, return None."""
import json
import logging
import math
import re
import unicodedata

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
    "oljor", "fett", "grease", "lubricant", "smörj", "glysantin", "kylvätska",
)
# Patterns for fluids with no generic keyword: viscosity grades, brake fluid grades, ATF.
FLUID_PATTERNS = (r"\b\d{1,2}w-?\d{2,3}\b", r"\bdot ?[345]\b", r"\batf\b")
# Words containing "filter" (oljefilter, oil filter, pollenfilter) are parts, not fluids.
# Only the filter word itself is stripped, so "Olja/filter" still has its fluid word.
_FILTER_WORD = re.compile(r"(?:\boil\s+)?[a-zåäö]*filter")

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
    return any(k in text for k in FLUID_KEYWORDS) or any(re.search(p, text) for p in FLUID_PATTERNS)


def _norm(value) -> str:
    """NFKC, drop format chars (zero-width, soft hyphen), lowercase, remove spaces/hyphens/underscores."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return re.sub(r"[\s\-_]+", "", text.lower())


def is_forbidden(cand: dict, rules: Rules) -> bool:
    forbidden = {_norm(b) for b in rules.brand_forbid} | set(ALWAYS_FORBIDDEN)
    forbidden.discard("")  # an empty entry would match everything
    haystacks = (_norm(cand.get("brand")), _norm(cand.get("title")))
    return any(f in h for f in forbidden for h in haystacks)


def rank_candidates(candidates: list[dict], rules: Rules) -> list[dict]:
    allowed = [c for c in candidates if not is_forbidden(c, rules)]
    prefer = [_norm(b) for b in rules.brand_prefer]

    def rank(cand: dict) -> int:
        brand = _norm(cand.get("brand"))
        for i, p in enumerate(prefer):
            if p and p in brand:
                return i
        return len(prefer)

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
