"""Validate MinHash near-dedup correctness on real ictihat pairs (+ a controlled check).

Runs the SAME shingling/threshold as the builder, but RECORDS which kept doc each dropped
doc matched, then computes the TRUE word-5-gram Jaccard of each (dropped, kept) pair:
  - dropped pairs should have true Jaccard >= ~threshold  (dedup was justified)
  - a sample of kept-vs-kept pairs should be < threshold   (no near-dupes wrongly kept)

Usage: python validate_minhash.py <html_dir> <sample_n>
"""
import itertools
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from builders.build_legal_ictihat import _parse_file  # noqa: E402

NGRAM, NUM_PERM, TH = 5, 128, 0.8


def shingles(text):
    toks = text.split()
    if len(toks) < NGRAM:
        return {text}
    return {" ".join(toks[j:j + NGRAM]) for j in range(len(toks) - NGRAM + 1)}


def true_jaccard(a, b):
    sa, sb = shingles(a), shingles(b)
    return len(sa & sb) / len(sa | sb) if (sa and sb) else 0.0


def minhash_dedup_with_pairs(docs):
    from datasketch import LeanMinHash, MinHash, MinHashLSH
    lsh = MinHashLSH(threshold=TH, num_perm=NUM_PERM)
    kept, pairs = [], []          # pairs = (dropped_idx, matched_kept_idx)
    for i, d in enumerate(docs):
        m = MinHash(num_perm=NUM_PERM)
        for sh in shingles(d["text"]):
            m.update(sh.encode("utf-8"))
        lm = LeanMinHash(m)
        res = lsh.query(lm)
        if res:
            pairs.append((i, int(res[0])))
        else:
            lsh.insert(str(i), lm)
            kept.append(i)
    return kept, pairs


def main():
    html_dir, n = sys.argv[1], int(sys.argv[2])
    print(f"[sample] scanning up to {n} html files")
    paths = []
    with os.scandir(html_dir) as it:
        for e in it:
            if e.name.endswith(".html"):
                paths.append(e.path)
                if len(paths) >= n:
                    break
    docs = [r for r in (_parse_file(p) for p in paths) if r]
    print(f"parsed {len(docs)} docs")

    kept, pairs = minhash_dedup_with_pairs(docs)
    print(f"RESULT: kept={len(kept)}  dropped(near-dup)={len(pairs)}  "
          f"({100*len(pairs)/max(1,len(docs)):.1f}%)")

    # (A) dropped pairs — true Jaccard should be >= threshold
    if pairs:
        js = [true_jaccard(docs[d]["text"], docs[k]["text"]) for d, k in pairs]
        bad = sum(1 for x in js if x < TH - 0.05)
        print(f"[A] dropped-pair TRUE Jaccard: min={min(js):.3f} mean={st.mean(js):.3f} "
              f"median={st.median(js):.3f} max={max(js):.3f}")
        print(f"    pairs below {TH-0.05:.2f} (would be false-drops): {bad}/{len(js)}")
        print("    === example dropped pairs (title | text[:180]) ===")
        for d, k in pairs[:5]:
            jj = true_jaccard(docs[d]["text"], docs[k]["text"])
            print(f"    -- jaccard={jj:.3f} --")
            print(f"       KEPT   : {docs[k]['title']} | {docs[k]['text'][:180]!r}")
            print(f"       DROPPED: {docs[d]['title']} | {docs[d]['text'][:180]!r}")
    else:
        print("[A] no near-dup pairs found in this sample (increase sample or use a contiguous range)")

    # (B) kept-vs-kept — should be mutually distinct (< threshold)
    import random
    random.seed(0)
    sub = random.sample(kept, min(80, len(kept)))
    mx, viol = 0.0, 0
    for a, b in itertools.combinations(sub, 2):
        jj = true_jaccard(docs[a]["text"], docs[b]["text"])
        mx = max(mx, jj)
        if jj >= TH:
            viol += 1
    print(f"[B] kept-vs-kept ({len(sub)} docs, {len(sub)*(len(sub)-1)//2} pairs): "
          f"max Jaccard={mx:.3f}  violations(>= {TH})={viol}  (should be 0)")

    # (C) controlled sanity: a hand-made near-dup must be caught; a distinct doc must not
    if len(docs) >= 2:
        base = docs[0]["text"]
        toks = base.split()
        near = " ".join(toks[:-3] + ["DEGISTIRILMIS", "KELIMELER", "SON"])  # tweak last few words
        other = docs[1]["text"]
        trio = [{"title": "base", "text": base},
                {"title": "distinct", "text": other},
                {"title": "near-dup-of-base", "text": near}]
        k2, p2 = minhash_dedup_with_pairs(trio)
        dropped_titles = {trio[d]["title"] for d, _ in p2}
        print(f"[C] controlled: kept={[trio[i]['title'] for i in k2]}  dropped={sorted(dropped_titles)}")
        print(f"    jaccard(base, near-dup)={true_jaccard(base, near):.3f} (high, should drop)  "
              f"jaccard(base, distinct)={true_jaccard(base, other):.3f} (low, should keep)")


if __name__ == "__main__":
    main()
