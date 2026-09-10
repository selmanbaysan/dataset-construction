"""Build legal_mevzuat from Turkish legislation pages (HTML).

Raw: s3://.../selman/legal/mevzuat/html/<TYPE>__<id>.html   (kanun/kararname/yönetmelik ...)
(The parallel */json/*_madde_tree.json holds the article tree but with null content, so
text comes from the HTML.)

Schema: title = document heading (law name); text = full legislative text.
Dedup: exact (re-crawls / consolidated copies) + light MinHash @0.9 (amended versions
overlap heavily). Laws are otherwise unique.

Usage:
    python builders/build_legal_mevzuat.py --work /scratch/mevzuat [--limit N] [--threshold 0.9]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.build_utils import (  # noqa: E402
    SOURCE_ROOT,
    clean_text,
    minhash_keep_mask,
    save_dataset,
    s3_sync_down,
    verify_dataset,
)

HTML_PREFIX = f"{SOURCE_ROOT}/legal/mevzuat/html"
DS_NAME = "legal_mevzuat"
MIN_CHARS = 120
TYPE_LABELS = {
    "CB_KARARNAME": "Cumhurbaşkanlığı Kararnamesi",
    "KANUN": "Kanun",
    "YONETMELIK": "Yönetmelik",
    "TUZUK": "Tüzük",
}


def parse(html: str, fname: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n").split("\n") if ln.strip()]
    if not lines:
        return None
    text = clean_text("\n".join(lines))
    if len(text) < MIN_CHARS:
        return None
    # heading: join the first few short all-caps-ish lines (the law name)
    head = []
    for ln in lines[:4]:
        head.append(ln)
        if len(" ".join(head)) > 90:
            break
    doc_type = TYPE_LABELS.get(fname.split("__")[0], "")
    title = " ".join(head).strip()
    if doc_type and doc_type.lower() not in title.lower():
        title = f"{title} ({doc_type})" if title else doc_type
    return {"title": title[:300] or "Mevzuat", "text": text}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--threshold", type=float, default=0.9)
    ap.add_argument("--strict", action="store_true", help="verify candidate Jaccard >= threshold before dropping")
    args = ap.parse_args()
    work = Path(args.work)
    html_dir = work / "html"

    print(f"[sync] {HTML_PREFIX}")
    s3_sync_down(HTML_PREFIX, html_dir, include=["*.html"], exclude=["*"])
    files = sorted(html_dir.glob("*.html"))
    if args.limit:
        files = files[: args.limit]
    print(f"[parse] {len(files)} html files")

    records, n_skip, seen = [], 0, set()
    for f in files:
        rec = parse(f.read_text(encoding="utf-8", errors="ignore"), f.name)
        if not rec:
            n_skip += 1
            continue
        h = hash(rec["text"])
        if h in seen:  # exact dedup
            n_skip += 1
            continue
        seen.add(h)
        records.append(rec)
    print(f"  kept={len(records)} skipped(empty/exact-dup)={n_skip}")

    keep = minhash_keep_mask([r["text"] for r in records], threshold=args.threshold, verify=args.strict)
    deduped = [r for r, k in zip(records, keep) if k]
    print(f"  after minhash({args.threshold}): {len(deduped)} (removed {len(records)-len(deduped)})")

    out_dir = work / "out" / DS_NAME
    save_dataset(deduped, out_dir)
    verify_dataset(out_dir)
    print(f"  built -> {out_dir}")


if __name__ == "__main__":
    main()
