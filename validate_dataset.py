"""Validate a staged HF pretraining dataset (loaded from a local dir synced from S3).

Checks schema, emptiness, length distribution, HTML/boilerplate leakage, encoding, exact
dup rate, and prints random samples + a PASS/WARN/FAIL verdict.

Usage: python validate_dataset.py <dataset_dir> [num_samples]
"""
import re
import sys
import statistics as st
from datasets import load_from_disk

HTML_RE = re.compile(r"<\s*/?\s*(p|div|span|br|table|tr|td|a|script|style|html|body|ul|li|img)\b", re.I)
REPL = "�"
TR_CHARS = set("çğıöşüÇĞİÖŞÜ")


def main():
    d = sys.argv[1]
    n_samples = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    ds = load_from_disk(d)
    n = ds.num_rows
    cols = ds.column_names
    print(f"DATASET_DIR: {d}")
    print(f"ROWS: {n}")
    print(f"COLUMNS: {cols}")
    print(f"FEATURES: { {k: str(v) for k,v in ds.features.items()} }")

    issues, warns = [], []
    if set(cols) != {"title", "text"}:
        (issues if not {"title", "text"}.issubset(cols) else warns).append(f"columns != [title,text]: {cols}")

    # scan (cap at 40k rows for speed on big sets, evenly sampled)
    step = max(1, n // 40000)
    idxs = list(range(0, n, step))
    txt = ds.select(idxs)["text"]
    ttl = ds.select(idxs)["title"]
    scanned = len(txt)

    empty_text = sum(1 for t in txt if not t or not t.strip())
    empty_title = sum(1 for t in ttl if not t or not t.strip())
    lens = [len(t or "") for t in txt]
    short50 = sum(1 for x in lens if x < 50)
    short200 = sum(1 for x in lens if x < 200)
    html_leak = sum(1 for t in txt if t and HTML_RE.search(t))
    repl_char = sum(1 for t in txt if t and REPL in t)
    tr_docs = sum(1 for t in txt if t and any(c in TR_CHARS for c in t[:500]))
    # exact dup rate on scanned sample
    seen, dups = set(), 0
    for t in txt:
        h = hash(t)
        if h in seen:
            dups += 1
        seen.add(h)

    lens_sorted = sorted(lens)
    def pct(p): return lens_sorted[min(len(lens_sorted) - 1, int(p * len(lens_sorted)))] if lens_sorted else 0

    print(f"SCANNED: {scanned} (step={step})")
    print(f"TEXT_LEN min/mean/median/p95/max: {min(lens)}/{int(st.mean(lens))}/{int(st.median(lens))}/{pct(0.95)}/{max(lens)}")
    print(f"EMPTY_TEXT: {empty_text}  EMPTY_TITLE: {empty_title}")
    print(f"SHORT(<50): {short50} ({100*short50/scanned:.2f}%)   SHORT(<200): {short200} ({100*short200/scanned:.2f}%)")
    print(f"HTML_LEAK docs: {html_leak} ({100*html_leak/scanned:.2f}%)")
    print(f"REPLACEMENT_CHAR docs: {repl_char}   TR_CHAR docs: {tr_docs} ({100*tr_docs/scanned:.1f}%)")
    print(f"EXACT_DUP in sample: {dups} ({100*dups/scanned:.2f}%)")

    # verdict thresholds
    if empty_text > 0:
        issues.append(f"{empty_text} empty text rows")
    if 100 * html_leak / scanned > 2:
        issues.append(f"HTML leakage {100*html_leak/scanned:.1f}% > 2%")
    if 100 * short50 / scanned > 5:
        warns.append(f"{100*short50/scanned:.1f}% docs < 50 chars")
    if 100 * repl_char / scanned > 1:
        warns.append(f"{100*repl_char/scanned:.1f}% docs with U+FFFD")
    if 100 * dups / scanned > 3:
        warns.append(f"exact-dup {100*dups/scanned:.1f}% > 3%")

    print("\n=== SAMPLES ===")
    import hashlib
    # deterministic spread of sample indices
    picks = [int((i + 0.5) / n_samples * n) for i in range(n_samples)]
    for i in picks:
        row = ds[i]
        print(f"--- row {i} ---")
        print(f"  title: {row['title'][:140]!r}")
        print(f"  text : {row['text'][:500]!r}")

    verdict = "FAIL" if issues else ("WARN" if warns else "PASS")
    print(f"\nVERDICT: {verdict}")
    if issues:
        print("ISSUES: " + "; ".join(issues))
    if warns:
        print("WARNINGS: " + "; ".join(warns))


if __name__ == "__main__":
    main()
