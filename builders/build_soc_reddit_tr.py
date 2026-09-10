"""Build soc_reddit_tr from the Turkish Reddit corpus (per-subreddit jsonl.zst).

Raw: s3://.../selman/social/reddit_turkish/corpus/<Subreddit>.jsonl.zst
Each line is a post {id, subreddit, title, selftext, author, score, comments[...]},
where comments nest recursively via `replies` and each carries a `score` (upvotes).

Content kept AS-IS (the corpus authors deliberately keep TR + some foreign) — no lang filter.

Schema: THREE columns —
  title    = the submission's title
  text     = the submission's main body (selftext); empty for link/title-only posts
  comments = the comment tree rendered like Reddit's default view: top-level comments
             sorted by score (desc), each reply nested UNDER its parent with indentation
             (so it's clear which comment a reply answers), replies also score-sorted;
             top-level threads separated by a special separator. Each line:
             "<indent>[↳ ]u/<author> (<score>): <body>".

Dedup: exact only (crossposts/copypasta) on (title, text, comments). MinHash off by default
(social text; near-dedup left to the global pass).

Robustness: deep megathreads (KGBTR/burdurland) nest thousands deep, so JSON parsing needs a
raised recursion limit + a large C stack (run under a big-stack thread); comment rendering is
iterative. Rows stream via Dataset.from_generator so 5.77M posts never all sit in RAM.

Usage:
    python builders/build_soc_reddit_tr.py --work /scratch/reddit [--limit-subs N] [--no-minhash]
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import threading
from pathlib import Path

import zstandard as zstd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.build_utils import (  # noqa: E402
    SOURCE_ROOT,
    clean_text,
    minhash_keep_mask,
    s3_sync_down,
    verify_dataset,
)

CORPUS_PREFIX = f"{SOURCE_ROOT}/social/reddit_turkish/corpus"
DS_NAME = "soc_reddit_tr"
MIN_CHARS = 40
THREAD_SEP = "\n\n⸻⸻⸻\n\n"   # separator between top-level comment threads
INDENT = "    "                # one nesting level
DELETED = ("[deleted]", "[removed]", "[silinmiş]")


def _score(x) -> int:
    s = x.get("score")
    return s if isinstance(s, int) else 0


def render_comments(comments) -> str:
    """Render the comment forest: top-level comments by score desc; each subtree rendered
    pre-order with replies score-sorted and indented one level deeper than their parent
    (nesting = reply relationship); top-level threads joined by THREAD_SEP. Iterative to
    survive very deep megathreads."""
    tops = sorted(comments or [], key=lambda c: -_score(c))
    blocks = []
    for top in tops:
        lines = []
        stack = [(top, 0)]
        while stack:
            c, depth = stack.pop()
            body = clean_text(c.get("body"))
            if body and body not in DELETED:
                author = clean_text(c.get("author")) or "[silinmiş]"
                ind = INDENT * min(depth, 15)
                marker = "↳ " if depth else ""
                body_ml = body.replace("\n", "\n" + ind + "  ")  # keep multi-line bodies aligned
                lines.append(f"{ind}{marker}u/{author} ({_score(c)}): {body_ml}")
            # push replies so the highest-score one is processed first (pre-order, best-first)
            for r in reversed(sorted(c.get("replies") or [], key=lambda r: -_score(r))):
                stack.append((r, depth + 1))
        if lines:
            blocks.append("\n".join(lines))
    return THREAD_SEP.join(blocks)


def post_to_record(d):
    title = clean_text(d.get("title"))
    selftext = clean_text(d.get("selftext"))
    if selftext in DELETED:
        selftext = ""
    comments = render_comments(d.get("comments"))
    if not (selftext or comments):          # nothing beyond the title -> skip
        return None
    if len(title) + len(selftext) + len(comments) < MIN_CHARS:
        return None
    if not title:
        title = f"r/{d.get('subreddit', '')}"
    return {"title": title, "text": selftext, "comments": comments}


def read_zst_jsonl(path: Path):
    dctx = zstd.ZstdDecompressor(max_window_size=2**31)
    with path.open("rb") as fh, dctx.stream_reader(fh) as reader:
        for line in io.TextIOWrapper(reader, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def build(args, subs, out_dir):
    from datasets import Dataset, Features, Value

    seen = set()
    stats = {"posts": 0, "docs": 0, "dups": 0}

    def gen():
        for sub in subs:
            for d in read_zst_jsonl(sub):
                stats["posts"] += 1
                rec = post_to_record(d)
                if not rec:
                    continue
                h = hash((rec["title"], rec["text"], rec["comments"]))
                if h in seen:  # exact dedup across the whole corpus
                    stats["dups"] += 1
                    continue
                seen.add(h)
                stats["docs"] += 1
                yield rec

    feats = Features({"title": Value("string"), "text": Value("string"), "comments": Value("string")})
    ds = Dataset.from_generator(gen, features=feats)
    print(f"  posts={stats['posts']} docs={stats['docs']} exact_dups_dropped={stats['dups']}")

    if not args.no_minhash and ds.num_rows:
        combined = [f"{t}\n{x}\n{c}" for t, x, c in zip(ds["title"], ds["text"], ds["comments"])]
        keep = minhash_keep_mask(combined, threshold=args.threshold, verify=True)
        ds = ds.select([i for i, k in enumerate(keep) if k])
        print(f"  after minhash({args.threshold}): {ds.num_rows}")

    ds.save_to_disk(str(out_dir))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--limit-subs", type=int, default=0)
    ap.add_argument("--threshold", type=float, default=0.85)
    ap.add_argument("--no-minhash", action="store_true")
    ap.add_argument("--src", default=None, help="override S3 corpus prefix (e.g. _raw_data)")
    args = ap.parse_args()
    work = Path(args.work)
    corpus_dir = work / "corpus"

    src = args.src or CORPUS_PREFIX
    print(f"[sync] {src}")
    s3_sync_down(src, corpus_dir, include=["*.jsonl.zst"], exclude=["*"])
    subs = sorted(corpus_dir.glob("*.jsonl.zst"))
    if args.limit_subs:
        subs = subs[: args.limit_subs]
    print(f"[parse] {len(subs)} subreddits")

    out_dir = work / "out" / DS_NAME
    # deep megathreads: allow very deep JSON nesting without a C-stack crash
    sys.setrecursionlimit(2_000_000)
    threading.stack_size(1024 * 1024 * 1024)  # 1 GB stack
    err = {}

    def runner():
        try:
            build(args, subs, out_dir)
        except BaseException as e:  # surface inside-thread failures
            err["e"] = e

    t = threading.Thread(target=runner)
    t.start()
    t.join()
    if "e" in err:
        raise err["e"]

    verify_dataset(out_dir)
    print(f"  built -> {out_dir}")


if __name__ == "__main__":
    main()
