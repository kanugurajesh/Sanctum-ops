"""Intent parsers. The parser only sees the user's own words and the assistant's own
clarifying questions; it never sees database rows, identities or permissions."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from app.agent.intent import ParsedIntent
from app.agent.prompts import FEW_SHOTS, SYSTEM_PROMPT
from app.config import get_settings


@dataclass
class Turn:
    role: str      # 'user' | 'assistant'
    content: str


class ParserError(Exception):
    pass


class IntentParser(Protocol):
    def parse(self, text: str, history: list[Turn]) -> ParsedIntent: ...


class GeminiParser:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        settings = get_settings()
        self.api_key = api_key or settings.gemini_api_key
        self.model = model or settings.gemini_model
        self._client = None

    def _get_client(self):
        if not self.api_key:
            raise ParserError("GEMINI_API_KEY is not set. Add it to .env and restart the api container.")
        if self._client is None:
            from google import genai
            from google.genai import types
            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(timeout=get_settings().llm_timeout_seconds * 1000),
            )
        return self._client

    def parse(self, text: str, history: list[Turn]) -> ParsedIntent:
        from google.genai import types

        contents: list[types.Content] = []
        for q, a in FEW_SHOTS:
            contents.append(types.Content(role="user", parts=[types.Part(text=q)]))
            contents.append(types.Content(role="model", parts=[types.Part(text=a)]))
        for t in history[-6:]:
            role = "user" if t.role == "user" else "model"
            # Prior assistant turns are wrapped so the model reads them as context, not as JSON examples.
            body = t.content if t.role == "user" else f"(assistant asked) {t.content}"
            contents.append(types.Content(role=role, parts=[types.Part(text=body)]))
        contents.append(types.Content(role="user", parts=[types.Part(text=text[:2000])]))

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0,
            response_mime_type="application/json",
            response_schema=ParsedIntent,
        )
        resp = None
        for attempt in range(3):
            try:
                resp = self._get_client().models.generate_content(model=self.model, contents=contents, config=config)
                break
            except ParserError:
                raise
            except Exception as e:  # network, quota, bad key, unknown model...
                code = getattr(e, "code", None)
                if code == 429 and attempt < 2:      # rate limited (e.g. free tier: 5 req/min)
                    time.sleep(6 * (attempt + 1))
                    continue
                if code == 429:
                    raise ParserError("The language model is rate-limited right now (API quota). "
                                      "Please wait a few seconds and try again.") from e
                if code == 404:
                    raise ParserError(f"Gemini model '{self.model}' was not found. Set GEMINI_MODEL in .env "
                                      "to an available model (e.g. gemini-2.5-flash).") from e
                raise ParserError(f"The language model could not be reached ({type(e).__name__}). "
                                  "Please try again.") from e

        parsed = getattr(resp, "parsed", None)
        if isinstance(parsed, ParsedIntent):
            return parsed
        try:
            return ParsedIntent.model_validate(json.loads(resp.text or "{}"))
        except (ValidationError, json.JSONDecodeError) as e:
            raise ParserError("I couldn't understand that request. Could you rephrase it?") from e


def _inline_refs(schema: dict) -> dict:
    """Pydantic emits $defs/$ref; inline them so any JSON-schema consumer accepts the schema."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k not in ("$defs", "default", "title")}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


INTENT_JSON_SCHEMA = _inline_refs(ParsedIntent.model_json_schema())


class GroqParser:
    """Groq (OpenAI-compatible chat completions), default model openai/gpt-oss-120b.
    Uses JSON-schema structured output; the reply is still validated by Pydantic."""

    URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        settings = get_settings()
        self.api_key = api_key or settings.groq_api_key
        self.model = model or settings.groq_model
        self.timeout = settings.llm_timeout_seconds

    def _messages(self, text: str, history: list[Turn]) -> list[dict]:
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        for q, a in FEW_SHOTS:
            msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
        for t in history[-6:]:
            if t.role == "user":
                msgs.append({"role": "user", "content": t.content})
            else:
                msgs.append({"role": "assistant", "content": f"(assistant asked) {t.content}"})
        msgs.append({"role": "user", "content": text[:2000]})
        return msgs

    def parse(self, text: str, history: list[Turn]) -> ParsedIntent:
        import httpx

        if not self.api_key:
            raise ParserError("GROQ_API_KEY is not set. Add it to .env and restart the api container.")
        body = {
            "model": self.model,
            "messages": self._messages(text, history),
            "temperature": 0,
            "reasoning_effort": "low",
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "parsed_intent", "schema": INTENT_JSON_SCHEMA},
            },
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        resp = None
        for attempt in range(3):
            try:
                resp = httpx.post(self.URL, json=body, headers=headers, timeout=self.timeout)
            except httpx.HTTPError as e:
                if attempt < 2 and isinstance(e, (httpx.ConnectError, httpx.ReadTimeout)):
                    time.sleep(1.0)   # transient network blip: retry
                    continue
                raise ParserError(f"The language model could not be reached ({type(e).__name__}). "
                                  "Please try again.") from e
            if resp.status_code == 429 and attempt < 2:
                # Free tier is ~8k tokens/min; wait out the window Groq reports (bounded).
                try:
                    wait = float(resp.headers.get("retry-after") or 3)
                except ValueError:
                    wait = 3.0
                time.sleep(min(max(wait, 1.0), 20.0))
                continue
            break
        if resp.status_code == 429:
            raise ParserError("The language model is rate-limited right now (API quota). "
                              "Please wait a few seconds and try again.")
        if resp.status_code == 401:
            raise ParserError("GROQ_API_KEY was rejected. Check the key in .env.")
        if resp.status_code == 404:
            raise ParserError(f"Groq model '{self.model}' was not found. Check GROQ_MODEL in .env.")
        if resp.status_code >= 400:
            raise ParserError(f"The language model returned an error ({resp.status_code}). Please try again.")
        try:
            content = resp.json()["choices"][0]["message"]["content"] or "{}"
            return ParsedIntent.model_validate(json.loads(content))
        except (KeyError, IndexError, ValidationError, json.JSONDecodeError) as e:
            raise ParserError("I couldn't understand that request. Could you rephrase it?") from e


def make_parser() -> IntentParser:
    provider = get_settings().llm_provider.lower()
    if provider == "gemini":
        return GeminiParser()
    if provider == "groq":
        return GroqParser()
    raise ValueError(f"Unknown LLM_PROVIDER {provider!r} (use 'groq' or 'gemini')")


class StaticParser:
    """Deterministic parser for tests and the offline eval baseline: maps exact query text
    to a prepared intent. Lets the full flow run without calling the LLM."""

    def __init__(self, mapping: dict[str, ParsedIntent | dict]):
        self.mapping = {k.strip().lower(): (v if isinstance(v, ParsedIntent) else ParsedIntent.model_validate(v))
                        for k, v in mapping.items()}
        self.calls: list[str] = []

    def parse(self, text: str, history: list[Turn]) -> ParsedIntent:
        self.calls.append(text)
        key = text.strip().lower()
        if key not in self.mapping:
            raise ParserError(f"StaticParser has no intent for {text!r}")
        return self.mapping[key]
