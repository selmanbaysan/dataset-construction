"""Scan built datasets for encoding problems: U+FFFD, mojibake, control chars, TR coverage."""
import sys
from datasets import load_from_disk

# common signatures of UTF-8 wrongly decoded as latin-1/cp1252 (mojibake)
MOJIBAKE = ("Ã¼", "Ã§", "ÅŸ", "Ä±", "Ã‡", "Ã–", "Ãœ", "ÄŸ", "â€", "Ã¶", "Ã¢", "Å", "Ã ")
TR = set("çğıöşüÇĞİÖŞÜ")


def scan(path, cap=60000):
    ds = load_from_disk(path)
    n = ds.num_rows
    step = max(1, n // cap)
    idx = list(range(0, n, step))
    texts = ds.select(idx)["text"]
    titles = ds.select(idx)["title"]
    m = len(texts)
    fffd = sum(1 for t in texts if t and "�" in t)
    moji = sum(1 for t in texts if t and any(s in t for s in MOJIBAKE))
    ctrl = sum(1 for t in texts if t and any(ord(c) < 9 or 11 <= ord(c) < 32 and c not in "\n\r\t" for c in t[:2000]))
    tr = sum(1 for t in texts if t and any(c in TR for c in t[:500]))
    title_fffd = sum(1 for t in titles if t and "�" in t)
    print(f"[{path.split('/')[-1]}] rows={n:,} scanned={m:,}")
    print(f"    U+FFFD(replacement): text={fffd} ({100*fffd/m:.3f}%)  title={title_fffd}")
    print(f"    mojibake-signature docs: {moji} ({100*moji/m:.3f}%)")
    print(f"    control-char docs: {ctrl} ({100*ctrl/m:.3f}%)")
    print(f"    Turkish-char docs: {tr} ({100*tr/m:.1f}%)")


for p in sys.argv[1:]:
    scan(p)
