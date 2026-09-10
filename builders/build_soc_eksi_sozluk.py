"""Build soc_eksi_sozluk: one document per title, all entries concatenated.

Raw: s3://.../selman/social/eksi-sozluk/jsonl/batch_*.jsonl
Each line is one entry: {entry_id, title_id, title, text, author, date, ...}

Strategy (per spec): group entries by `title_id`, order by `entry_id`, exact-dedup
identical entry texts, join with a separator -> {title, text}.

NOTE ON SCALE: eksi is large (hundreds of thousands of batch files). This builder groups
in memory, which is fine for a subset / a big machine. For the full run on modest RAM,
switch to the sorted external-merge path noted in CONVERSION_PLAN.md. Use --limit-files
for a pilot subset.

Usage:
    python builders/build_soc_eksi_sozluk.py --work /scratch/eksi --limit-files 300
    python builders/build_soc_eksi_sozluk.py --work /scratch/eksi          # full
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.build_utils import (  # noqa: E402
    PROFILE_ARGS,
    SOURCE_ROOT,
    clean_text,
    s3_sync_down,
    save_dataset,
    verify_dataset,
)

RAW_PREFIX = f"{SOURCE_ROOT}/social/eksi-sozluk/jsonl"
DS_NAME = "soc_eksi_sozluk"
MIN_CHARS = 20
ENTRY_SEP = "\n\n⸻\n\n"   # special-character separator between entries
_FAR_FUTURE = datetime(9999, 1, 1)


def parse_date(s: str | None) -> datetime:
    """eksi date is 'DD.MM.YYYY HH:MM' (sometimes date-only). Unparseable -> far future
    so it sorts last, with entry_id as the real tiebreaker."""
    if not s:
        return _FAR_FUTURE
    s = s.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return _FAR_FUTURE


def list_keys(limit: int) -> list[str]:
    """List jsonl object keys under the prefix, stopping early when limited."""
    proc = subprocess.Popen(
        ["aws", "s3", "ls", RAW_PREFIX + "/", *PROFILE_ARGS],
        stdout=subprocess.PIPE, text=True,
    )
    keys = []
    assert proc.stdout is not None
    for line in proc.stdout:
        name = line.split()[-1]
        if name.endswith(".jsonl"):
            keys.append(name)
        if limit and len(keys) >= limit:
            proc.terminate()
            break
    return keys


def download_subset(keys: list[str], dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for k in keys:
        local = dest / k
        if not local.exists():
            subprocess.run(["aws", "s3", "cp", f"{RAW_PREFIX}/{k}", str(local),
                            *PROFILE_ARGS, "--only-show-errors"], check=True)
        out.append(local)
    return out


def group_entries(files: list[Path], separator: str):
    """title_id -> (title, [(date, entry_id, text), ...]); entries joined in date order."""
    groups: dict = defaultdict(lambda: [None, []])
    n_entries = 0
    for f in files:
        for line in f.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = d.get("title_id")
            txt = clean_text(d.get("text"))
            if tid is None or not txt:
                continue
            n_entries += 1
            g = groups[tid]
            if g[0] is None:
                g[0] = clean_text(d.get("title")) or "eksi sözlük başlığı"
            g[1].append((parse_date(d.get("date")), d.get("entry_id") or 0, txt))
    print(f"  entries read={n_entries}  distinct titles={len(groups)}")

    for tid, (title, entries) in groups.items():
        entries.sort(key=lambda x: (x[0], x[1]))  # by date, entry_id as tiebreaker
        seen, texts = set(), []
        for _, _, txt in entries:
            if txt in seen:
                continue
            seen.add(txt)
            texts.append(txt)
        joined = separator.join(texts)
        if len(joined) < MIN_CHARS:
            continue
        yield {"title": title, "text": joined}


def build_bucketed(files, work: Path, separator: str, out_dir: Path, n_buckets: int = 128):
    """Disk-based group-by (bounded RAM): distribute entries into title_id-hashed bucket
    files, then group one bucket at a time and stream rows via from_generator.

    The full in-memory group-by does NOT fit even 247 GB, so we spill to disk. Only a
    single bucket + the title_id->title map (~1 GB) are ever held in RAM.
    """
    from datasets import Dataset, Features, Value

    bucket_dir = work / "buckets"
    bucket_dir.mkdir(parents=True, exist_ok=True)
    handles = [(bucket_dir / f"b{i:03d}.jsonl").open("w", encoding="utf-8") for i in range(n_buckets)]
    title_map: dict = {}
    n = 0
    print(f"[pass1] distributing entries into {n_buckets} buckets", flush=True)
    for f in files:
        try:
            for line in f.open(encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tid = d.get("title_id")
                txt = clean_text(d.get("text"))
                if tid is None or not txt:
                    continue
                if tid not in title_map:
                    title_map[tid] = clean_text(d.get("title")) or "eksi sözlük başlığı"
                handles[tid % n_buckets].write(
                    json.dumps([tid, d.get("date"), d.get("entry_id") or 0, txt], ensure_ascii=False) + "\n")
                n += 1
        except Exception:
            continue
    for h in handles:
        h.close()
    print(f"  entries={n:,}  distinct titles={len(title_map):,}", flush=True)

    def gen():
        for i in range(n_buckets):
            bf = bucket_dir / f"b{i:03d}.jsonl"
            groups: dict = defaultdict(list)
            for line in bf.open(encoding="utf-8"):
                tid, date, eid, txt = json.loads(line)
                groups[tid].append((parse_date(date), eid, txt))
            for tid, entries in groups.items():
                entries.sort(key=lambda x: (x[0], x[1]))
                seen, texts = set(), []
                for _, _, t in entries:
                    if t in seen:
                        continue
                    seen.add(t)
                    texts.append(t)
                joined = separator.join(texts)
                if len(joined) < MIN_CHARS:
                    continue
                yield {"title": title_map.get(tid, "eksi sözlük başlığı"), "text": joined}
            bf.unlink()  # free disk as we go

    feats = Features({"title": Value("string"), "text": Value("string")})
    ds = Dataset.from_generator(gen, features=feats)
    ds.save_to_disk(str(out_dir))
    return ds.num_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--limit-files", type=int, default=0, help="0 = all batch files")
    ap.add_argument("--separator", default=ENTRY_SEP)
    ap.add_argument("--buckets", type=int, default=128)
    ap.add_argument("--skip-sync", action="store_true", help="raw already local — skip the S3 mirror")
    args = ap.parse_args()
    work = Path(args.work)
    out_dir = work / "out" / DS_NAME

    jsonl_dir = work / "jsonl"
    if args.limit_files:
        print(f"[list] enumerating batch files (limit={args.limit_files})")
        keys = list_keys(args.limit_files)
        print(f"  {len(keys)} files")
        files = download_subset(keys, jsonl_dir)
        records = list(group_entries(files, args.separator))
        print(f"  documents={len(records)}")
        save_dataset(records, out_dir)
    else:
        # full run: parallel mirror of the whole prefix (files may already be present)
        if not args.skip_sync:
            print(f"[sync] mirroring {RAW_PREFIX} -> {jsonl_dir}")
            s3_sync_down(RAW_PREFIX, jsonl_dir, include=["*.jsonl"], exclude=["*"])
        files = sorted(jsonl_dir.glob("*.jsonl"))
        print(f"  {len(files)} files")
        n = build_bucketed(files, work, args.separator, out_dir, args.buckets)
        print(f"  documents={n}")

    verify_dataset(out_dir)
    print(f"  built -> {out_dir}"
          + ("  [SUBSET — not the full dataset]" if args.limit_files else ""))


if __name__ == "__main__":
    main()
