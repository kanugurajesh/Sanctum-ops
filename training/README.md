# Optional: a fine-tuned local intent parser (Qwen2.5-0.5B-Instruct)

Groq stays the default and Gemini stays available. This folder builds a **third, optional provider**
(`LLM_PROVIDER=qwen`): a small model fine-tuned (SFT, LoRA) to emit the same `ParsedIntent` JSON. It runs
locally through Ollama.

The model only proposes an intent, like the other providers. Permissions, tenant isolation, row caps,
previews and the audit log are enforced in code and don't depend on which model parses. A wrong parse is
still refused or shown in a preview.

What it changes:

| | Groq (default) | Local Qwen |
|---|---|---|
| Query text leaves the network | yes | no |
| Cost / rate limits | free tier ≈ 3 req/min | none |
| Prompt per request | ~2.4k tokens (rules + 16 few-shots) | ~200 tokens (format learned in training) |
| Eval (79 queries, fresh seed) | 79/79 outcomes, 0 false allows | 79/79 outcomes, 0 false allows (all held out of training) |
| Latency | API round trip | median 0.22 s on an RTX 3050 Laptop |

Nothing here is needed for `docker compose up`. Training code, data and weights stay in `training/`
(gitignored), outside the API's Docker build context.

## Hardware this was sized for

RTX 3050 Laptop (4 GB VRAM), 16 GB RAM, Windows 11. LoRA on the bf16 base model fits in about 3 GB of
VRAM. A full fine-tune (~8 GB) does not fit. If you run out of memory, use `--qlora` (4-bit base,
`pip install bitsandbytes`) or `--batch-size 2 --grad-accum 8`. Total disk use is about 7 GB: torch
plus the model cache, adapter and merged weights.

## Steps (PowerShell, from the repo root)

### 1. Training environment (once)

```powershell
py -3.11 -m venv training\.venv
training\.venv\Scripts\python -m pip install torch --index-url https://download.pytorch.org/whl/cu128
training\.venv\Scripts\python -m pip install -r training\requirements.txt
```

### 2. Build the dataset (uses the backend's environment)

```powershell
backend\.venv\Scripts\python training\build_dataset.py
```

This writes `training/data/{train,val}.jsonl` and `meta.json` (counts per action, language and source,
plus the backend commit). Each row uses `QWEN_SYSTEM_PROMPT` and the same history layout as
`QwenParser`, and is validated against `ParsedIntent`. **Every query in `backend/eval/queries.yaml` is
held out.**

Optional: label your own extra queries (one per line) with the Groq teacher. This uses your
`GROQ_API_KEY` and is capped by `--max-calls`:

```powershell
backend\.venv\Scripts\python training\build_dataset.py --extra-queries my_queries.txt --teacher groq --max-calls 200
```

### 3. Train (≈45 min on an RTX 3050 Laptop)

```powershell
training\.venv\Scripts\python training\train_sft.py
```

The output goes to `training/outputs/adapter`. Training prints the eval loss and peak VRAM. To try
another base model, pass `--base-model Qwen/Qwen3-0.6B`. Use `--max-steps 12` for a one-minute
memory smoke test. If a run is interrupted, `--resume` continues from the last checkpoint (saved every
100 steps). Training needs a few GB of free RAM, so close or stop heavy apps and containers first.

Measured on the reference machine (3,627 train / 176 val rows, 3 epochs, 681 steps): peak VRAM
2.08 GiB, final eval loss 0.015. With nothing else on the GPU a run takes about 45 minutes; with the
GPU and RAM shared with other apps it took about 85. The full-pipeline eval in step 6 is the real
measure.

### 4. Merge, convert to GGUF, then check offline

Ollama 0.40.x can't import Qwen2 safetensors (`unsupported MLX architecture: model
"Qwen2ForCausalLM"`), so the merged model is converted to GGUF with llama.cpp's converter (one-time
setup):

```powershell
git clone --depth 1 https://github.com/ggml-org/llama.cpp training\.llama.cpp
training\.venv\Scripts\python -m pip install .\training\.llama.cpp\gguf-py sentencepiece
```

```powershell
training\.venv\Scripts\python training\merge_and_export.py --llama-cpp training\.llama.cpp
training\.venv\Scripts\python training\check_model.py
training\.venv\Scripts\python training\check_model.py --model Qwen/Qwen2.5-0.5B-Instruct   # untuned baseline
```

`check_model.py` reports exact-JSON and action accuracy on the validation split, and action accuracy
on the held-out eval queries per language.

### 5. Serve with Ollama

`ollama create` must talk to an Ollama **running on Windows**, because it reads the merged weights
from disk. Another Ollama in Docker (for example a `documind-ollama-1` container) already on port
11434 can't see those files. Pick one:

- **Free port 11434:** `docker stop documind-ollama-1`, then start the Ollama app.
- **Use another port:** in a separate terminal run `$env:OLLAMA_HOST="127.0.0.1:11435"; ollama serve`,
  and set `$env:OLLAMA_HOST="127.0.0.1:11435"` in the terminal where you run `ollama create`. Then set
  `QWEN_BASE_URL=http://host.docker.internal:11435/v1` in `.env`.

```powershell
ollama create hms-intent-qwen -f training\outputs\Modelfile
ollama run hms-intent-qwen "Give 20% discount on all bills from last month"   # sanity check, no system prompt
```

### 6. Switch the app to it

In `.env`:

```
LLM_PROVIDER=qwen
# QWEN_BASE_URL=http://host.docker.internal:11434/v1   (default)
# QWEN_MODEL=hms-intent-qwen                           (default)
```

Then run `docker compose up -d api` and compare the providers on the real pipeline (policy + DB):

```powershell
docker compose exec api python -m eval.run_eval --out eval/results-qwen.json
docker compose exec api python -m eval.run_eval --lang te,hi --out eval/results-qwen-te-hi.json
```

**`false_allow_rate` must stay 0** whichever model parses, because the policy is enforced in code.
Switch back at any time with `LLM_PROVIDER=groq`.

The eval's expected outcomes assume a freshly seeded database. Run `python -m app.seed` first if demo
writes were executed. For example, if last month's bills already carry 20% or Ravi's `inventory.update`
was revoked, the correct intents get "nothing to change" or `no_permission` and count as outcome misses.

Measured on the reference machine (Ollama 0.40.2, q8_0 GGUF, RTX 3050 Laptop, freshly seeded database):

| | local Qwen (final model) |
|---|---|
| intent (action) accuracy | **79/79** |
| outcome accuracy | **79/79** (en 59/59, hi 9/9, te 11/11) |
| false allows / false denies | **0 / 0** |
| latency | median 0.22 s (first call ≈12–25 s while Ollama loads the model) |

All 79 eval queries were held out of training. How the training data got here (each round measured
with the same eval):

1. **First model, 78/79.** "what permissions do I have?" came out without `target_self` and was refused.
   Fix: more self-reference phrasings ("show my access", "naa permissions enti?") with contrasting
   named-person ones. The eval's own wording stayed held out.
2. **Second model, 78/79.** Vague writes such as "update quantities" were filled in with an invented
   `scope_all` and a value of 10, giving a 650-row preview. The preview and typed-count guard would still
   stop it, but the spec says "clarify, don't guess". Fix: vague write requests with no scope and no
   value ("change the stock", "update prices", "apply a discount", in all four language forms), so the
   code asks.
3. **Final model, 79/79.** "update quantities", "update prices" and "give a discount" now come out
   without invented fields.

The held-out set is small (79 queries), so 79/79 doesn't mean the model never misreads. A misread
still meets the same policy checks, previews and typed-count confirmations as with Groq.

**Why `QwenParser` uses JSON mode, not JSON-schema mode.** Ollama's grammar for the `ParsedIntent` schema
blocks optional nested fields, which pushes the model into wrong ones: "pichhle mahine ke saare bills pe 25
percent discount de do" came out as `{"target_user_name": "Pichhale Machine Shop"}`. Unconstrained, it
gives the correct intent. JSON mode still guarantees valid JSON, and every reply is validated against
`ParsedIntent` as for every provider.

## Files

- `build_dataset.py`: templates × slot values (items, categories, staff, dates) in English, Hinglish,
  romanised Telugu and Telugu script, plus clarification follow-ups, the backend's few-shots and the
  optional teacher labels.
- `train_sft.py`: TRL `SFTTrainer`, prompt-completion format (loss on the JSON reply only), LoRA r=16 on
  all linear layers, bf16, gradient checkpointing.
- `merge_and_export.py`: merges the LoRA adapter, converts the result to q8_0 GGUF (`--llama-cpp`), and
  writes an Ollama `Modelfile` with the ChatML template, greedy decoding and the `<|im_end|>` stop
  token.
- `check_model.py`: offline accuracy check with transformers.

If you change `QWEN_SYSTEM_PROMPT` or the intent schema in the backend, rebuild the dataset and retrain.
`meta.json` records the backend commit the dataset was built from.
