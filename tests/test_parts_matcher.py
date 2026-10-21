import json
import unicodedata
from types import SimpleNamespace

import pytest

import parts_matcher
from parts_matcher import (PICK_MODEL, SEARCH_SYNONYMS, choose, is_fluid, make_claude_picker, rank_candidates,
                           search_terms)
from rules import Rules

RULES = Rules(brand_prefer=("Bosch", "Mann"), brand_forbid=("ProMeister",), ceiling_sek=10000, mode="approve")
# Not axle-specific and no placement words: generic choose() tests are unaffected by placement rules.
LINE = {"name": "Oljefilter", "qty": 1}


def cand(title, brand, price=100, ref="x"):
    return {"title": title, "brand": brand, "price_sek": price, "url_or_id": ref}


@pytest.mark.parametrize("name", [
    "Motorolja 5W-30", "Bromsvätska DOT4", "Kylarvätska G12", "Spolarvätska", "Växellådsolja 75W-90",
    "Servoolja", "AdBlue 10L", "Brake fluid DOT 4", "Engine oil 5W-30", "Coolant", "Frostskydd", "Olja",
    "Motorolja och oljefilter", "Motorolja/oljefilter", "Motorolja,oljefilter", "Olja+filter", "Olja/Filter",
    "Mobil 1 0W-40", "Castrol Edge 5W-30", "ATF", "Oljor", "Motoroljor", "Smörjfett", "Fett", "DOT4", "DOT 5.1",
    "Grease", "Lubricant", "Glysantin G48", "Kylvätska",
])
def test_fluids_detected(name):
    assert is_fluid(name)


@pytest.mark.parametrize("name", [
    "Oljefilter", "Oil filter", "Bromsskivor fram", "Bromsskiva", "Bromsbelägg", "Pollenfilter", "Tändstift",
    "Luftfilter", "Bränslefilter", "Oljetrycksgivare",
])
def test_non_fluids(name):
    assert not is_fluid(name)


def test_fluids_are_never_matched():
    picker = lambda line, ranked: pytest.fail("picker must not be called")
    assert choose({"name": "Motorolja 5W-30", "qty": 1}, [cand("Motorolja", "Castrol")], RULES, picker) is None


def test_promeister_dropped_even_if_only_candidate():
    assert choose(LINE, [cand("Belägg", "ProMeister")], RULES, lambda l, r: (0, 1.0)) is None


def test_promeister_forbidden_even_when_rules_list_cleared():
    rules = Rules(("Bosch",), (), 10000, "approve")
    assert rank_candidates([cand("Belägg", " promeister ")], rules) == []


def test_promeister_in_title_dropped():
    assert rank_candidates([cand("ProMeister bromsbelägg", "")], RULES) == []
    assert rank_candidates([cand("PROMEISTER bromsbelägg", "Other")], RULES) == []


def test_forbid_list_brands_dropped_case_insensitive():
    rules = Rules((), ("Foo",), 10000, "approve")
    assert rank_candidates([cand("a", " FOO "), cand("b", "Bar")], rules) == [cand("b", "Bar")]


def test_bosch_ranked_before_other_brands():
    a, b, c, d = cand("a", "Other"), cand("b", "mann"), cand("c", "Bosch"), cand("d", "Else")
    assert rank_candidates([a, b, c, d], RULES) == [c, b, a, d]


def test_rank_does_not_mutate_input():
    items = [cand("a", "Other"), cand("c", "Bosch")]
    snapshot = list(items)
    rank_candidates(items, RULES)
    assert items == snapshot


def test_choose_returns_ranked_candidate():
    other, bosch = cand("a", "Other"), cand("c", "Bosch")
    seen = {}

    def picker(line, ranked):
        seen["ranked"] = ranked
        return 0, 0.8

    assert choose(LINE, [other, bosch], RULES, picker) is bosch
    assert seen["ranked"] == [bosch]  # brand preference: the preferred subset is offered first


def test_low_confidence_returns_none():
    assert choose(LINE, [cand("a", "Bosch")], RULES, lambda l, r: (0, 0.79)) is None


@pytest.mark.parametrize("result", [
    (1, 0.9), (-1, 0.9), (0, float("nan")), (0, float("inf")), (0, "high"), (0, None), ("0", 0.9), (True, 0.9),
])
def test_bad_picker_output_returns_none(result):
    assert choose(LINE, [cand("a", "Bosch")], RULES, lambda l, r: result) is None


def test_picker_index_out_of_range_returns_none():
    assert choose(LINE, [cand("a", "Bosch")], RULES, lambda l, r: (5, 0.99)) is None


def test_empty_candidates_returns_none():
    assert choose(LINE, [], RULES, lambda l, r: pytest.fail("not called")) is None


def test_picker_exception_returns_none():
    def boom(line, ranked):
        raise RuntimeError("api down")

    assert choose(LINE, [cand("a", "Bosch")], RULES, boom) is None


class FakeClient:
    def __init__(self, response):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self._response = response

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def reply(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


def test_claude_picker_sends_model_and_titles_and_parses():
    client = FakeClient(reply(json.dumps({"index": 1, "confidence": 0.93})))
    picker = make_claude_picker("key", client=client)
    ranked = [cand("Bosch Bromsbelägg", "Bosch", 450), cand("Febi Bromsskiva", "Febi", 300)]
    assert picker(LINE, ranked) == (1, 0.93)
    call = client.calls[0]
    assert call["model"] == PICK_MODEL == "claude-sonnet-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    prompt = call["messages"][0]["content"]
    assert "Bosch Bromsbelägg" in prompt and "Febi Bromsskiva" in prompt
    assert "Booking line: Oljefilter (qty 1)" in prompt


def test_claude_picker_refusal_returns_no_match():
    client = FakeClient(reply("", stop_reason="refusal"))
    assert make_claude_picker("key", client=client)(LINE, [cand("a", "b")]) == (-1, 0.0)


@pytest.mark.parametrize("text", ["not json", "{}", '{"index": "a", "confidence": 1}', "[1]"])
def test_claude_picker_malformed_returns_no_match(text):
    client = FakeClient(reply(text))
    assert make_claude_picker("key", client=client)(LINE, [cand("a", "b")]) == (-1, 0.0)


def test_claude_picker_no_text_block_returns_no_match():
    client = FakeClient(SimpleNamespace(stop_reason="end_turn", content=[]))
    assert make_claude_picker("key", client=client)(LINE, [cand("a", "b")]) == (-1, 0.0)


@pytest.mark.parametrize("brand,title", [
    ("Pro Meister", "x"), ("Pro-Meister", "x"), ("ProMeister AB", "x"), ("Promeister\u200b", "x"),
    ("", "ProMeis\u00adter x"), ("", "Pro\u00a0Meister x"), ("pro_meister", "x"), ("", "Pro-Meister belägg"),
])
def test_promeister_variants_forbidden(brand, title):
    assert rank_candidates([cand(title, brand)], RULES) == []


def test_forbid_list_is_normalised_substring():
    rules = Rules((), ("Foo",), 10000, "approve")
    assert rank_candidates([cand("a", "Foo Inc"), cand("Foo-brand part", "Z")], rules) == []


def test_empty_forbid_entry_does_not_block_everything():
    rules = Rules((), ("", "  "), 10000, "approve")
    assert len(rank_candidates([cand("a", "Bar")], rules)) == 1


def test_preferred_matching_is_normalised_substring():
    a, b, c = cand("a", "Other"), cand("b", "BOSCH "), cand("c", "Bosch GmbH")
    assert rank_candidates([a, b, c], RULES) == [b, c, a]


# ── Task 6b: title brand, placement, non-Latin letters, search terms ─────────

def pcand(title, placement="", ref=None, brand=""):
    return {"title": title, "brand": brand, "price_sek": 100, "url_or_id": ref or title,
            "article_nr": None, "in_stock": True, "placement": placement}


def recording(result=(0, 0.95)):
    seen = []

    def picker(line, ranked):
        seen.append(list(ranked))
        return result
    picker.seen = seen
    return picker


def test_preferred_brand_in_title_ranks_first():
    other, bosch = pcand("Oljefilter Hengst"), pcand("Bosch Oljefilter, Filterinsats")
    assert rank_candidates([other, bosch], RULES) == [bosch, other]


def test_preferred_brand_in_title_normalised():
    other, bosch = pcand("Oljefilter Purflux"), pcand("Oljefilter BOSCH GmbH")
    mann = pcand("Luftfilter MANN-FILTER")
    assert rank_candidates([other, mann, bosch], RULES) == [bosch, mann, other]


def test_title_brand_rank_still_drops_forbidden_and_keeps_input():
    items = [pcand("ProMeister Bosch-kompatibel"), pcand("Oljefilter Hengst")]
    snapshot = [dict(c) for c in items]
    assert rank_candidates(items, RULES) == [items[1]]
    assert items == snapshot


def test_rear_set_never_reaches_picker_for_front_line():
    rear, front = pcand("Bromsbelägg Bosch bak", "bak"), pcand("Bromsbelägg Bosch fram", "fram")
    picker = recording()
    assert choose({"name": "Bromsbelägg fram", "qty": 1}, [rear, front], RULES, picker) is front
    assert picker.seen == [[front]]


def test_axle_from_title_when_placement_empty():
    rear, front = pcand("Bromsbelägg bak Bosch"), pcand("Bromsbelägg fram Bosch")
    picker = recording()
    assert choose({"name": "Bromsbelägg fram", "qty": 1}, [rear, front], RULES, picker) is front
    assert picker.seen == [[front]]


def test_candidate_stating_both_axles_dropped_for_front_line():
    both = pcand("Bromsbelägg fram/bak", "fram")
    assert choose({"name": "Bromsbelägg fram", "qty": 1}, [both], RULES, recording()) is None


def test_wrong_side_returns_none():
    picker = recording()
    line = {"name": "Bromsskiva fram vä", "qty": 1}
    assert choose(line, [pcand("Bromsskiva Bosch", "fram hö")], RULES, picker) is None
    assert picker.seen == []


def test_matching_axle_and_side_chosen():
    left, right = pcand("Bromsskiva", "fram vä"), pcand("Bromsskiva", "fram hö")
    picker = recording()
    assert choose({"name": "Bromsskiva fram vänster", "qty": 1}, [right, left], RULES, picker) is left
    assert picker.seen == [[left]]


def test_unknown_side_dropped_when_line_names_side():
    picker = recording()
    assert choose({"name": "Bromsskiva fram vä", "qty": 1}, [pcand("Bromsskiva", "fram")],
                  RULES, picker) is None
    assert picker.seen == []


def test_unknown_axle_dropped_when_line_names_axle():
    unknown, front = pcand("Bromsbelägg Bosch"), pcand("Bromsbelägg Bosch", "fram")
    picker = recording()
    assert choose({"name": "Bromsbelägg fram", "qty": 1}, [unknown, front], RULES, picker) is front
    assert picker.seen == [[front]]
    assert choose({"name": "Bromsbelägg fram", "qty": 1}, [unknown], RULES, recording()) is None


def test_english_placement_words():
    rear, front = pcand("Brake pads rear"), pcand("Brake pads front")
    picker = recording()
    assert choose({"name": "Brake pads front", "qty": 1}, [rear, front], RULES, picker) is front
    assert picker.seen == [[front]]


def test_line_without_axle_and_mixed_candidates_is_ambiguous():
    # Torkarblad is not axle-specific, but candidates disagreeing on axle is still ambiguous.
    picker = recording()
    cands = [pcand("Torkarblad Bosch", "fram"), pcand("Torkarblad Bosch", "bak")]
    assert choose({"name": "Torkarblad", "qty": 1}, cands, RULES, picker) is None
    assert picker.seen == []


def test_line_without_side_and_mixed_sides_is_ambiguous():
    picker = recording()
    cands = [pcand("Bromsskiva", "fram vä"), pcand("Bromsskiva", "fram hö")]
    assert choose({"name": "Bromsskiva fram", "qty": 1}, cands, RULES, picker) is None
    assert picker.seen == []


def test_line_without_axle_and_same_axle_candidates_proceeds():
    a, b = pcand("Torkarblad Bosch", "fram"), pcand("Torkarblad Hengst fram")
    picker = recording()
    assert choose({"name": "Torkarblad", "qty": 1}, [b, a], RULES, picker) is a
    assert picker.seen == [[a]]  # Bosch offered alone first


def test_line_without_axle_and_no_candidate_axles_proceeds():
    a = pcand("Oljefilter Hengst")
    assert choose({"name": "Oljefilter", "qty": 1}, [a], RULES, recording()) is a


@pytest.mark.parametrize("name", ["Bakgrundsbelysning", "Höjdgivare", "Framtida del", "Backspegel"])
def test_placement_needs_whole_words_in_line(name):
    # None of these names an axle or side, so a candidate without placement is fine.
    a = pcand(name)
    assert choose({"name": name, "qty": 1}, [a], RULES, recording()) is a


def test_placement_needs_whole_words_in_candidate():
    # "Bakgrund"/"Höjd" in a title are not "bak"/"hö": the candidate has an unknown axle.
    picker = recording()
    cands = [pcand("Torkarblad Bakgrund Höjd"), pcand("Torkarblad Bosch", "fram")]
    assert choose({"name": "Torkarblad", "qty": 1}, cands, RULES, picker) is cands[1]
    assert picker.seen == [[cands[1]]]  # the unknown-axle candidate stays; Bosch offered first
    assert parts_matcher._placement_filter("Torkarblad", cands) == cands


def test_line_naming_both_axles_is_ambiguous():
    cands = [pcand("Bromsbelägg", "fram"), pcand("Bromsbelägg", "bak")]
    assert choose({"name": "Bromsbelägg fram och bak", "qty": 1}, cands, RULES, recording()) is None


@pytest.mark.parametrize("title,brand", [
    ("\u0420romeister bromsbelägg", ""),      # Cyrillic Er instead of P
    ("Bromsbelägg", "\u0420romeister"),
    ("Pro\u043Ceister bromsbelägg", ""),      # Cyrillic em
    ("M\u043Etorolja filter", ""),            # Cyrillic o in a fluid word
    ("Bromsbelägg \u0391", ""),               # Greek Alpha
])
def test_non_latin_candidate_dropped(title, brand):
    bad, good = pcand(title, brand=brand), pcand("Bromsbelägg Bosch")
    picker = recording()
    assert choose(LINE, [bad, good], RULES, picker) is good
    assert picker.seen == [[good]]
    assert rank_candidates([bad], RULES) == []


def test_swedish_and_accented_letters_are_latin():
    a = pcand("Bromsbelägg Bosch, för ÅÄÖ é ü – 2 st (fram)")
    assert rank_candidates([a], RULES) == [a]


@pytest.mark.parametrize("name", [
    "Motor\u043Elj\u0430",  # Cyrillic o and a: is_fluid misses it; the script check must not
    "M\u043Etorolja", "\u0420romeister belägg", "Bromsbelägg \u0444"])
def test_non_latin_line_is_unmatched(name):
    if name.startswith("Motor"):
        assert not is_fluid(name)
    picker = recording()
    assert choose({"name": name, "qty": 1}, [pcand("Bromsbelägg Bosch")], RULES, picker) is None
    assert picker.seen == []


@pytest.mark.parametrize("name,expected", [
    ("Bromsskivor fram x2", ["bromsskiva", "Bromsskivor fram x2"]),
    ("  Bromsskivor   bak  2 st ", ["bromsskiva", "Bromsskivor   bak  2 st"]),
    ("Bromsbelägg fram/bak", ["bromsbelägg", "Bromsbelägg fram/bak"]),
    ("Bromsskiva fram vä", ["bromsskiva", "Bromsskiva fram vä"]),
    ("Glödlampor höger", ["glödlampa", "Glödlampor höger"]),
    ("Drivremmar", ["drivrem", "Drivremmar"]),
    ("Kolvringar x4", ["kolvring", "Kolvringar x4"]),
    ("Brake pads front left", ["brake pads", "Brake pads front left"]),
    ("Torkarblad 2st", ["torkarblad", "Torkarblad 2st"]),
    ("Stötdämpare bakre", ["stötdämpare", "Stötdämpare bakre"]),
])
def test_search_terms_normalised_first_original_last(name, expected):
    assert search_terms(name) == expected


@pytest.mark.parametrize("name,expected", [
    ("Oljefilter", ["Oljefilter"]),          # differs only in case: one term, the original
    ("  Luftfilter ", ["Luftfilter"]),
    ("bromsskiva", ["bromsskiva"]),
    ("Bakgrundsbelysning", ["Bakgrundsbelysning"]),
    ("fram x2", ["fram x2"]),               # nothing left after normalising: original only
])
def test_search_terms_single_item(name, expected):
    assert search_terms(name) == expected


@pytest.mark.parametrize("name", ["", "   ", None])
def test_search_terms_empty(name):
    assert search_terms(name) == []


# ── Task 6b fix round 1: compound axle forms, Latin look-alikes, axle-specific parts ──

@pytest.mark.parametrize("word,axle", [
    ("fram", "fram"), ("front", "fram"), ("framaxel", "fram"), ("framaxeln", "fram"),
    ("framhjul", "fram"), ("framhjulet", "fram"), ("framre", "fram"), ("främre", "fram"),
    ("Fram-", "fram"), ("FRAMAXELN", "fram"),
    ("bak", "bak"), ("rear", "bak"), ("back", "bak"), ("bakaxel", "bak"), ("bakaxeln", "bak"),
    ("bakhjul", "bak"), ("bakhjulet", "bak"), ("bakre", "bak"), ("Bak-", "bak"),
])
def test_axle_forms_recognised(word, axle):
    assert parts_matcher.placement_of(f"Bromsbelägg {word}") == ({axle}, set())


@pytest.mark.parametrize("word,side", [
    ("vä", "vä"), ("vänster", "vä"), ("vänstra", "vä"), ("left", "vä"), ("LH", "vä"),
    ("hö", "hö"), ("höger", "hö"), ("högra", "hö"), ("right", "hö"), ("rh", "hö"),
])
def test_side_forms_recognised(word, side):
    assert parts_matcher.placement_of(f"Bromsskiva {word}") == (set(), {side})


@pytest.mark.parametrize("text", [
    "Bakgrund", "höjd", "väl", "Framrutan", "Bakrutan", "Framruta", "fa", "ra", "va", "ha",
    "Framtida", "Backspegel", "Bakljus",
])
def test_non_placement_tokens_not_parsed(text):
    assert parts_matcher.placement_of(text) == (set(), set())


def test_fram_slash_bak_states_both():
    assert parts_matcher.placement_of("Bromsbelägg fram/bak") == ({"fram", "bak"}, set())
    assert parts_matcher.placement_of("Fram-/bakaxel") == ({"fram", "bak"}, set())


@pytest.mark.parametrize("front,rear", [
    ("Bromsbelägg framaxeln", "Bromsbelägg bakaxeln"),
    ("Bromsbelägg framhjul", "Bromsbelägg bakhjul"),
    ("Bromsbelägg Fram-", "Bromsbelägg Bak-"),
])
def test_compound_axle_forms_filter_candidates(front, rear):
    f, r = pcand(front), pcand(rear)
    picker = recording()
    assert choose({"name": "Bromsbelägg framaxel", "qty": 1}, [r, f], RULES, picker) is f
    assert picker.seen == [[f]]
    picker = recording()
    assert choose({"name": "Bromsbelägg bakhjul", "qty": 1}, [f, r], RULES, picker) is r
    assert picker.seen == [[r]]


def test_reviewer_case_no_axle_line_with_compound_forms_is_none():
    cands = [pcand("Bromsbelägg framaxeln"), pcand("Bromsbelägg bakaxeln")]
    assert parts_matcher._placement_filter("Bromsbelägg", cands) == []
    cands = [pcand("Bromsbelägg framhjul"), pcand("Bromsbelägg bakhjul")]
    assert parts_matcher._placement_filter("Bromsbelägg", cands) == []
    # Same for a part that is not axle-specific: disagreeing candidates are ambiguous.
    cands = [pcand("Torkarblad framaxeln"), pcand("Torkarblad bakaxeln")]
    assert parts_matcher._placement_filter("Torkarblad", cands) == []


def test_side_compound_both_states_neither():
    picker = recording()
    cands = [pcand("Bromsskiva fram", "vä/hö"), pcand("Bromsskiva fram rh")]
    assert choose({"name": "Bromsskiva fram höger", "qty": 1}, cands, RULES, picker) is cands[1]
    assert picker.seen == [[cands[1]]]


@pytest.mark.parametrize("text", [
    "ProMéister belägg", "Promeıster belägg", "PróMeister", "Pro.Meister", "Pro Meister", "Pro-Meister",
    "PRO_MEISTER", "P r o m e i s t e r", "Pro/Meister", "ProMeíster",
])
def test_latin_promeister_lookalikes_forbidden(text):
    assert parts_matcher.is_forbidden(pcand(text), RULES)
    assert parts_matcher.is_forbidden(pcand("Belägg", brand=text), RULES)
    rules_cleared = Rules(("Bosch",), (), 10000, "approve")
    assert rank_candidates([pcand(text)], rules_cleared) == []


def test_promotion_meister_is_not_promeister():
    assert parts_matcher._norm("Promotion Meister") == "promotionmeister"
    assert not parts_matcher.is_forbidden(pcand("Promotion Meister"), RULES)


def test_norm_strips_marks_and_punctuation():
    assert parts_matcher._norm("Bosch GmbH") == "boschgmbh"
    assert parts_matcher._norm(" BOSCH. ") == "bosch"
    assert parts_matcher._norm("Pró-Meıster​") == "promeister"


def test_bosch_still_ranks_with_strict_norm():
    a, b, c, d = pcand("Oljefilter Hengst"), pcand("x", brand="BOSCH "), pcand("Bosch GmbH filter"), \
        pcand("Oljefilter, Bosch.")
    assert rank_candidates([a, b, c, d], RULES) == [b, c, d, a]


@pytest.mark.parametrize("name", [
    "Bromsbelägg", "Bromsskivor", "Bromsskiva x2", "Bromsklotsar", "Bromsklossar", "Bromsok",
    "Bromstrumma", "Stötdämpare", "Fjäder", "Spiralfjädrar", "Hjullager", "Länkarm",
    "Krängningshämmare länk", "Spindelled", "Styrled", "Hjulnav", "Drivaxel",
])
def test_axle_specific_part_without_axle_is_blocked(name):
    picker = recording()
    # Even a single candidate with no stated axle is not enough: the owner must name the axle.
    assert choose({"name": name, "qty": 1}, [pcand(f"{name} Bosch")], RULES, picker) is None
    assert choose({"name": name, "qty": 1}, [pcand(f"{name} Bosch", "fram")], RULES, picker) is None
    assert picker.seen == []


@pytest.mark.parametrize("name", ["Bromsbelägg fram", "Stötdämpare bak", "Hjullager framaxel vä"])
def test_axle_specific_part_with_axle_proceeds(name):
    axle = "bak" if "bak" in name else "fram"
    side = " vä" if " vä" in name else ""
    good = pcand("Del Bosch", axle + side)
    assert choose({"name": name, "qty": 1}, [good], RULES, recording()) is good


@pytest.mark.parametrize("name", ["Oljefilter", "Luftfilter", "Torkarblad", "Tändstift", "Pollenfilter"])
def test_not_axle_specific_part_without_axle_proceeds(name):
    good = pcand(f"{name} Bosch")
    assert choose({"name": name, "qty": 1}, [good], RULES, recording()) is good


@pytest.mark.parametrize("name,expected", [
    ("Bromsbelägg framaxeln", ["bromsbelägg", "Bromsbelägg framaxeln"]),
    ("Stötdämpare Bak- x2", ["stötdämpare", "Stötdämpare Bak- x2"]),
    ("Bromsskivor bakhjul LH", ["bromsskiva", "Bromsskivor bakhjul LH"]),
])
def test_search_terms_drop_new_placement_forms(name, expected):
    assert search_terms(name) == expected


# ── Final review: fluid candidates, brand preference enforced ─────────────────

def test_fluid_candidate_dropped_even_when_line_is_not_a_fluid():
    oil = pcand("Castrol Motorolja Edge 5W-30 4L")
    picker = recording()
    assert choose({"name": "Motorservice", "qty": 1}, [oil], RULES, picker) is None
    assert picker.seen == []


@pytest.mark.parametrize("title,brand", [
    ("Castrol Motorolja Edge 5W-30 4L", ""), ("Bromsvätska DOT4 1L", ""),
    ("Kylarvätska G12", "Bosch"), ("Delar", "Castrol Motorolja"),
])
def test_fluid_candidates_never_reach_picker(title, brand):
    fluid, good = pcand(title, brand=brand), pcand("Bosch Oljefilter, Filterinsats")
    picker = recording()
    assert choose(LINE, [fluid, good], RULES, picker) is good
    assert picker.seen == [[good]]
    assert rank_candidates([fluid], RULES) == []


@pytest.mark.parametrize("title", ["Bosch Oljefilter, Filterinsats", "Luftfilter MANN-FILTER",
                                   "Oil filter Hengst"])
def test_filter_candidates_still_allowed(title):
    good = pcand(title)
    assert choose(LINE, [good], RULES, recording()) is good


def test_preferred_subset_offered_first():
    other, bosch = pcand("Oljefilter Hengst"), pcand("Bosch Oljefilter")
    picker = recording((0, 0.95))
    assert choose(LINE, [other, bosch], RULES, picker) is bosch
    assert picker.seen == [[bosch]]


def test_falls_back_to_all_when_preferred_declined():
    other, bosch = pcand("Oljefilter Hengst"), pcand("Bosch Oljefilter")
    seen = []

    def picker(line, ranked):
        seen.append(list(ranked))
        return (0, 0.2) if len(seen) == 1 else (1, 0.9)

    assert choose(LINE, [other, bosch], RULES, picker) is other
    assert seen == [[bosch], [bosch, other]]


def test_no_preferred_candidates_single_call():
    a, b = pcand("Oljefilter Hengst"), pcand("Oljefilter Purflux")
    picker = recording((1, 0.9))
    assert choose(LINE, [a, b], RULES, picker) is b
    assert picker.seen == [[a, b]]


def test_all_preferred_single_call():
    a, b = pcand("Bosch Oljefilter A"), pcand("Bosch Oljefilter B")
    picker = recording((0, 0.1))
    assert choose(LINE, [a, b], RULES, picker) is None
    assert picker.seen == [[a, b]]


def test_preference_never_shows_filtered_candidates_and_calls_at_most_twice():
    bosch, other = pcand("Bosch Oljefilter"), pcand("Oljefilter Hengst")
    banned = [pcand("ProMeister Oljefilter"), pcand("Castrol Motorolja 5W-30"),
              pcand("Bosch Оljefilter")]
    picker = recording((0, 0.1))
    assert choose(LINE, banned + [other, bosch], RULES, picker) is None
    assert picker.seen == [[bosch], [bosch, other]]


def test_pick_system_mentions_brand_order_and_same_part():
    text = parts_matcher.PICK_SYSTEM.lower()
    assert "already ordered by brand preference" in text
    assert "same part" in text


# ── Follow-up: English fluid keywords match whole words only ─────────────────

@pytest.mark.parametrize("name", [
    "Ignition coil Bosch", "Bosch Ignition coil", "Coil", "Boil", "Foil", "Soil", "Tändspole coil-on-plug",
])
def test_english_keywords_need_whole_words(name):
    assert not is_fluid(name)


def test_ignition_coil_candidate_allowed():
    coil = pcand("Ignition coil Bosch")
    assert choose({"name": "Tändspole", "qty": 1}, [coil], RULES, recording()) is coil


@pytest.mark.parametrize("name", [
    "Motor oil 5W-30", "Engine Oil", "Oils", "Gear oil", "Motorolja", "Växellådsolja", "Bromsvätska DOT4",
    "Kylarvätska", "AdBlue 10L", "AdBlue10L", "Brake fluid", "Washer fluid", "Fluids", "Grease",
    "Coolant", "Antifreeze", "Lubricant", "Smörjfett", "Kullagerfett", "Fett",
])
def test_fluids_still_detected_after_word_boundaries(name):
    assert is_fluid(name)


def test_fettinsats_is_still_treated_as_a_fluid():
    # Documented decision (fail closed): Swedish "fett" stays a substring match, so
    # "Fettinsats" (grease insert/cartridge? not certain) keeps blocking.
    assert is_fluid("Fettinsats")


# ── Wave 2 Task 11: search synonyms (Pollenfilter = Kupéfilter) ───────────────

def test_search_synonyms_constant():
    assert SEARCH_SYNONYMS == {"pollenfilter": ("kupéfilter", "kupefilter"),
                               "kupefilter": ("kupéfilter",), "kupéfilter": ("kupefilter",)}


def test_pollenfilter_searches_kupefilter_first_original_last():
    terms = search_terms("Pollenfilter")
    assert terms == ["kupéfilter", "kupefilter", "Pollenfilter"]
    assert terms[0] == "kupéfilter" and terms[-1].casefold() == "pollenfilter"


@pytest.mark.parametrize("name,expected", [
    ("Pollenfilter fram x2", ["kupéfilter", "kupefilter", "pollenfilter", "Pollenfilter fram x2"]),
    ("POLLENFILTER 2 st", ["kupéfilter", "kupefilter", "pollenfilter", "POLLENFILTER 2 st"]),
    ("Kupéfilter", ["kupefilter", "Kupéfilter"]),
    ("Kupefilter x1", ["kupéfilter", "kupefilter", "Kupefilter x1"]),
    ("pollenfilter aktivt kol", ["kupéfilter aktivt kol", "kupefilter aktivt kol", "pollenfilter aktivt kol"]),
    ("Pollenfilter aktivt kol fram", ["kupéfilter aktivt kol", "kupefilter aktivt kol",
                                      "pollenfilter aktivt kol", "Pollenfilter aktivt kol fram"]),
])
def test_synonyms_after_quantity_and_placement_removal(name, expected):
    assert search_terms(name) == expected


def test_decomposed_accent_still_matches_synonym():
    decomposed = unicodedata.normalize("NFD", "Kupéfilter")
    assert search_terms(decomposed)[0] == "kupefilter"


@pytest.mark.parametrize("name,expected", [
    ("Oljefilter", ["Oljefilter"]),
    ("Bromsskivor fram x2", ["bromsskiva", "Bromsskivor fram x2"]),
    ("Kupé lampa", ["Kupé lampa"]),           # only whole words are replaced
    ("Pollenfiltret", ["Pollenfiltret"]),
])
def test_unrelated_words_unaffected_by_synonyms(name, expected):
    assert search_terms(name) == expected


@pytest.mark.parametrize("name", ["Pollenfilter", "Pollenfilter fram x2", "Kupéfilter",
                                  "kupefilter kupéfilter", "pollenfilter aktivt kol"])
def test_synonym_terms_unique_and_non_empty(name):
    terms = search_terms(name)
    assert all(t and t == t.strip() for t in terms)
    assert len({t.casefold() for t in terms}) == len(terms)
