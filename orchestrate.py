"""On-instance orchestrator: run a phase of builders, stage verified outputs to S3.

Runs each builder as a subprocess, logs to /build/logs/<name>.log, and on success syncs
every produced dataset dir to the cache staging prefix (the instance role can write cache
but NOT the corpus bucket — final promote cache->corpus is done from an admin machine).

Per-dataset status JSON + log are pushed to s3://.../selman/_deploy/build_status/ so
progress survives instance stop/start or a dropped monitor session.

Usage:  python orchestrate.py <phase>       # phase in {small, heavy}
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

os.environ["PATH"] = "/root/dsc/venv/bin:" + os.environ.get("PATH", "")
# On EC2 there is no named profile — build_utils reads this to use the instance IAM role.
os.environ["DSC_AWS_PROFILE"] = ""
PY = "/root/dsc/venv/bin/python"
AWS = "/root/dsc/venv/bin/aws"
REPO = "/root/dsc"
BUILD = "/build"
STAGING = "s3://cache-949787038248-us-east-1/selman/_deploy/built"
STATUS_S3 = "s3://cache-949787038248-us-east-1/selman/_deploy/build_status"

# (name, builder script, work dir, extra args)
PHASES = {
    "small": [
        ("econbiz", "builders/build_acad_econbiz_abstracts.py", f"{BUILD}/econbiz", []),
        ("acikders", "builders/build_edu_acikders_ulakbim.py", f"{BUILD}/acikders", []),
        ("ayk", "builders/build_dict_ayk_ansiklopedi.py", f"{BUILD}/ayk", []),
        ("wikibooks", "builders/build_edu_wikibooks_tr.py", f"{BUILD}/wikibooks", []),
        ("baro", "builders/build_legal_baro_disiplin.py", f"{BUILD}/baro", []),
        ("mevzuat", "builders/build_legal_mevzuat.py", f"{BUILD}/mevzuat", []),
    ],
    "heavy": [
        ("sayistay", "builders/build_legal_sayistay.py", f"{BUILD}/sayistay", []),
        ("reddit", "builders/build_soc_reddit_tr.py", f"{BUILD}/reddit", ["--no-minhash"]),
        ("anayasa", "builders/build_legal_anayasa_bireysel.py", f"{BUILD}/anayasa", []),
        # DEFERRED (need scalable/streaming approach or a bigger box):
        #  - ictihat: ~10M tiny html files (~30 GB) — sync+parse would take many hours; MinHash would OOM 15 GB.
        #  - eksi:    81 GB raw / 1.8M files — in-memory group-by won't fit 15 GB RAM / 81 GB disk.
    ],
    # reddit re-run on its own (recursion-safe build; --no-minhash to bound memory at 5.77M docs)
    "reddit": [
        ("reddit", "builders/build_soc_reddit_tr.py", f"{BUILD}/reddit", ["--no-minhash"]),
    ],
    # re-run the 4 datasets fixed after validation (footer/edit-link/doc-type/prefix cleanups)
    "fixes": [
        ("econbiz", "builders/build_acad_econbiz_abstracts.py", f"{BUILD}/econbiz", []),
        ("wikibooks", "builders/build_edu_wikibooks_tr.py", f"{BUILD}/wikibooks", []),
        ("sayistay", "builders/build_legal_sayistay.py", f"{BUILD}/sayistay", []),
        ("anayasa", "builders/build_legal_anayasa_bireysel.py", f"{BUILD}/anayasa", []),
    ],
    # large datasets on the big instance (quick wins first, behemoths last)
    "large": [
        ("anayasa_norm", "builders/build_legal_anayasa_norm.py", f"{BUILD}/anayasa_norm", []),
        ("reddit", "builders/build_soc_reddit_tr.py", f"{BUILD}/reddit", ["--no-minhash"]),
        ("eksi", "builders/build_soc_eksi_sozluk.py", f"{BUILD}/eksi", []),
        ("ictihat", "builders/build_legal_ictihat.py", f"{BUILD}/ictihat", []),
    ],
    # finish the two remaining behemoths (eksi rebuilt with date-ordered format; ictihat 11M)
    "finish": [
        ("eksi", "builders/build_soc_eksi_sozluk.py", f"{BUILD}/eksi", []),
        ("ictihat", "builders/build_legal_ictihat.py", f"{BUILD}/ictihat", []),
    ],
    # re-run the smaller legal sets with STRICT-verified dedup @ 0.90 (raw restored to cache)
    "legal_small_strict": [
        ("sayistay", "builders/build_legal_sayistay.py", f"{BUILD}/sayistay_s", ["--strict", "--threshold", "0.90"]),
        ("anayasa_bireysel", "builders/build_legal_anayasa_bireysel.py", f"{BUILD}/anayasa_bir_s", ["--strict", "--threshold", "0.90"]),
        ("anayasa_norm", "builders/build_legal_anayasa_norm.py", f"{BUILD}/anayasa_norm_s", ["--strict", "--threshold", "0.90"]),
        ("baro", "builders/build_legal_baro_disiplin.py", f"{BUILD}/baro_s", ["--strict", "--threshold", "0.90"]),
        ("mevzuat", "builders/build_legal_mevzuat.py", f"{BUILD}/mevzuat_s", ["--strict", "--threshold", "0.90"]),
    ],
}


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def push(local, s3uri):
    sh([AWS, "s3", "cp", local, s3uri, "--only-show-errors"])


def run_one(name, script, work, extra):
    Path(f"{BUILD}/logs").mkdir(parents=True, exist_ok=True)
    Path(f"{BUILD}/status").mkdir(parents=True, exist_ok=True)
    log = f"{BUILD}/logs/{name}.log"
    status = {"name": name, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    t0 = time.time()
    with open(log, "w") as fh:
        rc = subprocess.call([PY, script, "--work", work] + extra,
                             stdout=fh, stderr=subprocess.STDOUT, cwd=REPO)
    status["rc"] = rc
    status["secs"] = round(time.time() - t0, 1)

    logtext = Path(log).read_text(errors="ignore")
    # capture the builder's own reported counts
    status["rows_reported"] = [int(x) for x in re.findall(r"rows=(\d+)", logtext)]
    status["datasets"] = []
    if rc == 0:
        for out in sorted(glob.glob(f"{work}/out/*")):
            if not Path(out, "dataset_info.json").exists():
                continue
            dsname = os.path.basename(out)
            up = sh([AWS, "s3", "sync", out, f"{STAGING}/{dsname}/", "--only-show-errors"])
            status["datasets"].append({"name": dsname, "stage_rc": up.returncode,
                                       "stage_err": up.stderr[-300:] if up.returncode else ""})
    status["done"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    Path(f"{BUILD}/status/{name}.json").write_text(json.dumps(status, ensure_ascii=False, indent=1))
    push(log, f"{STATUS_S3}/{name}.log")
    push(f"{BUILD}/status/{name}.json", f"{STATUS_S3}/{name}.json")
    print(f"[{name}] rc={rc} secs={status['secs']} datasets={[d['name'] for d in status['datasets']]}",
          flush=True)
    return status


def main():
    phase = sys.argv[1]
    specs = PHASES[phase]
    print(f"=== PHASE {phase}: {[s[0] for s in specs]} ===", flush=True)
    results = []
    for spec in specs:
        results.append(run_one(*spec))
    summary = {"phase": phase, "results": results,
               "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    Path(f"{BUILD}/status/PHASE_{phase}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    push(f"{BUILD}/status/PHASE_{phase}.json", f"{STATUS_S3}/PHASE_{phase}.json")
    # simple done marker
    Path(f"{BUILD}/status/PHASE_{phase}.done").write_text("done")
    push(f"{BUILD}/status/PHASE_{phase}.done", f"{STATUS_S3}/PHASE_{phase}.done")
    print(f"=== PHASE {phase} COMPLETE ===", flush=True)


if __name__ == "__main__":
    main()
