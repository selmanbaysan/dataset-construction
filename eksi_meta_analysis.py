"""Analyze eksi-sozluk raw metadata (run on the instance; files in /build/eksi/jsonl)."""
import glob
import json
import random
import statistics as st
from collections import Counter

files = sorted(glob.glob("/build/eksi/jsonl/*.jsonl"))
random.seed(0)
sample = random.sample(files, min(400, len(files)))

keys = Counter()
n = 0
fav, textlen = [], []
dm_nonnull = 0
authors = Counter()
titles = set()
examples = []
for f in sample:
    try:
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            for k in d:
                keys[k] += 1
            fc = d.get("favorite_count")
            if isinstance(fc, int):
                fav.append(fc)
            if d.get("date_modified"):
                dm_nonnull += 1
            if d.get("author"):
                authors[d["author"]] += 1
            if d.get("title_id") is not None:
                titles.add(d["title_id"])
            if d.get("text"):
                textlen.append(len(d["text"]))
            if len(examples) < 2:
                examples.append(d)
    except Exception:
        continue

print(f"ENTRIES sampled: {n:,} from {len(sample)} files")
print(f"DISTINCT title_id in sample: {len(titles):,}")
print(f"DISTINCT authors in sample: {len(authors):,}")
print("\nFIELD fill rates:")
for k, c in keys.most_common():
    print(f"  {k:16s}: {100*c/n:5.1f}%")
if fav:
    favs = sorted(fav)
    print(f"\nfavorite_count: min={min(fav)} mean={st.mean(fav):.2f} median={st.median(fav)} "
          f"p95={favs[int(0.95*len(favs))]} max={max(fav)}")
    print(f"  %fav==0: {100*sum(1 for x in fav if x==0)/len(fav):.1f}   "
          f"%fav>=1: {100*sum(1 for x in fav if x>=1)/len(fav):.1f}   "
          f"%fav>=10: {100*sum(1 for x in fav if x>=10)/len(fav):.1f}   "
          f"%fav>=50: {100*sum(1 for x in fav if x>=50)/len(fav):.1f}")
print(f"\ndate_modified non-null: {100*dm_nonnull/n:.1f}%")
if textlen:
    tl = sorted(textlen)
    print(f"entry text len: median={tl[len(tl)//2]} mean={int(st.mean(textlen))} p95={tl[int(0.95*len(tl))]} max={max(textlen)}")
print(f"top authors: {authors.most_common(5)}")
print("\nEXAMPLE ENTRY:")
print(json.dumps(examples[0], ensure_ascii=False, indent=1))
