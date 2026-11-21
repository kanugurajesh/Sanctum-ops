"""Merge the LoRA adapter into the base model, convert it to GGUF, and write an Ollama Modelfile.

    training/.venv/Scripts/python training/merge_and_export.py --llama-cpp training/.llama.cpp
    ollama create hms-intent-qwen -f training/outputs/Modelfile

Recent Ollama builds (0.40.x) send safetensors imports down an MLX path that rejects
Qwen2ForCausalLM, so the merged model is converted to a q8_0 GGUF with llama.cpp's
convert_hf_to_gguf.py (needs a llama.cpp checkout and its gguf-py package, see training/README.md).
Without --llama-cpp the Modelfile points at the safetensors folder (works on Ollama builds that
import Qwen2 safetensors directly).

The Modelfile pins the ChatML template the model was trained with (system / user / assistant turns),
greedy decoding, and the <|im_end|> stop token.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(HERE / ".hf_cache"))

import torch  # noqa: E402
from peft import PeftModel  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

MODELFILE = '''FROM {merged}

TEMPLATE """{{{{- range .Messages }}}}<|im_start|>{{{{ .Role }}}}
{{{{ .Content }}}}<|im_end|>
{{{{ end }}}}<|im_start|>assistant
"""

PARAMETER temperature 0
PARAMETER num_ctx 2048
PARAMETER stop "<|im_end|>"
PARAMETER stop "<|endoftext|>"
'''


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default=str(HERE / "outputs" / "adapter"))
    ap.add_argument("--out", default=str(HERE / "outputs"))
    ap.add_argument("--llama-cpp", type=Path, help="llama.cpp checkout; converts the merged model to GGUF")
    ap.add_argument("--outtype", default="q8_0", help="GGUF type: q8_0 (default), f16, bf16")
    args = ap.parse_args()

    base_name = json.loads(Path(args.adapter, "adapter_config.json").read_text())["base_model_name_or_path"]
    print(f"merging {args.adapter} into {base_name} ...")
    base = AutoModelForCausalLM.from_pretrained(base_name, dtype=torch.bfloat16)   # CPU is fine for 0.5B
    merged = PeftModel.from_pretrained(base, args.adapter).merge_and_unload()

    merged_dir = Path(args.out, "merged").resolve()
    merged.save_pretrained(merged_dir, safe_serialization=True)
    AutoTokenizer.from_pretrained(args.adapter).save_pretrained(merged_dir)

    source = merged_dir
    if args.llama_cpp:
        gguf = Path(args.out, f"hms-intent-qwen-{args.outtype}.gguf").resolve()
        subprocess.run([sys.executable, str(args.llama_cpp / "convert_hf_to_gguf.py"), str(merged_dir),
                        "--outfile", str(gguf), "--outtype", args.outtype], check=True)
        source = gguf

    modelfile = Path(args.out, "Modelfile")
    modelfile.write_text(MODELFILE.format(merged=source.as_posix()), encoding="utf-8")
    print(f"model:     {source}\nModelfile: {modelfile.resolve()}\n\nNext:\n"
          f"  ollama create hms-intent-qwen -f {modelfile.resolve()}")


if __name__ == "__main__":
    main()
