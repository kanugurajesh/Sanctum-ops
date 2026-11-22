"""The optional local Qwen provider, without calling any model: it reuses Groq's request and
validation loop but talks to a local Ollama server with a short prompt and no API key."""
import json

import httpx
import pytest

from app.agent.parser import (
    FEW_SHOTS, INTENT_JSON_SCHEMA, GroqParser, ParserError, QwenParser, Turn, make_parser,
)
from app.agent.prompts import QWEN_SYSTEM_PROMPT, SYSTEM_PROMPT
from app.config import get_settings
from tests.test_parser import FakeResp, ok


def capture(monkeypatch, parser, responses):
    monkeypatch.setattr("app.agent.parser.GENERATION_RETRY_PAUSE_S", 0)
    calls, it = [], iter(responses)

    def fake_post(httpx_mod, body, headers):
        calls.append({"body": body, "headers": headers})
        return next(it)

    monkeypatch.setattr(parser, "_post", fake_post)
    return calls


def test_qwen_request_is_local_short_prompt_no_key(monkeypatch):
    p = QwenParser(base_url="http://localhost:11434/v1/", model="hms-intent-qwen")
    calls = capture(monkeypatch, p, [ok(json.dumps({"action": "inventory.query", "low_stock": True}))])
    history = [Turn("user", "Give Ravi access"), Turn("assistant", "Which access?")]
    intent = p.parse("inventory edit", history)

    assert intent.action.value == "inventory.query" and intent.low_stock
    assert p.url == "http://localhost:11434/v1/chat/completions"
    body, headers = calls[0]["body"], calls[0]["headers"]
    assert headers == {}
    assert "reasoning_effort" not in body
    assert body["model"] == "hms-intent-qwen" and body["temperature"] == 0
    # Plain JSON mode: Ollama's schema grammar derails the fine-tuned model (see QwenParser).
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [
        {"role": "system", "content": QWEN_SYSTEM_PROMPT},          # no few-shots
        {"role": "user", "content": "Give Ravi access"},
        {"role": "assistant", "content": "(assistant asked) Which access?"},
        {"role": "user", "content": "inventory edit"},
    ]


def test_groq_request_is_unchanged(monkeypatch):
    p = GroqParser(api_key="test-key", model="test-model")
    calls = capture(monkeypatch, p, [ok(json.dumps({"action": "help"}))])
    p.parse("hi", [])
    body, headers = calls[0]["body"], calls[0]["headers"]
    assert p.url == "https://api.groq.com/openai/v1/chat/completions"
    assert headers == {"Authorization": "Bearer test-key"}
    assert list(body) == ["model", "messages", "temperature", "reasoning_effort", "response_format"]
    assert body["reasoning_effort"] == "low"
    assert body["response_format"] == {"type": "json_schema",
                                       "json_schema": {"name": "parsed_intent", "schema": INTENT_JSON_SCHEMA}}
    assert body["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert len(body["messages"]) == 1 + 2 * len(FEW_SHOTS) + 1


def test_qwen_invalid_output_retried_then_fails(monkeypatch):
    p = QwenParser(base_url="http://localhost:11434/v1")
    capture(monkeypatch, p, [ok("not json"), ok("{}"), ok('{"action":"no.such"}')])
    with pytest.raises(ParserError, match="rephrase"):
        p.parse("anything", [])


def test_qwen_missing_model_explains_how_to_create_it(monkeypatch):
    p = QwenParser(base_url="http://localhost:11434/v1", model="nope")
    capture(monkeypatch, p, [FakeResp(404, {"error": {"message": "model not found"}})])
    with pytest.raises(ParserError, match="training/README.md"):
        p.parse("anything", [])


def test_qwen_unreachable_server_is_a_friendly_error(monkeypatch):
    def refuse(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", refuse)
    monkeypatch.setattr("app.agent.parser.time.sleep", lambda s: None)
    p = QwenParser(base_url="http://localhost:9/v1")
    with pytest.raises(ParserError, match="not reachable at http://localhost:9/v1.*LLM_PROVIDER=groq"):
        p.parse("anything", [])


@pytest.mark.parametrize("provider,cls", [("groq", GroqParser), ("qwen", QwenParser)])
def test_make_parser_selects_provider(monkeypatch, provider, cls):
    monkeypatch.setattr(get_settings(), "llm_provider", provider)
    assert type(make_parser()) is cls


def test_make_parser_rejects_unknown_provider(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "nope")
    with pytest.raises(ValueError, match="qwen"):
        make_parser()
