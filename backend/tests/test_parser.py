"""Parser robustness without calling the LLM: recovering a valid intent from Groq's
`json_validate_failed` error, and retrying on failed generations."""
import json

import pytest

from app.agent.parser import GroqParser, ParserError, _salvage


class FakeResp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload


def ok(content: str) -> FakeResp:
    return FakeResp(200, {"choices": [{"message": {"content": content}}]})


def validate_failed(generation: str) -> FakeResp:
    return FakeResp(400, {"error": {"code": "json_validate_failed", "failed_generation": generation}})


def test_salvage_takes_the_last_valid_object():
    # Real Groq output for "Approve request 1": a draft with a bad id, then the real answer.
    gen = '{"action":"approvals.approve","approval_request_id":"?"}{"action":"approvals.approve","approval_request_id":1}'
    intent = _salvage(gen)
    assert intent.action.value == "approvals.approve" and intent.approval_request_id == 1


@pytest.mark.parametrize("gen", [None, "", "not json", '{"action":"no.such.action"}'])
def test_salvage_returns_none_when_nothing_validates(gen):
    assert _salvage(gen) is None


def run(monkeypatch, responses):
    monkeypatch.setattr("app.agent.parser.GENERATION_RETRY_PAUSE_S", 0)
    calls = iter(responses)
    p = GroqParser(api_key="test-key", model="test-model")
    monkeypatch.setattr(p, "_post", lambda httpx, body, headers: next(calls))
    return p.parse("anything", [])


def test_json_validate_failed_is_recovered(monkeypatch):
    gen = '{"action":"approvals.approve","approval_request_id":"?"}{"action":"approvals.approve","approval_request_id":2}'
    assert run(monkeypatch, [validate_failed(gen)]).approval_request_id == 2


def test_unparseable_output_is_retried(monkeypatch):
    good = json.dumps({"action": "access.revoke", "target_self": True, "role": "admin"})
    intent = run(monkeypatch, [ok("not json"), ok(good)])
    assert intent.action.value == "access.revoke" and intent.target_self


def test_gives_up_after_three_bad_outputs(monkeypatch):
    with pytest.raises(ParserError):
        run(monkeypatch, [ok("not json"), validate_failed("garbage"), FakeResp(400, {"error": {"code": "output_parse_failed"}})])


def test_output_parse_failed_twice_then_ok(monkeypatch):
    empty = FakeResp(400, {"error": {"code": "output_parse_failed", "failed_generation": ""}})
    good = json.dumps({"action": "bills.query", "scope_all": True})
    assert run(monkeypatch, [empty, empty, ok(good)]).action.value == "bills.query"


def test_other_http_errors_are_not_retried(monkeypatch):
    with pytest.raises(ParserError, match="401|rejected"):
        run(monkeypatch, [FakeResp(401, {"error": {}})])


def test_other_400s_are_retried(monkeypatch):
    good = json.dumps({"action": "bills.query", "scope_all": True})
    intent = run(monkeypatch, [FakeResp(400, {"error": {"code": "something_else"}}), ok(good)])
    assert intent.action.value == "bills.query"
