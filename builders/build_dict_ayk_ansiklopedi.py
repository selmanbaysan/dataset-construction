"""Build the AYK encyclopedia datasets (Atatürk Ansiklopedisi + Türk Dünyası Ansiklopedisi).

Raw: s3://.../selman/reference/ayk-ansiklopedi/metadata/ayk_*_ansiklopedi.jsonl
Each record already carries fully-extracted clean article text in `item_text`.

Schema: title = record `title`; text = `item_text` (fallback `abstract_text`),
with `source_text` (bibliography) appended when present.

Usage:
    python builders/build_dict_ayk_ansiklopedi.py --work /path/to/scratch [--variant ataturk|turkdunyasi|both]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.build_utils import (  # noqa: E402
    SOURCE_ROOT,
    clean_text,
    s3_cp,
    save_dataset,
    verify_dataset,
)

RAW_PREFIX = f"{SOURCE_ROOT}/reference/ayk-ansiklopedi/metadata"
VARIANTS = {
    "ataturk": ("ayk_ataturk_ansiklopedi.jsonl", "dict_ayk_ataturk_ansiklopedi"),
    "turkdunyasi": ("ayk_turkdunyasi_ansiklopedi.jsonl", "dict_ayk_turkdunyasi_ansiklopedi"),
}
MIN_CHARS = 50


def download(fname: str, work: Path) -> Path:
    local = work / fname
    if local.exists() and local.stat().st_size > 0:
        return local
    return s3_cp(f"{RAW_PREFIX}/{fname}", local)


def build_records(jsonl_path: Path):
    n_in = n_out = n_skip = 0
    seen_titles: set[str] = set()
    for line in jsonl_path.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        n_in += 1
        d = json.loads(line)
        title = clean_text(d.get("title"))
        body = clean_text(d.get("item_text")) or clean_text(d.get("abstract_text"))
        if len(body) < MIN_CHARS:
            n_skip += 1
            continue
        src = clean_text(d.get("source_text"))
        text = body if not src else f"{body}\n\nKaynakça:\n{src}"
        key = (title, body[:200])
        if key in seen_titles:  # exact dup guard
            n_skip += 1
            continue
        seen_titles.add(key)
        n_out += 1
        yield {"title": title or "Ansiklopedi maddesi", "text": text}
    print(f"  read={n_in}  kept={n_out}  skipped={n_skip}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--variant", choices=[*VARIANTS, "both"], default="both")
    args = ap.parse_args()
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)

    variants = list(VARIANTS) if args.variant == "both" else [args.variant]
    for v in variants:
        fname, ds_name = VARIANTS[v]
        print(f"\n=== {ds_name} ===")
        raw = download(fname, work)
        out_dir = work / "out" / ds_name
        save_dataset(list(build_records(raw)), out_dir)
        verify_dataset(out_dir)
        print(f"  built -> {out_dir}")


if __name__ == "__main__":
    main()
