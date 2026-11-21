"""Eval harness: runs labelled queries through the REAL pipeline (LLM + policy + DB) and reports
intent accuracy, false-allow rate and false-deny rate. Uses LLM_PROVIDER (groq | gemini). Previews are cancelled, never confirmed,
so running it changes no business data.

    docker compose exec api python -m eval.run_eval [--limit N] [--lang en,te,hi] [--out eval/results.json]

Queries without a `lang` label are English (en); te = Telugu (script or romanised), hi = Hinglish.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

from app.agent.parser import make_parser
from app.config import get_settings
from app.db.engine import tenant_tx
from app.execution import directory
from app.orchestrator import Orchestrator

HERE = Path(__file__).parent
PERMITTED = {"preview", "answer"}


def load_principal(user_id: int):
    from sqlalchemy import text
    from app.db.engine import unscoped_tx
    with unscoped_tx() as c:
        hid = c.execute(text("SELECT hospital_id FROM demo_directory() WHERE user_id = :u"), {"u": user_id}).scalar()
    with tenant_tx(hid) as conn:
        return directory.load_principal(conn, user_id, hid, directory.load_catalog(conn))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(HERE / "results.json"))
    ap.add_argument("--delay", type=float, default=0.0, help="seconds between LLM calls (rate limits)")
    ap.add_argument("--lang", default="", help="comma-separated languages to run, e.g. te,hi (default: all)")
    args = ap.parse_args()

    s = get_settings()
    if not (s.groq_api_key if s.llm_provider.lower() == "groq" else s.gemini_api_key):
        print(f"No API key for LLM_PROVIDER={s.llm_provider}; the eval needs the real model.", file=sys.stderr)
        return 2

    cases = yaml.safe_load((HERE / "queries.yaml").read_text(encoding="utf-8"))
    for c in cases:
        c.setdefault("lang", "en")
    if args.lang:
        langs = {x.strip() for x in args.lang.split(",") if x.strip()}
        cases = [c for c in cases if c["lang"] in langs]
    if args.limit:
        cases = cases[: args.limit]
    orch = Orchestrator(make_parser())

    def run_case(c: dict) -> dict:
        p = load_principal(c["user"])
        t0 = time.time()
        r = orch.handle(p, c["query"])
        elapsed = time.time() - t0
        if r.type == "preview":
            orch.cancel(p, r.preview.operation_id)
        expected = c["expect"] if isinstance(c["expect"], list) else [c["expect"]]
        got_action = (r.intent or {}).get("action")
        code = r.refusal.code if r.refusal else None
        return {
            "query": c["query"], "lang": c["lang"], "user": c["user"], "roles": list(p.roles),
            "expected_action": c["action"], "got_action": got_action,
            "expected": expected, "got": r.type, "expected_code": c.get("code"), "got_code": code,
            "intent_ok": got_action == c["action"],
            "outcome_ok": r.type in expected and (not c.get("code") or c.get("code") == code),
            "false_allow": "refusal" in expected and r.type in PERMITTED,
            "false_deny": bool(set(expected) & PERMITTED) and r.type == "refusal",
            "rate_limited": r.type == "error" and "rate-limited" in r.message,
            "seconds": round(elapsed, 2), "message": r.message[:160],
        }

    def show(i: int, row: dict) -> None:
        mark = "ok " if row["outcome_ok"] and row["intent_ok"] else "XX "
        code = f"/{row['got_code']}" if row["got_code"] else ""
        print(f"{mark}{i:>2}. [{','.join(row['roles'])}] {row['query'][:60]:<60} -> {row['got']}{code}"
              f" (intent {row['got_action']})", flush=True)

    results = []
    for i, c in enumerate(cases, 1):
        results.append(run_case(c))
        show(i, results[-1])
        if args.delay:
            time.sleep(args.delay)

    # Provider rate limits (e.g. Groq free tier ~8k tokens/min) are not model errors: retry them slowly.
    for attempt in range(3):
        retry = [i for i, r in enumerate(results) if r["rate_limited"]]
        if not retry:
            break
        print(f"\nretrying {len(retry)} rate-limited quer{'y' if len(retry) == 1 else 'ies'} (pass {attempt + 1})...")
        for i in retry:
            time.sleep(20)
            results[i] = run_case(cases[i])
            show(i + 1, results[i])

    n = len(results)
    refusals = [r for r in results if "refusal" in r["expected"]]
    permitted = [r for r in results if set(r["expected"]) & PERMITTED]
    summary = {
        "queries": n,
        "intent_accuracy": round(sum(r["intent_ok"] for r in results) / n, 3),
        "outcome_accuracy": round(sum(r["outcome_ok"] for r in results) / n, 3),
        "false_allow_rate": round(sum(r["false_allow"] for r in refusals) / max(len(refusals), 1), 3),
        "false_deny_rate": round(sum(r["false_deny"] for r in permitted) / max(len(permitted), 1), 3),
        "false_allows": [r["query"] for r in results if r["false_allow"]],
        "outcome_accuracy_by_lang": {
            lang: round(sum(r["outcome_ok"] for r in rs) / len(rs), 3)
            for lang in sorted({r["lang"] for r in results})
            for rs in [[r for r in results if r["lang"] == lang]]
        },
        "still_rate_limited": sum(r["rate_limited"] for r in results),
        "provider": s.llm_provider,
        "model": s.groq_model if s.llm_provider.lower() == "groq" else s.gemini_model,
    }
    print("\n" + json.dumps(summary, indent=2))
    Path(args.out).write_text(json.dumps({"summary": summary, "results": results}, indent=2), encoding="utf-8")
    print(f"details written to {args.out}")
    return 0 if summary["false_allow_rate"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
