"""Build legal_sayistay from Sayıştay (Court of Accounts) chamber decisions (HTML).

Raw: s3://.../selman/legal/sayistay/html/daire__<year>__<id>.html
Each page is a label/value record ending in the KARAR (decision) body.

Schema: title = "Sayıştay <Daire>. Daire Kararı <KararNo> — <Konu>"; text = decision body.
Applies MinHash near-dedup (threshold 0.8) — these decisions are heavily templated.

Usage:
    python builders/build_legal_sayistay.py --work /scratch/sayistay [--limit N]
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

RAW_PREFIX = f"{SOURCE_ROOT}/legal/sayistay/html"
DS_NAME = "legal_sayistay"
NAV_NOISE = {
    "Daire Kararları Detay : T.C. Sayıştay Başkanlığı",
    "Daire Karar Detayı",
    "Listeye Dön",
    "Yazdır",
}
LABELS = ["Daire", "Karar Tarihi", "Karar No", "İlam No", "Madde No",
          "Kamu İdaresi Türü", "Hesap Yılı", "Konu"]
MIN_CHARS = 60


def parse_html(html: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n").split("\n") if ln.strip()]
    lines = [ln for ln in lines if ln not in NAV_NOISE]

    fields: dict[str, str] = {}
    karar_idx = None
    for i, ln in enumerate(lines):
        if ln == "KARAR" and karar_idx is None:
            karar_idx = i
        if karar_idx is None and ln in LABELS and i + 1 < len(lines):
            nxt = lines[i + 1]
            if nxt not in LABELS and nxt != "KARAR":
                fields[ln] = nxt
    if karar_idx is None:
        return None
    body_lines = lines[karar_idx + 1:]
    # cut the trailing site footer/nav block (present on every page)
    for i, ln in enumerate(body_lines):
        if ln in ("İletişim Bilgileri", "İletişim") or ln.startswith("T.C. Sayıştay Başkanlığı ©"):
            body_lines = body_lines[:i]
            break
    body = clean_text("\n".join(body_lines))
    if len(body) < MIN_CHARS:
        return None

    daire = fields.get("Daire", "").strip()
    karar_no = fields.get("Karar No", "").strip()
    konu = fields.get("Konu", "").strip()
    title_bits = ["Sayıştay"]
    if daire:
        title_bits.append(f"{daire}. Daire Kararı")
    else:
        title_bits.append("Kararı")
    if karar_no:
        title_bits.append(karar_no)
    title = " ".join(title_bits)
    if konu:
        title = f"{title} — {konu}"
    return {"title": title, "text": body}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--limit", type=int, default=0, help="0 = all files")
    ap.add_argument("--threshold", type=float, default=0.8)
    ap.add_argument("--strict", action="store_true", help="verify candidate Jaccard >= threshold before dropping")
    args = ap.parse_args()
    work = Path(args.work)
    html_dir = work / "html"

    print(f"[download] syncing {RAW_PREFIX} -> {html_dir}")
    s3_sync_down(RAW_PREFIX, html_dir)
    files = sorted(html_dir.glob("*.html"))
    if args.limit:
        files = files[: args.limit]
    print(f"[parse] {len(files)} html files")

    records = []
    n_fail = 0
    for f in files:
        try:
            rec = parse_html(f.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            rec = None
        if rec:
            records.append(rec)
        else:
            n_fail += 1
    print(f"  parsed={len(records)}  unparsable/empty={n_fail}")

    texts = [r["text"] for r in records]
    keep = minhash_keep_mask(texts, threshold=args.threshold, verify=args.strict)
    deduped = [r for r, k in zip(records, keep) if k]
    print(f"  after minhash(threshold={args.threshold}): {len(deduped)} "
          f"(removed {len(records) - len(deduped)} near-dupes)")

    out_dir = work / "out" / DS_NAME
    save_dataset(deduped, out_dir)
    verify_dataset(out_dir)
    print(f"  built -> {out_dir}")


if __name__ == "__main__":
    main()
