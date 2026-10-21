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

# Swedish stems: lowercase SUBSTRINGS, because Swedish compounds them ("motorolja",
# "växellådsolja", "spolarvätska", "smörjfett"). "fett" stays a substring on purpose (fail
# closed): "Fettinsats" therefore still counts as a fluid. Extend as needed.
FLUID_KEYWORDS = (
    "olja", "bromsvätska", "kylarvätska", "frostskydd", "glykol", "spolarvätska", "vätska",
    "oljor", "fett", "smörj", "glysantin", "kylvätska",
)
# English keywords: WHOLE WORDS only (optional plural "s"), so "Ignition coil", "foil",
# "boil" and "soil" are not fluids. Covers "brake fluid", "washer fluid", "engine oil".
# Bounded by non-letters, so digits may touch ("AdBlue10L").
FLUID_WORDS = ("oil", "fluid", "grease", "lubricant", "coolant", "antifreeze", "adblue")
_FLUID_WORD = re.compile(r"(?<![^\W\d_])(?:" + "|".join(FLUID_WORDS) + r")s?(?![^\W\d_])")
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
    "You match a workshop booking line to a supplier catalogue. The candidates are already "
    "ordered by brand preference (preferred brands first). Pick the single candidate that is "
    "the best match for the same part as the booking line; never pick a different part, even "
    "from a preferred brand. If none truly matches, give a confidence below 0.5. Never guess."
)


def is_fluid(name: str) -> bool:
    text = _FILTER_WORD.sub(" ", str(name).lower())
    return (any(k in text for k in FLUID_KEYWORDS) or bool(_FLUID_WORD.search(text))
            or any(re.search(p, text) for p in FLUID_PATTERNS))


def _norm(value) -> str:
    """Comparison key for brands: NFKD, accents dropped, dotless i -> i, lowercase, and every
    character that is not a letter or digit removed ("Pró.Meıster" -> "promeister")."""
    text = unicodedata.normalize("NFKD", str(value or "")).replace("\u0131", "i")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return "".join(ch for ch in text.lower() if ch.isalnum())


def has_non_latin(value) -> bool:
    """True if any letter is outside the Latin script (homoglyphs such as a Cyrillic "р")."""
    return any(ch.isalpha() and not unicodedata.name(ch, "").startswith("LATIN")
               for ch in str(value or ""))


# Whole tokens only, explicit forms only: "Bakgrund", "Höjd", "Framrutan" never count as
# placement. Two-letter codes like fa/ra/va/ha are deliberately absent (false positives).
# Tokens are \w+ runs, so "Fram-" gives "fram" and "fram/bak" gives both.
AXLE_WORDS = {
    **dict.fromkeys(("fram", "front", "framaxel", "framaxeln", "framhjul", "framhjulet",
                     "framre", "främre"), "fram"),
    **dict.fromkeys(("bak", "rear", "back", "bakaxel", "bakaxeln", "bakhjul", "bakhjulet",
                     "bakre"), "bak"),
}
SIDE_WORDS = {
    **dict.fromkeys(("vä", "vänster", "vänstra", "left", "lh"), "vä"),
    **dict.fromkeys(("hö", "höger", "högra", "right", "rh"), "hö"),
}
# Parts that exist per axle: a booking line naming none of these without an axle is blocked
# (casefolded substring of the line name). Extend as needed.
AXLE_SPECIFIC = (
    "belägg", "bromsskiv", "bromsklots", "bromsklossar", "bromsok", "bromstrumma",
    "stötdämpare", "fjäder", "fjädrar", "hjullager", "länkarm", "krängningshämmare",
    "spindelled", "styrled", "hjulnav", "drivaxel",
)


def _words(value) -> list[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.findall(r"\w+", text)


def placement_of(*texts) -> tuple[set[str], set[str]]:
    """(axles, sides) stated anywhere in the given texts, e.g. ({"fram"}, {"vä"})."""
    words = [w for t in texts for w in _words(t)]
    return ({AXLE_WORDS[w] for w in words if w in AXLE_WORDS},
            {SIDE_WORDS[w] for w in words if w in SIDE_WORDS})


def _placement_filter(line_name: str, ranked: list[dict]) -> list[dict]:
    """Keep only candidates whose axle/side agrees with the booking line. Fails closed.

    A line naming an axle (or side) keeps only candidates stating exactly that axle (side);
    candidates stating none or both are dropped. An axle-specific part (AXLE_SPECIFIC) on a
    line naming no axle is blocked ([]): the owner must say which axle. Otherwise a line
    naming none keeps everything unless the candidates disagree among themselves, which is
    ambiguous ([]). A line naming both axles (or both sides) is ambiguous too.
    """
    line_axles, line_sides = placement_of(line_name)
    if len(line_axles) > 1 or len(line_sides) > 1:
        return []
    folded = unicodedata.normalize("NFKC", str(line_name or "")).casefold()
    if not line_axles and any(part in folded for part in AXLE_SPECIFIC):
        return []
    states = [(c, *placement_of(c.get("placement"), c.get("title"))) for c in ranked]
    for i, wanted in ((1, line_axles), (2, line_sides)):
        if wanted:
            states = [s for s in states if s[i] == wanted]
        elif len(set().union(*(s[i] for s in states))) > 1:
            return []
    return [s[0] for s in states]


# Explicit plural -> singular for common part words (only mappings known to be right).
_SINGULAR = {
    "bromsskivor": "bromsskiva", "kolvringar": "kolvring", "glödlampor": "glödlampa",
    "drivremmar": "drivrem",
}
# Search synonyms, keyed by the normalised (lowercase, NFC) word or phrase. Owner rule
# (Wave 2): Pollenfilter = Kupéfilter. pro.meca.se returns NOTHING for "pollenfilter";
# it files these parts as "Kupéfilter", so that word is searched first. The unaccented
# spelling is searched too, in case the shop's search does not fold accents.
SEARCH_SYNONYMS: dict[str, tuple[str, ...]] = {
    "pollenfilter": ("kupéfilter", "kupefilter"),
    "kupefilter": ("kupéfilter",),
    "kupéfilter": ("kupefilter",),
}
_PLACEMENT_TERMS = set(AXLE_WORDS) | set(SIDE_WORDS)
_QTY_TOKEN = re.compile(r"(?:x\d+|\d+x|\d+st)")


def search_terms(line_name) -> list[str]:
    """Search terms for a booking line: [synonyms..., normalised, original], most specific last.

    normalised = lowercase, quantity ("x2", "2 st") and placement words removed, common
    plurals made singular. A form equal to the original (ignoring case) gives one term.
    SEARCH_SYNONYMS are applied to the normalised form (the whole phrase, then each word in
    place) and searched first. Duplicates (ignoring case) are dropped.
    """
    original = str(line_name or "").strip()
    if not original:
        return []
    folded = unicodedata.normalize("NFC", original).casefold()
    tokens = [t.strip(".:()[]-") for t in re.split(r"[\s/,;+&]+", folded)]
    tokens = [t for t in tokens if t]
    drop = set()
    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok in _PLACEMENT_TERMS or _QTY_TOKEN.fullmatch(tok):
            drop.add(i)
        elif tok in ("och", "and") and nxt in _PLACEMENT_TERMS:
            drop.add(i)  # "fram och bak"
        elif (tok == "x" and nxt.isdigit()) or (tok.isdigit() and nxt == "st"):
            drop.update((i, i + 1))  # "x 2", "2 st"
    kept = [_SINGULAR.get(t, t) for i, t in enumerate(tokens) if i not in drop]
    normalised = " ".join(kept)
    if not normalised:
        return [original]
    synonyms = list(SEARCH_SYNONYMS.get(normalised, ()))
    for i, tok in enumerate(kept):
        synonyms += [" ".join(kept[:i] + [syn] + kept[i + 1:]) for syn in SEARCH_SYNONYMS.get(tok, ())]
    same = normalised == " ".join(folded.split())
    terms, seen = [], set()
    for term in synonyms + ([] if same else [normalised]) + [original]:
        key = unicodedata.normalize("NFC", term).casefold()
        if term and key not in seen:
            seen.add(key)
            terms.append(term)
    return terms


def is_forbidden(cand: dict, rules: Rules) -> bool:
    forbidden = {_norm(b) for b in rules.brand_forbid} | set(ALWAYS_FORBIDDEN)
    forbidden.discard("")  # an empty entry would match everything
    haystacks = (_norm(cand.get("brand")), _norm(cand.get("title")))
    return any(f in h for f in forbidden for h in haystacks)


def _brand_rank(cand: dict, rules: Rules) -> int:
    """Index of the first rules.brand_prefer entry in the brand or title; len(prefer) if none."""
    # pro.meca.se titles carry the brand ("Bosch Oljefilter, ..."); brand is usually "".
    prefer = [_norm(b) for b in rules.brand_prefer]
    haystacks = (_norm(cand.get("brand")), _norm(cand.get("title")))
    for i, p in enumerate(prefer):
        if p and any(p in h for h in haystacks):
            return i
    return len(prefer)


def rank_candidates(candidates: list[dict], rules: Rules) -> list[dict]:
    """Allowed candidates, preferred brands first. Drops forbidden brands, non-Latin letters
    and fluids (a fluid is never auto-ordered, whatever the booking line says)."""
    allowed = [c for c in candidates
               if not is_forbidden(c, rules)
               and not any(has_non_latin(c.get(k)) for k in ("title", "brand", "placement"))
               and not any(is_fluid(c.get(k) or "") for k in ("title", "brand"))]
    return sorted(allowed, key=lambda c: _brand_rank(c, rules))  # stable, new list


def _pick(line: dict, ranked: list[dict], picker) -> dict | None:
    """One picker call on `ranked`; the chosen candidate, or None unless valid and confident."""
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


def choose(line: dict, candidates: list[dict], rules: Rules, picker) -> dict | None:
    """Best allowed candidate for a booking line, or None. Brand preference is enforced:
    the picker sees the preferred-brand candidates first and the rest only if it declines."""
    name = line.get("name", "")
    if is_fluid(name) or has_non_latin(name):
        return None
    ranked = _placement_filter(name, rank_candidates(candidates, rules))
    if not ranked:
        return None
    preferred = [c for c in ranked if _brand_rank(c, rules) < len(rules.brand_prefer)]
    if preferred and len(preferred) < len(ranked):
        chosen = _pick(line, preferred, picker)
        if chosen is not None:
            return chosen
    return _pick(line, ranked, picker)


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
