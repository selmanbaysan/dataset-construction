# dataset-construction

Scripts to convert raw Turkish/English data in S3 into pretraining-ready HuggingFace
datasets. See **[CONVERSION_PLAN.md](CONVERSION_PLAN.md)** for the full classification of
every raw source, per-dataset strategy, and the dedup decisions.

- **Source:** `s3://cache-949787038248-us-east-1/selman/`
- **Target:** `s3://corpus-949787038248-us-east-1/vngrs-pretraining-corpora-v4/`
- **AWS profile:** `vngrs`
- **Schema:** every dataset is a `datasets.save_to_disk()` folder with `{title, text}`.

## Setup

```bash
pip install -r requirements.txt   # datasets, bs4, lxml, zstandard, datasketch, tqdm
```

## Builders (Tier A — directly convertible, no OCR)

Each builder syncs its raw prefix, cleans → maps to `{title, text}`, dedups where flagged,
then `save_to_disk` locally and self-verifies. Nothing is uploaded automatically.

```bash
python builders/build_dict_ayk_ansiklopedi.py     --work $W/ayk          # clean JSONL (2 encyclopedias)
python builders/build_acad_econbiz_abstracts.py   --work $W/econbiz      # abstracts (reports fill rate)
python builders/build_edu_acikders_ulakbim.py     --work $W/acikders     # courseware pages
python builders/build_edu_wikibooks_tr.py         --work $W/wikibooks    # wiki pages (drops redirects)
python builders/build_legal_mevzuat.py            --work $W/mevzuat      # legislation  (MinHash 0.9)
python builders/build_legal_baro_disiplin.py      --work $W/baro         # bar decisions (MinHash 0.8)
python builders/build_legal_sayistay.py           --work $W/sayistay     # audit decisions (MinHash 0.8)  [large]
python builders/build_legal_ictihat.py            --work $W/ictihat      # Danıştay decisions (MinHash 0.8) [large]
python builders/build_legal_anayasa_bireysel.py   --work $W/anayasa      # AYM decisions (MinHash 0.8) [large ~15GB]
python builders/build_soc_reddit_tr.py            --work $W/reddit       # TR reddit, keep-as-is (MinHash 0.85) [large]
python builders/build_soc_eksi_sozluk.py          --work $W/eksi         # group entries by title [very large]
```

`[large]` sets sync 15k–50k files (or GBs) — **run on EC2**, not a laptop. Every builder
takes `--limit`/`--limit-files`/`--limit-subs` for a quick subset dry-run.

## Publish flow (manual, after eyeballing a build)

```python
from common.build_utils import sync_to_s3, verify_dataset, move_raw_source
verify_dataset("$W/ayk/out/dict_ayk_ataturk_ansiklopedi")
sync_to_s3("$W/ayk/out/dict_ayk_ataturk_ansiklopedi", "dict_ayk_ataturk_ansiklopedi")  # upload
move_raw_source("reference/ayk-ansiklopedi", "dict_ayk_ansiklopedi", dry_run=False)     # move raw AFTER verify
```

`move_raw_source` is **destructive** (relocates the raw source into
`…/_raw_data/<name>/`) and defaults to `dry_run=True`.

## Excluded / deferred
`corpus/kumru-chat` (privacy — real user chats), the English `dolma_reddit_gap`
(deferred), and all PDF/ebook collections (academic, books, periodicals, archives,
manuals) which need the separate OCR pipeline. See CONVERSION_PLAN.md §3–4.
