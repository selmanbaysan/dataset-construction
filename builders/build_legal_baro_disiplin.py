"""Build legal_baro_disiplin from Türkiye Barolar Birliği disciplinary decisions (HTML).

Raw: s3://.../selman/legal/baro-disiplin/html/disiplin__<year>__<n>.html  (full page)
                                              disiplin__<year>__<n>_body.html  (clean body)

Prefers the *_body.html extract (site announcements stripped). Parses the
Tarih / Esas / Karar header then the decision body.

Schema: title = "TBB Disiplin Kararı <Esas> E. <Karar> K." ; text = decision body.
MinHash near-dedup (moderate) — disciplinary rulings are templated.

Usage:
    python builders/build_legal_baro_disiplin.py --work /scratch/baro [--limit N]
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
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

HTML_PREFIX = f"{SOURCE_ROOT}/legal/baro-disiplin/html"
DS_NAME = "legal_baro_disiplin"
MIN_CHARS = 80
DATE_RE = re.compile(r"^\d{1,2}[./]\d{1,2}[./]\d{4}$")
BASE_RE = re.compile(r"^(disiplin__\d+__\d+)(_body)?\.html$")


def parse(html: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n").split("\n") if ln.strip()]

    # anchor on the "Tarih" label whose following line looks like a date
    start = None
    for i, ln in enumerate(lines[:-1]):
        if ln == "Tarih" and DATE_RE.match(lines[i + 1]):
            start = i
            break
    if start is None:
        return None
    seg = lines[start:]

    def val(label):
        for j, ln in enumerate(seg[:-1]):
            if ln == label:
                return seg[j + 1]
        return ""

    esas, karar = val("Esas"), val("Karar")
    # body = everything after the Karar value line
    body_start = 0
    for j, ln in enumerate(seg[:-1]):
        if ln == "Karar" and seg[j + 1] == karar:
            body_start = j + 2
            break
    body = clean_text("\n".join(seg[body_start:]))
    if len(body) < MIN_CHARS:
        return None
    bits = ["TBB Disiplin Kararı"]
    if esas:
        bits.append(f"{esas} E.")
    if karar:
        bits.append(f"{karar} K.")
    return {"title": " ".join(bits), "text": body}


def pick_files(html_dir: Path) -> list[Path]:
    """One file per decision, preferring the *_body.html variant."""
    groups: dict[str, dict[str, Path]] = defaultdict(dict)
    for f in html_dir.glob("*.html"):
        m = BASE_RE.match(f.name)
        if not m:
            continue
        groups[m.group(1)]["body" if m.group(2) else "full"] = f
    return [g.get("body") or g.get("full") for g in groups.values()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--threshold", type=float, default=0.8)
    ap.add_argument("--strict", action="store_true", help="verify candidate Jaccard >= threshold before dropping")
    args = ap.parse_args()
    work = Path(args.work)
    html_dir = work / "html"

    print(f"[sync] {HTML_PREFIX}")
    s3_sync_down(HTML_PREFIX, html_dir, include=["*.html"], exclude=["*"])
    files = pick_files(html_dir)
    if args.limit:
        files = files[: args.limit]
    print(f"[parse] {len(files)} decisions (deduplicated body/full pairs)")

    records, n_skip = [], 0
    for f in files:
        rec = parse(f.read_text(encoding="utf-8", errors="ignore"))
        if rec:
            records.append(rec)
        else:
            n_skip += 1
    print(f"  kept={len(records)} skipped={n_skip}")

    keep = minhash_keep_mask([r["text"] for r in records], threshold=args.threshold, verify=args.strict)
    deduped = [r for r, k in zip(records, keep) if k]
    print(f"  after minhash({args.threshold}): {len(deduped)} (removed {len(records)-len(deduped)})")

    out_dir = work / "out" / DS_NAME
    save_dataset(deduped, out_dir)
    verify_dataset(out_dir)
    print(f"  built -> {out_dir}")


if __name__ == "__main__":
    main()
