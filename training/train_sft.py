"""LoRA supervised fine-tuning of Qwen2.5-0.5B-Instruct on the intent dataset.

Sized for a 4 GB laptop GPU (RTX 3050): bf16 base weights (~1 GB), LoRA adapters on every linear
layer, gradient checkpointing, small batches with gradient accumulation. Loss is computed only on
the assistant's JSON reply (prompt-completion format), not on the prompt.

    training/.venv/Scripts/python training/train_sft.py              # LoRA, bf16
    training/.venv/Scripts/python training/train_sft.py --qlora      # 4-bit base, if LoRA runs out of memory

Output: training/outputs/adapter (LoRA weights + tokenizer). Next: merge_and_export.py.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(HERE / ".hf_cache"))   # keep downloads inside training/

import torch  # noqa: E402
from datasets import load_dataset  # noqa: E402
from peft import LoraConfig  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig  # noqa: E402
from trl import SFTConfig, SFTTrainer  # noqa: E402


def to_prompt_completion(row: dict) -> dict:
    """[system, (history...), user, assistant] -> prompt = all but the reply, completion = the reply.
    SFTTrainer then masks the prompt tokens so only the JSON answer is learned."""
    return {"prompt": row["messages"][:-1], "completion": row["messages"][-1:]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-model", default="Qwen/Qwen2.5-0.5B-Instruct")
    ap.add_argument("--data", default=str(HERE / "data"))
    ap.add_argument("--out", default=str(HERE / "outputs"))
    ap.add_argument("--epochs", type=float, default=3)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--batch-size", type=int, default=4)
    ap.add_argument("--grad-accum", type=int, default=4)
    ap.add_argument("--max-len", type=int, default=512)
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--qlora", action="store_true", help="load the base model in 4-bit (needs bitsandbytes)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--max-steps", type=int, default=-1, help="stop early (smoke test)")
    ap.add_argument("--resume", action="store_true", help="continue from the last checkpoint in --out")
    args = ap.parse_args()

    if not torch.cuda.is_available():
        print("WARNING: CUDA not available, training on CPU will be very slow.")
    bf16 = torch.cuda.is_available() and torch.cuda.is_bf16_supported()
    dtype = torch.bfloat16 if bf16 else torch.float32

    data = load_dataset("json", data_files={"train": f"{args.data}/train.jsonl",
                                            "validation": f"{args.data}/val.jsonl"})
    data = data.map(to_prompt_completion, remove_columns=data["train"].column_names)

    tokenizer = AutoTokenizer.from_pretrained(args.base_model)
    quant = None
    if args.qlora:
        quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                   bnb_4bit_compute_dtype=dtype, bnb_4bit_use_double_quant=True)
    model = AutoModelForCausalLM.from_pretrained(args.base_model, dtype=dtype, quantization_config=quant,
                                                 attn_implementation="sdpa")

    lora = LoraConfig(r=args.lora_r, lora_alpha=2 * args.lora_r, lora_dropout=0.05,
                      target_modules="all-linear", task_type="CAUSAL_LM")

    config = SFTConfig(
        output_dir=f"{args.out}/checkpoints",
        num_train_epochs=args.epochs,
        max_steps=args.max_steps,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_steps=0.03,                   # a float < 1 is a ratio of total steps
        max_length=args.max_len,
        completion_only_loss=True,           # loss on the JSON reply only
        gradient_checkpointing=True,
        bf16=bf16,
        logging_steps=10,
        eval_strategy="steps", eval_steps=100,
        save_strategy="steps", save_steps=100, save_total_limit=2,
        load_best_model_at_end=True, metric_for_best_model="eval_loss",
        dataloader_num_workers=0,            # Windows-friendly
        report_to="none",
        seed=args.seed,
    )
    trainer = SFTTrainer(model=model, args=config, train_dataset=data["train"],
                         eval_dataset=data["validation"], processing_class=tokenizer, peft_config=lora)
    trainer.model.print_trainable_parameters()
    trainer.train(resume_from_checkpoint=True if args.resume else None)
    print("final eval:", trainer.evaluate())

    adapter_dir = f"{args.out}/adapter"
    trainer.save_model(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)
    if torch.cuda.is_available():
        print(f"peak VRAM: {torch.cuda.max_memory_allocated() / 2**30:.2f} GiB")
    print(f"LoRA adapter saved to {adapter_dir}. Next: python training/merge_and_export.py")


if __name__ == "__main__":
    main()
