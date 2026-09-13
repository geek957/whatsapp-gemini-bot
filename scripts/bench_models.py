#!/usr/bin/env python3
"""Compare Gemini models on the real task: images in, a strictly formatted summary out.

Model choice for this workload is an empirical question, not a matter of version numbers, so
this scores candidates on template adherence rather than prose quality.

Usage:
  python3 scripts/bench_models.py --chat 120363411021022965@g.us --prompt prompts/clinical-summary.md
  python3 scripts/bench_models.py --models gemini-3.8-flash,gemini-2.5-flash --images 3
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import config as config_module
from bot.gemini import GeminiError, GeminiImage, analyze
from bot.providers.base import InboundMessage
from bot.providers.greenapi import GreenApiProvider

DEFAULT_MODELS = (
    "gemini-2.5-flash",
    "gemini-3.5-flash",
    "gemini-3.8-flash",
    "gemini-3.1-pro-preview",
)

# Headings the template demands, in order.
REQUIRED_SECTIONS = (
    "DIAGNOSIS", "CASE HISTORY", "CHIEF COMPLAINTS", "HISTORY OF PRESENTING ILLNESS",
    "PERSONAL HISTORY", "DIET:", "APPETITE:", "SLEEP:", "BOWEL AND BLADDER:",
    "PHYSICAL EXAMINATION", "PULSE", "BLOOD PRESSURE:", "RESPIRATORY RATE:",
    "TEMPERATURE", "ORAL CAVITY", "SYSTEMIC EXAMINATION", "CNS:", "CVS:", "RS:", "P/A:",
)

# Markdown the template forbids.
FORBIDDEN = ("##", "**", "- ", "* ", "|")


def score(text: str) -> dict:
    upper = text.upper()
    present = [s for s in REQUIRED_SECTIONS if s in upper]
    missing = [s for s in REQUIRED_SECTIONS if s not in upper]

    # Sections must appear in template order.
    positions = [upper.find(s) for s in present]
    ordered = positions == sorted(positions)

    return {
        "sections": f"{len(present)}/{len(REQUIRED_SECTIONS)}",
        "missing": missing,
        "ordered": ordered,
        "flags_empty": "[EMPTY]" in upper,
        "markdown_leak": [token for token in FORBIDDEN if token in text],
        "chars": len(text),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", required=True)
    parser.add_argument("--prompt", default="prompts/clinical-summary.md")
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--images", type=int, default=3)
    parser.add_argument("--message-ids", default="", help="comma-separated ids; default: newest images in history")
    parser.add_argument("--out-dir", default="/tmp/bench")
    args = parser.parse_args()

    base = config_module.from_env(dict(os.environ, WHATSAPP_CHAT_IDS=""))
    base.raise_for_errors()
    provider = GreenApiProvider(base)

    if args.message_ids:
        ids = [part.strip() for part in args.message_ids.split(",") if part.strip()]
    else:
        rows = provider._call("POST", "getChatHistory", body={"chatId": args.chat, "count": 40}) or []
        ids = [r["idMessage"] for r in rows if r.get("typeMessage") == "imageMessage"][: args.images]
    if not ids:
        print("no image messages found", file=sys.stderr)
        return 1

    images: list[GeminiImage] = []
    for mid in ids:
        data, mime = provider.download_image(
            InboundMessage(id=mid, chat_id=args.chat, kind="image", mime_type="image/jpeg"))
        images.append(GeminiImage(mime, data))
    print(f"{len(images)} image(s), {sum(len(i.data) for i in images):,} bytes total\n")

    prompt = open(args.prompt, encoding="utf-8").read().strip()
    os.makedirs(args.out_dir, exist_ok=True)

    header = f"{'model':26} {'secs':>6} {'out_tok':>8} {'chars':>7} {'sections':>9} {'order':>6} {'[EMPTY]':>8} {'md_leak':>8}"
    print(header)
    print("-" * len(header))

    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        for budget in (0, -1):  # pro models refuse a zero thinking budget
            cfg = replace(base, gemini_model=model, gemini_thinking_budget=budget,
                          gemini_temperature=0.0)
            started = time.time()
            try:
                result = analyze(cfg, prompt, images)
            except GeminiError as exc:
                if budget == 0:
                    continue  # retry with a dynamic budget
                print(f"{model:26} FAILED  {str(exc)[:70]}")
                break
            elapsed = time.time() - started
            marks = score(result.text)
            print(f"{model:26} {elapsed:6.1f} {result.output_tokens:8d} {marks['chars']:7d} "
                  f"{marks['sections']:>9} {str(marks['ordered']):>6} {str(marks['flags_empty']):>8} "
                  f"{len(marks['markdown_leak']):8d}")
            if marks["missing"]:
                print(f"{'':26} missing: {', '.join(marks['missing'][:6])}")
            path = os.path.join(args.out_dir, f"{model.replace('/', '_')}.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(result.text)
            break
        time.sleep(3)  # Green API and the free Gemini tier both rate limit

    print(f"\nfull outputs written to {args.out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
