"""Shared helpers for building HuggingFace pretraining datasets from raw S3 data.

Every dataset in the VNGRS pretraining corpus is a `datasets.Dataset` saved with
`save_to_disk()` and a uniform `{title, text}` schema. These helpers implement the
common build -> verify -> upload -> (move raw) flow described in CONVERSION_PLAN.md.
"""
from __future__ import annotations

import os
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Iterable, Iterator

# Profile is env-driven: locally default to "vngrs"; on EC2 set DSC_AWS_PROFILE="" so the
# CLI uses the instance IAM role (there is no named profile on the box).
AWS_PROFILE = os.environ.get("DSC_AWS_PROFILE", "vngrs")
PROFILE_ARGS = ["--profile", AWS_PROFILE] if AWS_PROFILE else []
TARGET_ROOT = "s3://corpus-949787038248-us-east-1/vngrs-pretraining-corpora-v4"
SOURCE_ROOT = "s3://cache-949787038248-us-east-1/selman"
RAW_DEST_ROOT = f"{TARGET_ROOT}/_raw_data"  # where sources move after a verified build


# --------------------------------------------------------------------------- #
# text cleaning
# --------------------------------------------------------------------------- #
_WS_RUN = re.compile(r"[ \t ]+")
_NL_RUN = re.compile(r"\n{3,}")


def clean_text(s: str | None) -> str:
    """Normalise unicode + whitespace without destroying paragraph structure."""
    if not s:
        return ""
    s = unicodedata.normalize("NFC", s)
    s = s.replace(" ", " ").replace("\r\n", "\n").replace("\r", "\n")
    s = _WS_RUN.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    s = _NL_RUN.sub("\n\n", s)
    return s.strip()


def html_to_text(html: str, parser: str = "lxml") -> str:
    """Strip a full HTML document to readable text (drops script/style/nav chrome)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, parser)
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "svg"]):
        tag.decompose()
    return clean_text(soup.get_text("\n", strip=True))


# --------------------------------------------------------------------------- #
# minhash near-dedup
# --------------------------------------------------------------------------- #
def minhash_keep_mask(
    texts: list[str],
    threshold: float = 0.8,
    num_perm: int = 128,
    ngram: int = 5,
    verify: bool = False,
) -> list[bool]:
    """Return a keep/drop mask; keeps the first doc in each near-duplicate cluster.

    Uses word n-gram shingles + MinHashLSH (Jaccard threshold). verify=False drops on any
    LSH band-collision (aggressive); verify=True only drops when a candidate's estimated
    Jaccard >= `threshold` (strict — avoids collapsing distinct docs sharing boilerplate).
    """
    from datasketch import MinHash, MinHashLSH

    lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
    kept_mh: dict = {}
    keep = [False] * len(texts)
    for i, text in enumerate(texts):
        tokens = text.split()
        if len(tokens) < ngram:
            shingles = {text}
        else:
            shingles = {" ".join(tokens[j : j + ngram]) for j in range(len(tokens) - ngram + 1)}
        m = MinHash(num_perm=num_perm)
        for sh in shingles:
            m.update(sh.encode("utf-8"))
        cand = lsh.query(m)
        if cand:
            if not verify:
                continue
            if any(m.jaccard(kept_mh[c]) >= threshold for c in cand):
                continue
        lsh.insert(str(i), m)
        keep[i] = True
        if verify:
            kept_mh[str(i)] = m
    return keep


def _mh_signature(args):
    """Worker: text -> LeanMinHash (compact + picklable). Module-level for pickling."""
    from datasketch import LeanMinHash, MinHash

    text, num_perm, ngram = args
    tokens = text.split()
    if len(tokens) < ngram:
        shingles = {text}
    else:
        shingles = {" ".join(tokens[j : j + ngram]) for j in range(len(tokens) - ngram + 1)}
    m = MinHash(num_perm=num_perm)
    for sh in shingles:
        m.update(sh.encode("utf-8"))
    return LeanMinHash(m)


def minhash_keep_mask_parallel(
    texts,
    threshold: float = 0.8,
    num_perm: int = 128,
    ngram: int = 5,
    procs: int | None = None,
    log_every: int = 500_000,
    verify: bool = False,
) -> list[bool]:
    """Same result as minhash_keep_mask, but computes the (expensive) MinHash signatures
    across all cores, then does a fast serial LSH insert/query pass. Prints progress + ETA.

    Keeps the first doc of each near-duplicate cluster (order-preserving, so the mask aligns
    with `texts`).

    verify=False (default): drop a doc on ANY LSH band-collision candidate (aggressive —
      can drop pairs with true Jaccard somewhat below `threshold`).
    verify=True: only drop when a candidate's *estimated* MinHash Jaccard >= `threshold`
      (strict — avoids collapsing distinct docs that merely share heavy boilerplate).
    """
    import multiprocessing as mp
    import time

    from datasketch import MinHashLSH

    procs = procs or mp.cpu_count()
    n = len(texts)
    print(f"  [minhash] {n:,} signatures across {procs} procs", flush=True)
    sigs = [None] * n
    t0 = time.time()
    with mp.Pool(procs) as pool:
        # imap preserves order -> sigs[i] aligns with texts[i]
        for i, lm in enumerate(pool.imap(_mh_signature,
                                         ((t, num_perm, ngram) for t in texts),
                                         chunksize=2000)):
            sigs[i] = lm
            if log_every and i and i % log_every == 0:
                rate = i / max(1e-9, time.time() - t0)
                print(f"    sig {i:,}/{n:,} ({100*i/n:.1f}%)  {rate:.0f}/s  "
                      f"eta {(n-i)/max(1,rate)/60:.1f} min", flush=True)
    print(f"  [minhash] signatures done in {(time.time()-t0)/60:.1f} min; building LSH (serial)",
          flush=True)

    lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
    kept_lms: dict = {}  # only populated when verify=True
    keep = [False] * n
    t1 = time.time()
    for i, lm in enumerate(sigs):
        cand = lsh.query(lm)
        if cand:
            if not verify:
                continue  # aggressive: any band-collision -> drop
            # strict: confirm at least one candidate is actually >= threshold
            if any(lm.jaccard(kept_lms[c]) >= threshold for c in cand):
                continue
        lsh.insert(str(i), lm)
        keep[i] = True
        if verify:
            kept_lms[str(i)] = lm
        if log_every and i and i % (log_every * 2) == 0:
            print(f"    lsh {i:,}/{n:,} ({100*i/n:.1f}%)", flush=True)
    print(f"  [minhash] LSH pass done in {(time.time()-t1)/60:.1f} min "
          f"({'strict-verified' if verify else 'aggressive'})", flush=True)
    return keep


# --------------------------------------------------------------------------- #
# build / verify / upload / move
# --------------------------------------------------------------------------- #
def save_dataset(records: Iterable[dict], out_dir: str | Path, num_proc: int | None = None):
    """Materialise an iterable of {title,text} dicts to a save_to_disk dataset."""
    from datasets import Dataset

    out_dir = Path(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    rows = list(records) if not isinstance(records, list) else records
    ds = Dataset.from_list(rows)
    # enforce column order title, text (+ any extras after)
    cols = ["title", "text"] + [c for c in ds.column_names if c not in ("title", "text")]
    ds = ds.select_columns(cols)
    ds.save_to_disk(str(out_dir), num_proc=num_proc)
    return ds


def verify_dataset(out_dir: str | Path, n_samples: int = 2) -> dict:
    """Load a built dataset back and report schema, size and samples."""
    from datasets import load_from_disk

    ds = load_from_disk(str(out_dir))
    info = {
        "num_rows": ds.num_rows,
        "columns": ds.column_names,
        "features": {k: str(v) for k, v in ds.features.items()},
    }
    print(f"[verify] {out_dir}")
    print(f"  rows={info['num_rows']}  columns={info['columns']}")
    total_chars = sum(len(t) for t in ds["text"][: min(ds.num_rows, 5000)])
    print(f"  avg text chars (first<=5k rows) ~ {total_chars / max(1, min(ds.num_rows,5000)):.0f}")
    for i in range(min(n_samples, ds.num_rows)):
        row = ds[i]
        print(f"  --- sample {i} ---")
        print(f"    title: {row['title'][:120]!r}")
        print(f"    text : {row['text'][:300]!r}")
    return info


def s3_cp(uri: str, local: str | Path) -> Path:
    """Download a single S3 object to a local path (quiet)."""
    local = Path(local)
    local.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["aws", "s3", "cp", uri, str(local), *PROFILE_ARGS, "--only-show-errors"],
        check=True,
    )
    return local


def s3_sync_down(prefix: str, local_dir: str | Path, exclude: list[str] | None = None,
                 include: list[str] | None = None) -> Path:
    """Mirror an S3 prefix to a local dir (quiet). Optional include/exclude filters."""
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["aws", "s3", "sync", prefix.rstrip("/") + "/", str(local_dir),
           *PROFILE_ARGS, "--only-show-errors"]
    for e in exclude or []:
        cmd += ["--exclude", e]
    for i in include or []:
        cmd += ["--include", i]
    subprocess.run(cmd, check=True)
    return local_dir


def sync_to_s3(local_dir: str | Path, dataset_name: str, dry_run: bool = False) -> str:
    """Upload a built dataset dir to TARGET_ROOT/<dataset_name>/ via `aws s3 sync`."""
    dest = f"{TARGET_ROOT}/{dataset_name}/"
    cmd = ["aws", "s3", "sync", str(local_dir), dest, *PROFILE_ARGS, "--only-show-errors"]
    if dry_run:
        cmd.append("--dryrun")
    print("[upload]", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return dest


def move_raw_source(src_subpath: str, dataset_name: str, dry_run: bool = True) -> None:
    """Move a verified dataset's raw source out of SOURCE_ROOT into _raw_data/<name>/.

    DESTRUCTIVE (relocates source). Defaults to dry_run=True — pass dry_run=False only
    after the built dataset has been uploaded and verified.
    """
    src = f"{SOURCE_ROOT}/{src_subpath.rstrip('/')}/"
    dst = f"{RAW_DEST_ROOT}/{dataset_name}/"
    cmd = ["aws", "s3", "mv", src, dst, "--recursive", *PROFILE_ARGS]
    if dry_run:
        cmd.append("--dryrun")
    print("[move-raw]", " ".join(cmd))
    subprocess.run(cmd, check=True)
