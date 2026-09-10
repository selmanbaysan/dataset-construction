"""Empirically choose the MinHash Jaccard threshold for legal dedup.

Idea: for a sample of docs, surface candidate pairs across the whole similarity range
(LSH at a LOW threshold), compute their TRUE word-5-gram Jaccard, and bucket them. Print
counts + real example pairs per band so we can SEE at what Jaccard level two docs stop
being "distinct decisions sharing a template" and become "the same decision".
Also reports the boilerplate floor: Jaccard of random (unrelated) pairs.

Usage: python threshold_validate.py <html_dir> <sample_n>
"""
import itertools
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from builders.build_legal_ictihat import _parse_file  # noqa: E402

NGRAM, NUM_PERM = 5, 128
BANDS = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.75), (0.75, 0.8),
         (0.8, 0.85), (0.85, 0.9), (0.9, 0.95), (0.95, 1.01)]


def shingles(t):
    toks = t.split()
    return {t} if len(toks) < NGRAM else {" ".join(toks[j:j + NGRAM]) for j in range(len(toks) - NGRAM + 1)}


def jac(a, b):
    sa, sb = a, b
    return len(sa & sb) / len(sa | sb) if (sa and sb) else 0.0


def main():
    html_dir, n = sys.argv[1], int(sys.argv[2])
    paths = []
    with os.scandir(html_dir) as it:
        for e in it:
            if e.name.endswith(".html"):
                paths.append(e.path)
                if len(paths) >= n:
                    break
    docs = [r for r in (_parse_file(p) for p in paths) if r]
    print(f"parsed {len(docs)} docs")
    shs = [shingles(d["text"]) for d in docs]

    from datasketch import LeanMinHash, MinHash, MinHashLSH
    lms = []
    for s in shs:
        m = MinHash(num_perm=NUM_PERM)
        for sh in s:
            m.update(sh.encode("utf-8"))
        lms.append(LeanMinHash(m))

    # candidate pairs via LSH at a LOW threshold (0.5) -> spans the 0.5..1.0 range
    lsh = MinHashLSH(threshold=0.5, num_perm=NUM_PERM)
    seen_pairs = set()
    cand = []  # (i, j, true_jaccard)
    for i, lm in enumerate(lms):
        for c in lsh.query(lm):
            j = int(c)
            key = (j, i)
            if key in seen_pairs:
                continue
            seen_pairs.add((i, j))
            cand.append((i, j, jac(shs[i], shs[j])))
        lsh.insert(str(i), lm)
    print(f"candidate pairs (LSH@0.5): {len(cand)}")

    print("\n=== TRUE-Jaccard distribution of candidate pairs ===")
    for lo, hi in BANDS:
        band = [(i, j, v) for i, j, v in cand if lo <= v < hi]
        print(f"  [{lo:.2f}-{hi:.2f}): {len(band):6d} pairs")
        for i, j, v in band[:2]:
            print(f"      j={v:.3f}  A={docs[i]['title'][:40]!r}  B={docs[j]['title'][:40]!r}")
            print(f"              A:{docs[i]['text'][:110]!r}")
            print(f"              B:{docs[j]['text'][:110]!r}")

    # boilerplate floor: random unrelated pairs
    random.seed(0)
    idx = list(range(len(docs)))
    rand = [jac(shs[a], shs[b]) for a, b in
            (random.sample(idx, 2) for _ in range(3000))]
    rand.sort()
    print("\n=== random-pair Jaccard (boilerplate floor) ===")
    print(f"  mean={sum(rand)/len(rand):.3f}  median={rand[len(rand)//2]:.3f}  "
          f"p95={rand[int(0.95*len(rand))]:.3f}  max={rand[-1]:.3f}")
    print("  (high random-pair Jaccard => heavy shared boilerplate => threshold must sit well above this)")


if __name__ == "__main__":
    main()
