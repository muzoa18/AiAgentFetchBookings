import json
from types import SimpleNamespace

import pytest

import parts_matcher
from parts_matcher import PICK_MODEL, choose, is_fluid, make_claude_picker, rank_candidates
from rules import Rules

RULES = Rules(brand_prefer=("Bosch", "Mann"), brand_forbid=("ProMeister",), ceiling_sek=10000, mode="approve")
LINE = {"name": "Bromsbelägg fram", "qty": 1}


def cand(title, brand, price=100, ref="x"):
    return {"title": title, "brand": brand, "price_sek": price, "url_or_id": ref}


@pytest.mark.parametrize("name", [
    "Motorolja 5W-30", "Bromsvätska DOT4", "Kylarvätska G12", "Spolarvätska", "Växellådsolja 75W-90",
    "Servoolja", "AdBlue 10L", "Brake fluid DOT 4", "Engine oil 5W-30", "Coolant", "Frostskydd", "Olja",
    "Motorolja och oljefilter",
])
def test_fluids_detected(name):
    assert is_fluid(name)


@pytest.mark.parametrize("name", [
    "Oljefilter", "Oil filter", "Bromsskivor fram", "Bromsskiva", "Bromsbelägg", "Pollenfilter", "Tändstift",
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
    assert seen["ranked"] == [bosch, other]


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
    assert "Bosch Bromsbelägg" in prompt and "Febi Bromsskiva" in prompt and "Bromsbelägg fram" in prompt


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
