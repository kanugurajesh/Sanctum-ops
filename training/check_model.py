"""Quick offline check of a trained model, before wiring it into the app.

Greedy-decodes every validation row (exact JSON match + action accuracy) and every query in
backend/eval/queries.yaml (action accuracy only: the eval set is held out of training, so this
is the closest local stand-in for the reviewers' hidden set). The full pipeline eval with policy
and database is still `python -m eval.run_eval` with LLM_PROVIDER=qwen.

    training/.venv/Scripts/python training/check_model.py                         # merged model
    training/.venv/Scripts/python training/check_model.py --model Qwen/Qwen2.5-0.5B-Instruct  # untuned baseline
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(HERE / ".hf_cache"))
sys.path.insert(0, str(HERE.parent / "backend"))

import torch  # noqa: E402
import yaml  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

from app.agent.prompts import QWEN_SYSTEM_PROMPT  # noqa: E402


def generate(model, tok, messages: list[dict]) -> str:
    inputs = tok.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt",
                                     return_dict=True).to(model.device)
    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=160, do_sample=False, pad_token_id=tok.eos_token_id)
    return tok.decode(out[0, inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip()


def action_of(text: str) -> str | None:
    try:
        return json.loads(text).get("action")
    except (json.JSONDecodeError, AttributeError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=str(HERE / "outputs" / "merged"))
    ap.add_argument("--val", default=str(HERE / "data" / "val.jsonl"))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForCausalLM.from_pretrained(args.model, dtype=torch.bfloat16).to(device).eval()

    val = [json.loads(l) for l in open(args.val, encoding="utf-8")][: args.limit or None]
    exact = act = 0
    for r in val:
        got = generate(model, tok, r["messages"][:-1])
        want = r["messages"][-1]["content"]
        exact += got == want
        act += action_of(got) == json.loads(want)["action"]
    print(f"validation: {len(val)} rows, exact JSON {exact / len(val):.3f}, action {act / len(val):.3f}")

    cases = yaml.safe_load((HERE.parent / "backend" / "eval" / "queries.yaml").read_text(encoding="utf-8"))
    cases = cases[: args.limit or None]
    per_lang, misses = defaultdict(lambda: [0, 0]), []
    for c in cases:
        got = generate(model, tok, [{"role": "system", "content": QWEN_SYSTEM_PROMPT},
                                    {"role": "user", "content": c["query"]}])
        lang = c.get("lang", "en")
        ok = action_of(got) == c["action"]
        per_lang[lang][0] += ok
        per_lang[lang][1] += 1
        if not ok:
            misses.append(f"  [{lang}] {c['query']}\n      want {c['action']}, got {got}")
    total = sum(v[0] for v in per_lang.values()), sum(v[1] for v in per_lang.values())
    print(f"held-out eval queries: action {total[0]}/{total[1]} = {total[0] / total[1]:.3f}  " +
          "  ".join(f"{k}: {v[0]}/{v[1]}" for k, v in sorted(per_lang.items())))
    if misses:
        print("misses:\n" + "\n".join(misses))


if __name__ == "__main__":
    main()
