"""Build edu_acikders_ulakbim from ULAKBİM open-courseware page content.

Raw: s3://.../selman/reference/acikders-ulakbim/metadata/acikders_ulakbim_content.jsonl
Fields: {course_id, course_name, resource_id, resource_type, title, section, text_content, url}

Schema: title = "<course_name> — <title>"; text = text_content.
Dedup: exact (repeated boilerplate pages). No MinHash (short, mostly unique content).

Usage:
    python builders/build_edu_acikders_ulakbim.py --work /scratch/acikders
"""
from __future__ import annotations

import argparse
import json
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

RAW_URI = f"{SOURCE_ROOT}/reference/acikders-ulakbim/metadata/acikders_ulakbim_content.jsonl"
DS_NAME = "edu_acikders_ulakbim"
MIN_CHARS = 80


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    args = ap.parse_args()
    work = Path(args.work)

    local = s3_cp(RAW_URI, work / "acikders_ulakbim_content.jsonl")
    records, seen, n_skip = [], set(), 0
    for line in local.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        body = clean_text(d.get("text_content"))
        if len(body) < MIN_CHARS:
            n_skip += 1
            continue
        h = hash(body)
        if h in seen:
            n_skip += 1
            continue
        seen.add(h)
        course = clean_text(d.get("course_name"))
        title = clean_text(d.get("title"))
        full_title = f"{course} — {title}" if course and title else (title or course or "Açık Ders")
        records.append({"title": full_title, "text": body})
    print(f"  kept={len(records)} skipped(short/dup)={n_skip}")

    out_dir = work / "out" / DS_NAME
    save_dataset(records, out_dir)
    verify_dataset(out_dir)
    print(f"  built -> {out_dir}")


if __name__ == "__main__":
    main()
