"""Build legal_ictihat from Turkish high/appeals court decisions (HTML) — SCALE build.

Raw: s3://.../selman/legal/ictihat/html/<DOCTYPE>__<birim>__<documentId>.html
The corpus mixes FIVE court types (not just Danıştay):
  DANISTAYKARAR  = Danıştay (Council of State)          birim e.g. "10. Daire"
  YARGITAYKARARI = Yargıtay (Court of Cassation)        birim e.g. "1. Ceza Dairesi"
  KYB            = Kanun Yararına Bozma (Yargıtay)       birim e.g. "1. Ceza Dairesi"
  ISTINAFHUKUK   = Bölge Adliye (regional appeals, civil) birim self-describing (has court name)
  YERELHUKUK     = yerel/first-instance civil courts     birim self-describing (has court name)
This set is ~11M files (~39 GB), so:
  - parse in parallel across all cores (title from filename; body via lxml get_text),
  - EXACT-dedup (hash) first to cheaply drop crawl duplicates,
  - then MinHash near-dedup on the survivors (court decisions are heavily templated).

Schema: title names the actual court (e.g. "Yargıtay 1. Ceza Dairesi — Karar <id>",
"Adana 1. Asliye Ticaret Mahkemesi — Karar <id>") ; text = decision body.

Usage (big instance):
    python builders/build_legal_ictihat.py --work /scratch/ictihat [--limit N] [--no-minhash] [--procs N]
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.build_utils import (  # noqa: E402
    SOURCE_ROOT,
    clean_text,
    minhash_keep_mask_parallel,
    save_dataset,
    s3_sync_down,
    verify_dataset,
)

HTML_PREFIX = f"{SOURCE_ROOT}/legal/ictihat/html"
DS_NAME = "legal_ictihat"
MIN_CHARS = 80
# <DOCTYPE>__<birim>__<documentId>.html  (birim may itself contain spaces/dots/digits)
NAME_RE = re.compile(r"^(?P<doctype>[A-ZÇĞİÖŞÜ0-9]+)__(?P<birim>.+)__(?P<docid>\d+)\.html$")
# doctype -> (court-name prefix, decision label). Empty prefix => birim already names the court.
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


def _parse_file(path_str: str):
    """Worker: HTML file -> {title, text} or None. Title from filename, body via lxml."""
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
    return {"title": title, "text": body}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--threshold", type=float, default=0.8)
    ap.add_argument("--no-minhash", action="store_true")
    ap.add_argument("--procs", type=int, default=0, help="0 = all cores")
    ap.add_argument("--skip-sync", action="store_true", help="raw already local — skip S3 mirror")
    ap.add_argument("--strict", action="store_true",
                    help="verify candidate Jaccard >= threshold before dropping (precise dedup)")
    args = ap.parse_args()
    work = Path(args.work)
    html_dir = work / "html"

    if not args.skip_sync:
        print(f"[sync] {HTML_PREFIX} (~11M files — this takes a while)", flush=True)
        s3_sync_down(HTML_PREFIX, html_dir, include=["*.html"], exclude=["*"])
    files = [str(p) for p in html_dir.glob("*.html")]
    if args.limit:
        files = files[: args.limit]
    procs = args.procs or mp.cpu_count()
    print(f"[parse] {len(files)} files across {procs} procs", flush=True)

    records, n_skip = [], 0
    with mp.Pool(procs) as pool:
        for i, rec in enumerate(pool.imap_unordered(_parse_file, files, chunksize=500)):
            if rec:
                records.append(rec)
            else:
                n_skip += 1
            if i and i % 1_000_000 == 0:
                print(f"  parsed {i:,} …", flush=True)
    print(f"  parsed_ok={len(records)} skipped/empty={n_skip}", flush=True)

    # exact dedup first (cheap; removes crawl duplicates before the expensive MinHash)
    seen, exact = set(), []
    for r in records:
        h = hash(r["text"])
        if h in seen:
            continue
        seen.add(h)
        exact.append(r)
    print(f"  after exact-dedup: {len(exact)} (removed {len(records)-len(exact)})", flush=True)
    del records, seen

    if args.no_minhash:
        keep = [True] * len(exact)
        print("  minhash SKIPPED (--no-minhash)", flush=True)
    else:
        keep = minhash_keep_mask_parallel([r["text"] for r in exact],
                                          threshold=args.threshold, procs=args.procs,
                                          verify=args.strict)
    n_kept = sum(keep)
    print(f"  after minhash({args.threshold}): {n_kept} (removed {len(exact)-n_kept})", flush=True)

    # stream to arrow via from_generator (from_list on 8M+ rows spikes memory -> OOM)
    from datasets import Dataset, Features, Value

    feats = Features({"title": Value("string"), "text": Value("string")})

    def _gen():
        for r, k in zip(exact, keep):
            if k:
                yield {"title": r["title"], "text": r["text"]}

    out_dir = work / "out" / DS_NAME
    Dataset.from_generator(_gen, features=feats).save_to_disk(str(out_dir))
    verify_dataset(out_dir)
    print(f"  built -> {out_dir}", flush=True)


if __name__ == "__main__":
    main()
