"""Build acad_econbiz_abstracts from EconBiz bibliographic records (Turkish abstracts).

Raw: s3://.../selman/reference/econbiz/metadata/econbiz_items.jsonl
Fields: {record_id, title, authors, year, abstract, ...}  (abstract present on a subset)

Schema: title = title; text = abstract. Keeps only records with a non-empty abstract and
reports the fill rate (drop the whole set if too sparse). Dedup: exact only.

Usage:
    python builders/build_acad_econbiz_abstracts.py --work /scratch/econbiz
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

RAW_URI = f"{SOURCE_ROOT}/reference/econbiz/metadata/econbiz_items.jsonl"
DS_NAME = "acad_econbiz_abstracts"
MIN_CHARS = 80
INTL_NAME = "acad_econbiz_abstracts_intl"

# Strong Turkish-language signals (ö/ü excluded — shared with German)
TR_CHARS = set("çğışÇĞİŞ")
# Turkey/Turkish topical relevance terms
TURKEY_TERMS = (
    "turkey", "turkish", "türk", "türkiye", "ottoman", "osmanlı", "istanbul",
    "ankara", "izmir", "anatolia", "anadolu", "bosphorus", "boğaz", "marmara", "aegean",
)


def is_turkish(s: str) -> bool:
    return any(c in TR_CHARS for c in s)


def turkey_related(*parts) -> bool:
    blob = " ".join(p for p in parts if p).lower()
    return any(term in blob for term in TURKEY_TERMS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    args = ap.parse_args()
    work = Path(args.work)

    local = s3_cp(RAW_URI, work / "econbiz_items.jsonl")
    total = 0
    main_recs, intl_recs, seen = [], [], set()
    for line in local.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        total += 1
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        abstract = clean_text(d.get("abstract"))
        # strip leading source-label artifacts like "Turkish Abstract: ..."
        low = abstract.lower()
        for pfx in ("turkish abstract:", "english abstract:", "abstract:", "özet:", "öz:"):
            if low.startswith(pfx):
                abstract = abstract[len(pfx):].strip()
                break
        if len(abstract) < MIN_CHARS:
            continue
        h = hash(abstract)
        if h in seen:
            continue
        seen.add(h)
        title = clean_text(d.get("title")) or "EconBiz kaydı"
        rec = {"title": title, "text": abstract}
        # Turkish abstracts, or English/other abstracts about Turkey -> main set;
        # off-topic non-Turkish -> separated intl set.
        if is_turkish(abstract) or turkey_related(title, abstract, d.get("subjects"), d.get("authors")):
            main_recs.append(rec)
        else:
            intl_recs.append(rec)
    fill = 100 * (len(main_recs) + len(intl_recs)) / max(1, total)
    print(f"  total_records={total}  with_abstract={len(main_recs)+len(intl_recs)}  fill_rate={fill:.1f}%")
    print(f"  main(TR or Turkey-related)={len(main_recs)}  intl(off-topic non-TR)={len(intl_recs)}")

    out_dir = work / "out" / DS_NAME
    save_dataset(main_recs, out_dir)
    verify_dataset(out_dir)
    print(f"  built -> {out_dir}")
    if intl_recs:
        intl_dir = work / "out" / INTL_NAME
        save_dataset(intl_recs, intl_dir)
        verify_dataset(intl_dir)
        print(f"  built -> {intl_dir}")


if __name__ == "__main__":
    main()
