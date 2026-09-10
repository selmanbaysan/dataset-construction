"""Build legal_anayasa_norm from Constitutional Court NORM-CONTROL decisions.

Raw: s3://.../selman/legal/anayasa-mahkemesi/html/norm_denetimi__<year>__<id>.html
(These are the non-bireysel pages separated out of legal_anayasa_bireysel.)

The decision text lives in <div id="Karar">, prefixed by a citation line
`(AYM, E.YYYY/N, K.YYYY/N, ...)` and some modal chrome (Kopyala / Başvuru Kararı /
Dava Dilekçesi / x / Kapat + editorial disclaimer) which we strip.

Schema: title = "AYM Norm Denetimi E.<esas> K.<karar>" ; text = decision body.
MinHash near-dedup applied (templated legal boilerplate).

Usage:
    python builders/build_legal_anayasa_norm.py --work /scratch/anayasa_norm [--limit N] [--no-minhash]
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
DS_NAME = "legal_anayasa_norm"
MIN_CHARS = 200
NORM_FILE = re.compile(r"^norm_denetimi__.*\.html$")
ESAS_KARAR = re.compile(r"E\.?\s*(\d{4}/\d+)\s*,?\s*K\.?\s*(\d{4}/\d+)")
NOISE_LINES = {
    "Kopyala", "Başvuru Kararı / Dava Dilekçesi", "x", "Kapat",
    "Kararlar Bilgi Bankasında yayınlanan karar metni",
    "editöryal düzeltmelere tabi tutulmuş olabilir.",
    "Kararlar Bilgi Bankasında yayınlanan karar metni editöryal düzeltmelere tabi tutulmuş olabilir.",
}


def extract(html: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    node = soup.find(id="Karar")
    if node is None:
        return None
    for t in node(["script", "style", "noscript"]):
        t.decompose()
    lines = [ln.strip() for ln in node.get_text("\n").split("\n")
             if ln.strip() and ln.strip() not in NOISE_LINES]
    body = clean_text("\n".join(lines))
    if len(body) < MIN_CHARS:
        return None
    m = ESAS_KARAR.search(body[:400])
    title = f"AYM Norm Denetimi E.{m.group(1)} K.{m.group(2)}" if m else "AYM Norm Denetimi Kararı"
    return {"title": title, "text": body}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--threshold", type=float, default=0.8)
    ap.add_argument("--no-minhash", action="store_true")
    ap.add_argument("--strict", action="store_true", help="verify candidate Jaccard >= threshold before dropping")
    args = ap.parse_args()
    work = Path(args.work)
    html_dir = work / "html"

    print(f"[sync] {HTML_PREFIX} (norm_denetimi only)")
    s3_sync_down(HTML_PREFIX, html_dir, include=["norm_denetimi__*.html"], exclude=["*"])
    files = sorted(f for f in html_dir.glob("*.html") if NORM_FILE.match(f.name))
    if args.limit:
        files = files[: args.limit]
    print(f"[parse] {len(files)} norm-control html files")

    records, n_skip, n_huge = [], 0, 0
    for f in files:
        rec = extract(f.read_text(encoding="utf-8", errors="ignore"))
        if not rec:
            n_skip += 1
            continue
        if len(rec["text"]) > 3_000_000:
            n_huge += 1
            continue
        records.append(rec)
    print(f"  kept={len(records)} skipped={n_skip} huge_dropped={n_huge}")

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
