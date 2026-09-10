"""Build edu_wikibooks_tr from tr.wikibooks pages (one JSON per page).

Raw: s3://.../selman/education/wikibooks/json/<pageid>_<title>.json
Fields: {page_id, raw_title, html, wikitext, sections, ...}

Schema: title = raw_title; text = cleaned rendered HTML.
Filters out redirects/stubs (wikitext starting with #YÖNLENDİRME / #REDIRECT, or tiny text).

Usage:
    python builders/build_edu_wikibooks_tr.py --work /scratch/wikibooks [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup
from bs4 import Comment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.build_utils import (  # noqa: E402
    SOURCE_ROOT,
    clean_text,
    save_dataset,
    s3_sync_down,
    verify_dataset,
)

JSON_PREFIX = f"{SOURCE_ROOT}/education/wikibooks/json"
DS_NAME = "edu_wikibooks_tr"
MIN_CHARS = 120
REDIRECT_MARKERS = ("#YÖNLENDİRME", "#YONLENDIRME", "#REDIRECT")


def is_redirect(wikitext: str) -> bool:
    wt = (wikitext or "").lstrip().upper()
    return wt.startswith(REDIRECT_MARKERS)


_EDIT_RE = re.compile(r"\[\s*(düzenle|değiştir|edit)\s*(\|\s*kaynağı\s*değiştir\s*)?\]", re.I)
_TMPL_RE = re.compile(r"\{\{[^{}]*\}\}")


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for t in soup(["script", "style", "noscript", "table"]):
        t.decompose()
    # MediaWiki UI cruft: section "[düzenle]" edit links, jump links, no-print boxes
    for sel in (".mw-editsection", ".mw-editsection-bracket", ".noprint",
                ".mw-jump-link", ".mw-headline-anchor", "style"):
        for el in soup.select(sel):
            el.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    txt = soup.get_text("\n", strip=True)
    txt = _EDIT_RE.sub("", txt)      # any residual [düzenle] tokens
    txt = _TMPL_RE.sub("", txt)      # leftover {{templates}}
    return clean_text(txt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    work = Path(args.work)
    json_dir = work / "json"

    print(f"[sync] {JSON_PREFIX}")
    s3_sync_down(JSON_PREFIX, json_dir, include=["*.json"], exclude=["*"])
    files = sorted(json_dir.glob("*.json"))
    if args.limit:
        files = files[: args.limit]
    print(f"[parse] {len(files)} pages")

    records, n_redirect, n_short = [], 0, 0
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if is_redirect(d.get("wikitext", "")):
            n_redirect += 1
            continue
        text = clean_html(d.get("html", ""))
        if len(text) < MIN_CHARS:
            n_short += 1
            continue
        title = clean_text(d.get("raw_title")) or "Vikikitap"
        records.append({"title": title, "text": text})
    print(f"  kept={len(records)} redirects={n_redirect} stubs/short={n_short}")

    out_dir = work / "out" / DS_NAME
    save_dataset(records, out_dir)
    verify_dataset(out_dir)
    print(f"  built -> {out_dir}")


if __name__ == "__main__":
    main()
