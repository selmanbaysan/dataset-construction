# Pretraining Corpus Conversion Plan

**Source (raw):** `s3://cache-949787038248-us-east-1/selman/`
**Target (HF datasets):** `s3://corpus-949787038248-us-east-1/vngrs-pretraining-corpora-v4/`
**AWS profile:** `vngrs`
**Date:** 2026-07-07

---

## 0. Conventions learned from existing corpora

Every dataset under the target path is a HuggingFace `datasets` object written with
**`Dataset.save_to_disk()`** — i.e. a folder containing `data-XXXXX-of-YYYYY.arrow`,
`dataset_info.json`, `state.json`.

- **Schema is always `title` (string) + `text` (string).** A few sets add one extra
  column (`arxiv_id`, `content_type`) but the two-column core is universal.
- **Naming** uses a category prefix: `acad_`, `dict_`, `edu_`, `gov_`, `news_`, `web_`,
  `book_`, `opus_`, `synth_`, `math_`, `conv_`, `creat_`, `mov_`, `wiki_`, `annot_`.
  There is **no `legal_` or `soc_` prefix yet** → proposed below (needs sign-off).
- **`_mttr` suffix** marks sets that went through the heavy text pipeline (dedup/cleaning).
  Simple conversions (e.g. `dict_tdk`, `web_arabam_ads`) carry no suffix.
- **Two independent dedup layers exist:**
  1. **File-level sha256 dedup** — the `selman/_dedup/` system (corpus ledger + dedup
     cache DB + tombstone map, keeper policy = *oldest*). Removes byte-identical raw
     objects. Already operational; not text-aware.
  2. **Text-level MinHash near-dedup** — applied at dataset-construction time on the
     `text` column. This is the per-dataset decision the plan makes below.
- **Move-after-success:** once a target dataset verifies, the raw source is moved out of
  `selman/…` (mirror of the existing `_disposed/` and `_raw_data/` prefixes on target).

---

## 1. Classification of every raw dataset

Verdict legend: **A** = directly convertible now (structured text, no OCR) · **B** =
convertible but needs a decision (privacy / low-yield / partial) · **C** = needs
OCR / document-extraction pipeline (deferred to a separate batch).

| Raw path (`selman/…`) | Dominant format | Verdict | Note |
|---|---|---|---|
| `social/eksi-sozluk/jsonl` | JSONL | **A** | entries → group by title |
| `legal/ictihat/html` | HTML + metadata JSON | **A** | Danıştay decisions |
| `legal/sayistay/html` | HTML | **A** | Court of Accounts |
| `legal/anayasa-mahkemesi/html` | HTML | **A** | needs content-div selector |
| `legal/baro-disiplin/html` | HTML (+`_body.html`) | **A** | prefer `_body.html` |
| `legal/mevzuat/html` (+`json`) | HTML + JSON tree | **A** | legislation text in HTML |
| `social/reddit_turkish/corpus` | JSONL.zst | **A** | 168 subs, nested comments |
| `social/dolma_reddit_gap_2023-2025/documents` | JSONL.gz (Dolma) | **A** | **English**, already deduped |
| `education/wikibooks/json` | JSON per page | **A** | filter redirects/stubs |
| `reference/ayk-ansiklopedi/metadata` | JSONL (full article text) | **A** | 2 encyclopedias, high value |
| `reference/acikders-ulakbim/metadata` | JSONL (`text_content`) | **A** | open courseware pages |
| `reference/econbiz/metadata` | JSONL (`abstract`) | **A** | abstract corpus (partial fill) |
| `corpus/kumru-chat` | JSON (30 GB) | **B** | real user↔bot chats — privacy + IFT-shaped |
| `legal/ombudsman/html` | HTML | **B→C** | 15,207 files are 130-byte error stubs; real text is PDF |
| `reference/openaire-turkish/metadata` | JSONL | **B** | `description` mostly empty → low yield |
| `reference/isam-misc/metadata` | JSONL | **B** | bülten posts full of WP shortcodes → low yield |
| `academic/zenodo-turkish/{json,md}` | JSON + MD | **B** | small subset only; rest is PDF/doc |
| `corpus/kumru-chat-analysis` | npz/jsonl artifacts | **skip** | analysis output, not a corpus |
| `reference/selman-crawler-metadata` | csv/json | **skip** | crawler bookkeeping |
| `academic/*` (dergipark, trdizin, openalex, isam-makaleler, mkutup, rclis, ttk, tubitak-*, yoktez, ankara-uni-dspace, aperta) | **PDF** + metadata | **C** | metadata JSON *could* yield an abstracts-only set without OCR — optional |
| `books/*` (all 13) | PDF / epub / djvu / mobi / doc | **C** | epub/txt subsets extractable without OCR; PDF/djvu need OCR |
| `periodicals/*` (all 6) | PDF | **C** | OCR |
| `archives/*` (ayk-katalog, isam-arsiv, isam-salname) | PDF | **C** | OCR |
| `manuals/kullanimkilavuzu` | PDF → webp page images | **C** | OCR pilot already running (`_pilot_subset` webp) |
| `legal/{hsk,ysk,mulkiye-dergi,diger-hukuk,gdrive-hukuk-ders-notlari}` | PDF (+some html/doc) | **C** | OCR (diger-hukuk has some html worth a second look) |

---

## 2. Per-dataset conversion strategies (Tier A)

Proposed target names are suggestions pending prefix sign-off.

### 2.1 `soc_eksi_sozluk`  — social/eksi-sozluk/jsonl
- **Scale:** entry IDs span to ~182M (sparse); hundreds of thousands of `batch_*.jsonl`
  files, each line = one entry `{entry_id, title_id, title, text, author, date, url, …}`.
- **Strategy (matches the spec you gave):**
  1. Stream all `jsonl/*.jsonl` (ignore `failed/`, `_deploy/`, `progress/`).
  2. **Group by `title_id`** (stable) — not raw `title` string. Keep the human `title`.
  3. Within a title, order entries by `entry_id` (chronological), **exact-dedup** identical
     entry texts, then **concatenate with a separator** (recommend `\n\n---\n\n` or a
     newline pair — confirm preferred separator).
  4. Emit `{title: <entry title>, text: <joined entries>}`.
  5. Drop titles whose joined text is below a min length (e.g. < 20 chars).
- **Dedup:** entry-level exact dedup **yes**; document-level MinHash **optional** (low —
  each title is distinct). Defer to global dedup layer.

### 2.2 `legal_ictihat`  — legal/ictihat/html (+metadata)  ·  ~51k files, 2.05 GB
- **Content:** Danıştay decisions. HTML → clean decision body via `bs4.get_text()`
  (drop `script/style`; the body is the "İçtihat Metni …" block). Metadata JSONL provides
  `birimAdi`, `esasNo`, `kararNo`, `kararTarihiStr`.
- **Schema:** `title = f"{birimAdi} {esasNo} E. {kararNo} K."` · `text = decision body`.
- **Dedup:** **MinHash YES (required).** Court decisions share large templated passages and
  many decisions are near-identical. Recommend MinHashLSH, char/word 5-grams,
  threshold ≈ 0.8.

### 2.3 `legal_sayistay`  — legal/sayistay/html  ·  ~1,977 files, 0.03 GB
- **Content:** Sayıştay chamber decisions. Small clean pages. Strip nav tokens
  ("Listeye Dön", "Yazdır"). Structured labels: Daire, Karar Tarihi, Karar No, Konu, KARAR.
- **Schema:** `title = f"Sayıştay {Daire}. Daire Kararı {KararNo} — {Konu}"` ·
  `text = KARAR section` (optionally prefixed with the metadata block).
- **Dedup:** **MinHash YES.** Highly templated ("oybirliğiyle karar verildi" etc.).

### 2.4 `legal_anayasa_bireysel`  — legal/anayasa-mahkemesi/html  ·  ~22k files, 14.57 GB
- **Content:** Constitutional Court individual-application decisions. HTML is large
  (~771 KB) and **mostly filter/nav boilerplate** — must target the decision content
  container, not `get_text()` on the whole page. Identify the main `<div>` holding the
  karar text (verify selector on a sample of 20–30 pages before full run).
- **Schema:** `title` from metadata (Başvuru No / Başvuru Adı) · `text = decision body`.
- **Dedup:** **MinHash YES (strong).** These decisions repeat long standardized legal
  sections across thousands of files.

### 2.5 `legal_baro_disiplin`  — legal/baro-disiplin/html  ·  ~3,910 files, 0.18 GB
- **Content:** Bar association disciplinary decisions. **Use `*_body.html`** when present
  (clean body extract, ~4.5 KB) instead of the full page (~80 KB with site announcements).
- **Schema:** `title = f"TBB Disiplin Kararı {Esas} E. {Karar} K."` · `text = decision body`.
- **Dedup:** **MinHash YES (moderate).** Templated disciplinary rulings.

### 2.6 `legal_mevzuat`  — legal/mevzuat/html (+json)  ·  ~16,555 files, 1.13 GB
- **Content:** Legislation (kararname/kanun/yönetmelik). Clean legislative text from HTML.
  The `*_madde_tree.json` gives article structure but `content` is null → **text comes
  from HTML**. Some entries also have PDF (ignore; HTML suffices).
- **Schema:** `title = law name (first heading)` · `text = full legislative text`.
- **Dedup:** exact-dedup **yes** (re-crawls, consolidated versions). MinHash **light** —
  laws are largely unique but amended versions overlap heavily; threshold ≈ 0.9.

### 2.7 `soc_reddit_tr`  — social/reddit_turkish/corpus  ·  168 files, 2.78 GB
- **Content:** 168 Turkish subreddits, 5.77M posts / 51.3M items. Per record:
  `{id, subreddit, title, selftext, author, score, url, num_comments, lang, comments[]}`;
  `comments[]` is nested (threaded). REPORT.md documents a clean two-tier corpus (PRUNE =
  TR-only subs, KEEP-ALL = TR+foreign kept deliberately).
- **Strategy:** one document per post = post `title` + `selftext` + flattened comment
  tree (recommend thread-style join, e.g. comments joined by `\n`). Optionally filter by
  `lang == 'tr'` if a Turkish-only variant is wanted (the corpus intentionally keeps some
  non-TR). Confirm whether to keep foreign-language items.
- **Schema:** `title = f"r/{subreddit}: {post title}"` · `text = post + comments`.
- **Dedup:** exact-dedup **yes** (crossposts, bot copypasta). MinHash **yes (moderate).**

### 2.8 `soc_reddit_gap_en`  — social/dolma_reddit_gap_2023-2025/documents  ·  398 files, 121 GB
- **⚠ English**, not Turkish. Valid for the corpus (it already contains `wiki_en_*`,
  `opus_nllb`, `conv_open_ift_en`, `acad_arxiv`). 252M docs, already in **Dolma format**
  (`{text, …}` per line) and **already deduped** (recipe: document Bloom dedup + English
  filter + NSFW/hate/PII filtering).
- **Strategy:** straight format conversion jsonl.gz → arrow. Reddit atomic docs have no
  natural title → `title = ""` (or subreddit if present in the record — inspect a doc's
  metadata field first).
- **Dedup:** **none needed** (already done upstream). Confirm this English set is in scope.

### 2.9 `edu_wikibooks_tr`  — education/wikibooks/json
- **Content:** tr.wikibooks pages `{page_id, raw_title, html, wikitext, sections, …}`.
- **Strategy:** `title = raw_title`; `text` from cleaned `html` (or `wikitext`).
  **Filter out redirects/stubs** — drop pages whose `wikitext` starts with `#YÖNLENDİRME`/
  `#REDIRECT`, empty `sections`, or tiny length (the sampled `Köfteler.json` was a redirect).
- **Dedup:** exact-dedup only (drop dup titles). MinHash low.

### 2.10 `dict_ayk_ataturk_ansiklopedi` + `dict_ayk_turkdunyasi_ansiklopedi`  — reference/ayk-ansiklopedi/metadata
- **Content:** Two encyclopedias with **full clean article text already extracted**:
  `ayk_ataturk_ansiklopedi.jsonl` (64 MB) + `ayk_turkdunyasi_ansiklopedi.jsonl` (26 MB).
  Fields: `title`, `item_text` (full body), `abstract_text`, `source_text`, plus HTML
  variants. **High value.**
- **Strategy:** `title = title`; `text = item_text` (optionally prepend `abstract_text`,
  append `source_text` as references). Use plain-text fields, not `*_html`.
- **Dedup:** exact-dedup only. Matches the `dict_*` encyclopedia convention.

### 2.11 `edu_acikders_ulakbim`  — reference/acikders-ulakbim/metadata
- **Content:** `acikders_ulakbim_content.jsonl` — open courseware pages
  `{course_name, title, section, text_content, url}`.
- **Strategy:** `title = f"{course_name} — {title}"`; `text = text_content`. Optionally
  group all pages of a course into one doc.
- **Dedup:** exact-dedup (repeated headers). MinHash low.

### 2.12 `acad_econbiz_abstracts`  — reference/econbiz/metadata
- **Content:** `econbiz_items.jsonl` — bibliographic records; **Turkish `abstract`** present
  on a subset.
- **Strategy:** keep only records with non-empty `abstract`; `title = title`;
  `text = abstract`. Report the fill rate; drop the set if too sparse.
- **Dedup:** exact-dedup only.

---

## 3. Tier B — needs an explicit decision before building

- **`corpus/kumru-chat` (30 GB):** real user conversations with the production Kumru bot,
  each starting with the internal system prompt. **Recommend excluding from the pretraining
  batch:** (a) privacy — real user queries need PII review / consent sign-off; (b) format is
  conversational → belongs in an IFT set, not pretraining; (c) the repeated system prompt
  and internal company info would need stripping. Route to a separate, reviewed pipeline.
- **`legal/ombudsman`:** HTML is 15,207 error stubs → treat as **Tier C** (OCR the PDFs).
- **`reference/openaire-turkish`, `reference/isam-misc`:** low text yield (empty
  descriptions / WordPress shortcodes). Skip unless a cleaning pass proves worthwhile.
- **`academic/zenodo-turkish` json/md subset:** small; optional low-priority mini-set.
- **Academic metadata abstracts (optional):** every `academic/*/metadata/` folder likely
  holds abstracts that are convertible **without OCR** → could yield an
  `acad_*_abstracts` family in parallel with the (deferred) full-text OCR sets. Flag for
  decision.

---

## 4. Tier C — deferred to the OCR / document-extraction batch

All `academic/*` PDFs, all `books/*`, all `periodicals/*`, `archives/*`,
`manuals/kullanimkilavuzu` (webp OCR pilot already in `_pilot_subset/`), and the PDF-only
legal sets (`hsk`, `ysk`, `mulkiye-dergi`, `ombudsman`, most of `diger-hukuk`,
`gdrive-hukuk-ders-notlari`). These require the OCR/extraction pipeline (Autopaper) and are
out of scope for this "directly convertible" batch.

---

## 5. Standard build procedure (per dataset)

1. Stream raw from `selman/…` (never mutate source during build).
2. Parse → clean → map to `{title, text}` (dataset-specific logic above).
3. Drop empties / min-length; exact-dedup; **MinHash near-dedup where flagged**.
4. `Dataset.from_generator(...)` (or shard parquet) → `save_to_disk` locally →
   `aws s3 sync` to `…/vngrs-pretraining-corpora-v4/<name>/`.
5. **Verify** (`load_from_disk`, row count, sample rows, `dataset_info.json` schema).
6. **Only after verification**, move raw source
   `selman/<path>/` → `…/vngrs-pretraining-corpora-v4/_raw_data/<name>/`
   (or `selman/_disposed/`) — mirroring the existing move convention.

---

## 6. Decisions (confirmed 2026-07-07)

- ✅ **Approach: pilot first** — build 2–3 representative sets end-to-end (ayk = clean
  JSONL, sayıştay = HTML+MinHash, eksi-sözlük = group-by) to lock the
  build→verify→move pipeline, then fan out.
- ✅ **kumru-chat: EXCLUDED** from the pretraining batch (privacy + IFT-shaped).
- ✅ **`soc_reddit_gap_en` (English, 121 GB): SKIP for now** — revisit later.
- ✅ **reddit_turkish: keep as-is** (preserve the two-tier TR + intentionally-kept
  foreign design; do **not** filter to Turkish-only).

## 7. Pilot results (2026-07-07)

Reusable scaffold: `common/build_utils.py` (clean/minhash/save/verify/upload/move) +
`builders/build_*.py` + `requirements.txt`. Three patterns validated end-to-end
(build + verify locally; upload + raw-move demonstrated as dry-runs — **nothing written
to production, no raw moved**):

| Pilot | Pattern | Result |
|---|---|---|
| `dict_ayk_ataturk_ansiklopedi` | clean JSONL | 1,411 read → **1,404 rows**, ~15.8k chars/article |
| `dict_ayk_turkdunyasi_ansiklopedi` | clean JSONL | **345 rows**, ~21.8k chars/article |
| `legal_sayistay` | HTML + MinHash | 500-sample: 500/500 parsed, MinHash(0.8) dropped ~3.4% |
| `soc_eksi_sozluk` | group-by-title | 120-file subset: 45,107 entries → 12,248 titles → **11,817 docs** |

**Scale corrections (re-measure cleanly — some counts were taken during network flakiness):**
- `social/eksi-sozluk/jsonl`: **1,800,593 files, ~80.87 GB** (reliable). This is the
  heaviest set. The in-memory group-by will NOT fit on a laptop — run on a high-RAM EC2
  instance, or switch to the sorted external-merge path (sort entries by `title_id` on
  disk, then group in a single streaming pass).
- `legal/ictihat/html`: measured ~10.2M objects / ~30 GB in one pass but this looks
  inflated/double-counted — **re-measure**.
- `legal/sayistay/html`: **30k+ files** (not the ~2k first seen — the initial parallel
  count was unreliable).
- Large sets (eksi, sayıştay, anayasa ~15 GB, ictihat, reddit) are all EC2 work — the
  bottleneck is S3 file I/O (15k–1.8M-file syncs), not CPU.

Minor refinement for the full sayıştay run: some older-format pages don't expose
`Karar No`/`Konu` in the label/value order, so a few titles fall back to
"Sayıştay N. Daire Kararı" — bodies are still clean.

### Still to confirm (non-blocking; sensible defaults chosen)
- Prefixes `legal_` / `soc_` (encyclopedias → `dict_`) — using these unless told otherwise.
- eksi-sözlük entry separator — defaulting to `\n\n`.
- Academic abstracts-only sets — deferred with the OCR batch for now.
- Move-after-success target — defaulting to `…/_raw_data/<name>/`.
