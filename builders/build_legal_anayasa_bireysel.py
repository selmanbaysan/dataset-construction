"""Build legal_anayasa_bireysel from Constitutional Court individual-application decisions.

Raw: s3://.../selman/legal/anayasa-mahkemesi/html/bireysel__unknown__<year>-<no>.html

The page is mostly filter/nav chrome; the decision text lives in <div id="Karar">.
Başvuru numarası is recovered from the filename (<year>-<no> -> "<year>/<no>").
MinHash near-dedup STRONG (long standardized legal sections repeat across decisions).

Usage (EC2-friendly):
    python builders/build_legal_anayasa_bireysel.py --work /scratch/anayasa [--limit N] [--threshold 0.8]
"""
from __future__ import annotations

import argparse
import re
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

HTML_PREFIX = f"{SOURCE_ROOT}/legal/anayasa-mahkemesi/html"
DS_NAME = "legal_anayasa_bireysel"
MIN_CHARS = 200
BASVURU_RE = re.compile(r"__([0-9]{4})-([0-9]+)\.html$")
# UI/boilerplate lines that repeat across every decision page
NOISE_LINES = {
    "Kopyala",
    "Kararlar Bilgi Bankasında yayınlanan karar metni editöryal düzeltmelere tabi tutulmuş olabilir.",
    "Kararlar Bilgi Bankasında yayınlanan karar metni",
    "editöryal düzeltmelere tabi tutulmuş olabilir.",
}


def extract(html: str, basvuru: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    node = soup.find(id="Karar")  # decision text tab
    if node is None:
        return None
    for t in node(["script", "style", "noscript"]):
        t.decompose()
    lines = [ln.strip() for ln in node.get_text("\n").split("\n")
             if ln.strip() and ln.strip() not in NOISE_LINES]
    body = clean_text("\n".join(lines))
    if len(body) < MIN_CHARS:
        return None
    title = f"AYM Bireysel Başvuru {basvuru}" if basvuru else "AYM Bireysel Başvuru Kararı"
    return {"title": title, "text": body}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--threshold", type=float, default=0.8)
    ap.add_argument("--no-minhash", action="store_true",
                    help="skip in-builder MinHash (leave near-dedup to the global pass)")
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

    records, n_skip, n_nonbireysel, n_huge = [], 0, 0, 0
    for f in files:
        m = BASVURU_RE.search(f.name)
        if not m:
            # non-bireysel page template (norm-control Esas/Karar etc.) — wrong doc type here
            n_nonbireysel += 1
            continue
        basvuru = f"{m.group(1)}/{m.group(2)}"
        rec = extract(f.read_text(encoding="utf-8", errors="ignore"), basvuru)
        if not rec:
            n_skip += 1
            continue
        if len(rec["text"]) > 3_000_000:  # guard vs concatenation/parse artifacts
            n_huge += 1
            continue
        records.append(rec)
    print(f"  kept={len(records)} skipped(no #Karar/empty)={n_skip} "
          f"non_bireysel_dropped={n_nonbireysel} huge_dropped={n_huge}")

    if args.no_minhash:
        deduped = records
        print("  minhash SKIPPED (--no-minhash)")
    else:
        keep = minhash_keep_mask([r["text"] for r in records], threshold=args.threshold, verify=args.strict)
        deduped = [r for r, k in zip(records, keep) if k]
        print(f"  after minhash({args.threshold}): {len(deduped)} (removed {len(records)-len(deduped)})")

    out_dir = work / "out" / DS_NAME
    save_dataset(deduped, out_dir)
    verify_dataset(out_dir)
    print(f"  built -> {out_dir}")


if __name__ == "__main__":
    main()
