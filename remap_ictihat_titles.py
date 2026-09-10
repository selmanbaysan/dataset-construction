"""Relabel legal_ictihat titles WITHOUT re-running dedup.

Context: the ictihat corpus mixes 5 court types (Danıştay / Yargıtay / KYB / İstinaf /
Yerel), but the original builder's title regex only matched DANISTAYKARAR__, so every
non-Danıştay decision got the wrong fallback title "Danıştay Kararı".

The row-set is UNCHANGED: exact-dedup + strict MinHash@0.90 key on `text`, which was
correct. Only `title` (derived from filename) was wrong. So instead of re-parsing +
re-deduping (the MinHash pass is ~1h of redundant work), we:
  1. re-parse the local html once -> {stable_hash(text): correct_title},
  2. relabel the EXISTING deduped dataset's titles by hash lookup,
  3. save.

Stable hash = blake2b (NOT Python's salted hash(), which differs per mp worker).

Usage (instance):
    python remap_ictihat_titles.py --html /build/ictihat/html \
        --old /build/ictihat/old_ds --out /build/ictihat/out/legal_ictihat --procs 32
"""
from __future__ import annotations

import argparse
import hashlib
import multiprocessing as mp
import re
import sys
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.build_utils import clean_text  # noqa: E402  (same normalizer as the build)

MIN_CHARS = 80
NAME_RE = re.compile(r"^(?P<doctype>[A-ZÇĞİÖŞÜ0-9]+)__(?P<birim>.+)__(?P<docid>\d+)\.html$")
COURT = {
    "DANISTAYKARAR":  ("Danıştay", "Karar"),
    "YARGITAYKARARI": ("Yargıtay", "Karar"),
    "KYB":            ("Yargıtay", "Kanun Yararına Bozma Kararı"),
    "ISTINAFHUKUK":   ("", "Karar"),
    "ISTINAFCEZA":    ("", "Karar"),
    "YERELHUKUK":     ("", "Karar"),
    "YERELCEZA":      ("", "Karar"),
}


def _title_from_name(name: str) -> str:
    m = NAME_RE.match(name)
    if not m:
        return "Mahkeme Kararı"
    prefix, label = COURT.get(m.group("doctype"), ("", "Karar"))
    birim = m.group("birim").strip()
    court = f"{prefix} {birim}".strip() if prefix else birim
    return f"{court} — {label} {m.group('docid')}"


def _h(text: str) -> bytes:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()


def _parse(path_str: str):
    """Worker: html file -> (stable_hash(body), correct_title) or None. Mirrors the builder's
    body extraction exactly so the hash matches the stored `text`."""
    p = Path(path_str)
    title = _title_from_name(p.name)
    try:
        soup = BeautifulSoup(p.read_text(encoding="utf-8", errors="ignore"), "lxml")
    except Exception:
        return None
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    body = clean_text(soup.get_text("\n", strip=True))
    if len(body) < MIN_CHARS:
        return None
    return (_h(body), title)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", required=True)
    ap.add_argument("--old", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--procs", type=int, default=0)
    args = ap.parse_args()

    files = [str(p) for p in Path(args.html).glob("*.html")]
    procs = args.procs or mp.cpu_count()
    print(f"[parse] {len(files):,} files across {procs} procs -> title map", flush=True)
    tmap: dict[bytes, str] = {}
    with mp.Pool(procs) as pool:
        for i, r in enumerate(pool.imap_unordered(_parse, files, chunksize=500)):
            if r:
                tmap[r[0]] = r[1]
            if i and i % 1_000_000 == 0:
                print(f"  parsed {i:,} … map={len(tmap):,}", flush=True)
    print(f"  title map built: {len(tmap):,} unique texts", flush=True)

    from datasets import load_from_disk

    ds = load_from_disk(args.old)
    print(f"[remap] old deduped rows={ds.num_rows:,}", flush=True)
    stats = {"miss": 0, "changed": 0}

    def fix(batch):
        titles = []
        for old_t, x in zip(batch["title"], batch["text"]):
            nt = tmap.get(_h(x))
            if nt is None:
                stats["miss"] += 1
                nt = old_t
            elif nt != old_t:
                stats["changed"] += 1
            titles.append(nt)
        return {"title": titles}

    ds = ds.map(fix, batched=True, batch_size=2000, desc="relabel titles")
    print(f"  relabeled: changed={stats['changed']:,} unmatched={stats['miss']:,}", flush=True)

    ds.save_to_disk(args.out)

    # title audit: distribution of the leading court token on a sample
    c = Counter()
    n = min(300_000, ds.num_rows)
    for t in ds.select(range(n))["title"]:
        c[t.split(" ")[0]] += 1
    print(f"[audit] leading-token counts over {n:,} rows:", c.most_common(12), flush=True)
    print(f"  built -> {args.out} rows={ds.num_rows:,}", flush=True)


if __name__ == "__main__":
    main()
