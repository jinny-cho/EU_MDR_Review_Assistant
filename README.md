# EU MDR (2017/745) & MDCG Guidance Local RAG System — Architecture & Agent Handover Specification

> **Note to AI agents (Claude, Gemini, or others):** This document is the single source of truth for this system's purpose, requirements, and architecture. Read **Section 2 (User Requirements)** before you change any code. Requirements marked **[MUST]** are hard constraints and must not be relaxed for convenience, speed, or cost. Items marked **[ASSUMPTION]** have not yet been confirmed by the user. Do not treat them as fixed. Confirm them with the user if your change depends on them.

## 1. System Overview & Purpose
- **Objective:** 유럽 의료기기 규정(**EU MDR 2017/745**)과 유럽 집행위원회(**EC / MDCG**) 공식 가이던스 문서로 로컬 벡터 데이터베이스(ChromaDB)를 구축합니다. Regulatory Affairs(RA) 실무자는 자연어 질의로 관련 법령 조항과 가이던스 원문을 교차 검색하고, 한국어 규제 해석 보고서를 생성·아카이빙합니다. 이를 위한 로컬 RAG(Retrieval-Augmented Generation) 시스템입니다.
- **One-line definition:** EU MDR과 MDCG를 한국어로 빠르게 교차 검색하되 반드시 원문 근거와 함께 답하고, 사용자가 승인한 검토 결과만 버전 정보와 함께 조직의 지식으로 축적하는 로컬 RA 조수.
- **Target Domain:** Medical Device Regulatory Affairs, especially active/diagnostic devices such as X-ray systems, SaMD, and general medical devices. **IVDR 2017/746 is excluded.**
- **Working Directory:** the repository root (wherever it is cloned). All code paths are relative to the folder that contains `update_mdr.py` (`BASE_DIR = os.path.dirname(os.path.abspath(__file__))`), so the folder can be deployed to any path or PC.
- **Runtime Environment:** Windows, **Python 3.14 (64-bit)** in a project-local virtual environment (`.venv`), local ChromaDB, Streamlit web UI, and the Google GenAI SDK (`google-genai`).
  - The launcher `.bat` creates `.venv` with `py -3.14` and installs the pinned `requirements.txt` on first run. It reinstalls only when `requirements.txt` changes.
  - Never rely on a bare `python`. On the original PC, `python` resolves to 3.15, which has no dependencies installed.
- **LLM access (4 connection methods, §5.2):** company account via **Gemini CLI** (`gemini_cli`, default when the CLI is installed), company account via **Vertex AI** (`vertex`, gcloud ADC login), **Gemini API key** (`gemini`), or **OpenAI API key** (`openai`). The company Enterprise Gemini contract may forbid API keys and allow only account (SSO) login, which is why the two account-based methods exist. Confidentiality of indexed documents is not a concern under the Enterprise contract (user-confirmed).

---

## 2. User Requirements & Design Principles (User Perspective)

### 2.1. Target User
- Medical device company RA (Regulatory Affairs) practitioners who work on EU CE certification (EU MDR) for X-ray systems, SaMD, and similar devices.
- They are **not developers**. They think and report in Korean, but the source documents are in English.

### 2.2. Problems This System Solves
1. EU MDR articles and annexes are extensive, and their practical interpretation is scattered across dozens of MDCG documents. Answering one question normally requires cross-reading many documents.
2. There is a language gap: the sources are in English, while the user's questions and reports are in Korean.
3. The regulation is consolidated (amended) periodically, and MDCG documents are revised or newly published. It is hard to know whether a document is current.
4. Review results end up in personal notes or e-mails and do not accumulate as organizational knowledge.

### 2.3. Functional & Quality Requirements
| ID | Requirement | Level | Implemented in |
|---|---|---|---|
| R1 | A **Korean** natural-language query retrieves the relevant **English** EU MDR provisions and MDCG pages together (cross-lingual retrieval). | MUST | Multilingual embedding model (§4.3) |
| R2 | **Grounded answers only.** The answer must be based solely on the retrieved excerpts. It must not speculate beyond them. | MUST | System prompt in `app.py` |
| R3 | **Separate binding law from interpretive guidance.** Cite EU MDR Article/Annex numbers (legally binding) separately from MDCG document codes and page numbers (interpretive). | MUST | System prompt + metadata (`doc_type`, `section`) |
| R4 | **Verifiability.** Show the retrieved source text verbatim next to the AI answer so the user can check it. The AI is a research assistant. It is not the final authority. | MUST | Right column of the UI; verbatim blocks in saved MD/Word |
| R5 | **Actionable output.** Answers are in Korean, structure conditions and exceptions, and give key English terms in parentheses. They must be usable directly for technical documentation, classification, translation scoping, change management, and similar tasks. | MUST | System prompt |
| R6 | **Accuracy over speed or cost.** Never fall back to lite or lower-tier models (Gemini `*-lite`, OpenAI `*-mini`/`*-nano`). If all approved models fail, show an error instead of a degraded answer. | MUST | `GEMINI_MODELS` policy, OpenAI model filter (§5.2) |
| R7 | **Model transparency.** The UI always shows the configured provider and model, and the provider and model that actually produced each answer, including when a fallback happened. | MUST | Sidebar + chip above the answer |
| R19 | **Connection methods and switching.** The sidebar offers 4 AI connection methods (Gemini CLI account login, Vertex AI account login, Gemini API key, OpenAI API key). Each account method has a status check and a **연결 테스트** (connection test) button, so the user can tell on site which one the company contract supports. Switching is manual only; the system never changes provider silently. | MUST | `PROVIDERS`, `generate_answer()`, `test_ai_connection()` (§5.2) |
| R8 | **Currency of sources.** One command syncs the latest MDR consolidated text and revised MDCG PDFs, and alerts the user to new MDCG codes. Each answer and saved record carries the regulation version. | MUST | `update_mdr.py`, `db_version.json` |
| R9 | **Auto-save of reviews + user-driven export (changed 2026-11-18 by user).** Every *successful* AI review is saved automatically as MD to `results\saved\` (no toggle). Search-only results are not auto-saved. Distribution files are created only when the user clicks **MD + Word 내보내기** (export). | MUST | `run_analysis()` auto-save, export panel |
| R10 | **Saved vs exported.** `results\saved\` = automatic review history (MD). `results\exported\` = user-curated exports, MD + Word side by side, for team distribution and downstream AI agents. Filenames are `YYYYMMDD_HHMMSS_<title>` (to the second; `_2`, `_3` added on collision). | MUST | `save_review_files(target_dir, with_word)` |
| R11 | **Local-first.** The vector DB and embeddings run locally. Only the query and the retrieved excerpts are sent to the Gemini API. | MUST | ChromaDB + local SentenceTransformer |
| R12 | **Extensible corpus.** Company SOPs, technical documents, and harmonised standards (PDF) can be added and are searched in the same way. | SHOULD | Drop PDFs into `mdcg_pdfs\` and re-run sync |
| R13 | **One-click launch for non-developers.** Double-clicking `Start_EU_MDR_Review_Assistant.bat` starts the app. If startup fails, the window stays open and shows the error. | MUST | `Start_EU_MDR_Review_Assistant.bat` (with `pause`) |
| R14 | **Resilience to transient API errors.** Transient errors (503/429/500/504) are retried with backoff before the system gives up. | SHOULD | Retry loop in `app.py` (§5.2) |
| R15 | **Works from a completely blank state.** On a fresh deployment there is no `.venv`, no vector DB, no PDFs, and no `db_version.json`. The launcher sets up the environment, and the UI offers a single **구축 시작** button that downloads everything and builds the DB. | MUST | `.bat` bootstrap + `render_update_panel()` (§5.0) |
| R16 | **Freshness check on startup, one-click update.** When the app starts, it checks automatically whether the MDR and MDCG sources are current. If they are not, it prompts the user and updates with one click. | MUST | `check_for_updates()` + update panel |
| R17 | **The freshness check costs no LLM tokens.** It uses plain HTTP only (two requests, about 10 s) and is cached for 6 h per local-state signature. | MUST | `cached_update_check()` |
| R18 | **Never degrade the DB on failure.** If a remote check fails, the status is "확인 불가" (cannot verify), not "outdated". A build is done in a staging collection and is swapped in only after validation. On failure, the existing DB stays untouched. | MUST | `build_database()` (§4.4) |

### 2.4. Open Points / Assumptions (Not Yet Confirmed by the User)
- **[ASSUMPTION]** Final responsibility for every regulatory conclusion lies with the human user. The system only drafts answers and finds evidence.
- **[ASSUMPTION]** The downstream AI agent that will consume `results\exported\*.md` is not yet specified. Keep the MD format stable and self-contained (§5.3) until the user specifies it.
- **[ASSUMPTION]** The current Q&A is stateless: each query is independent, with no conversation memory. Whether the user needs follow-up (multi-turn) questions has not been confirmed.

### 2.5. Rules for Agents Modifying This System
1. Do not weaken any **[MUST]** requirement. In particular, do not add lite-model fallbacks, and do not let answers go beyond the retrieved sources.
2. Keep the `doc_type` values (`EU_MDR`, `MDCG_GUIDANCE`), the collection name, and the `results\saved` / `results\exported` separation unchanged (§6.3).
3. UI text and answers are in **Korean**. Keep legal identifiers and key terms in English.
4. Never print, log, or commit the API key. It is stored only in `.env`.
5. Verify changes with `py -3.14 -m py_compile app.py` at a minimum, and preferably by launching the UI.

---

## 3. Directory & File Structure

```text
EU_MDR_Review_Assistant\   (repository root)
 ├── .env                     # [Config] Local storage for GEMINI_API_KEY (auto-loaded by app.py). Never share.
 ├── README.md                # [Docs] Requirements, architecture & handover specification for AI agents
 ├── GEMINI.md                # [Docs] Mandatory rules for Gemini agents (sync README, non-negotiable constraints)
 ├── .gitignore               # [Repo] Excludes .env (API keys), .venv, generated data (DB, PDFs, db_version.json), results/
 ├── .gitattributes           # [Repo] Forces CRLF for *.bat (cmd.exe label handling)
 ├── LICENSE                  # [Repo] MIT License
 ├── docs\index.html          # [Docs] Standalone introduction page (problem → approach → how it works, 8-step query animation)
 ├── requirements.txt         # [Deps] Pinned package versions (verified on Python 3.14)
 ├── Start_EU_MDR_Review_Assistant.bat # [Launcher] Creates .venv (py -3.14) + installs deps on first run, then `streamlit run app.py`
 ├── .venv\                   # [Env] Project-local virtual environment (auto-created; delete it to reinstall from scratch)
 ├── .streamlit\config.toml   # [UI] Theme (light/dark palettes, Pretendard font, radius), viewer toolbar
 ├── assets\logo_mark.svg     # [UI] Original logo mark (document + gold magnifier with check), used in sidebar brand card and header
 ├── .build.lock              # [State] Exists only while a DB build is running (prevents concurrent builds; stale after 2 h)
 ├── update_mdr.py            # [ETL & Sync] Official EUR-Lex / EC crawler, parser, and ChromaDB builder
 ├── app.py                   # [UI & RAG] Streamlit web interface, RAG pipeline, and dual-format exporter
 ├── tools\                   # [QA] Retrieval evaluation: eval_retrieval.py (30-question gold set), eval_db.py
 ├── db_version.json          # [State] Metadata tracking consolidated MDR date, chunk counts, PDF URLs, and MDCG codes
 ├── mdr_chroma_db\           # [Vector DB] Persistent ChromaDB storage (Collection: `eu_mdr_2017_745`)
 ├── mdcg_pdfs\               # [Raw Source] Downloaded official MDCG & EC Language Requirement PDFs (17+ core docs)
 └── results\                 # [Output] saved\ (auto-saved review history, MD) · exported\ (user exports, MD + Word). Older layouts (saved_records\saved, exported, md, word) are moved here at startup (_migrate_legacy_records), then saved_records\ is removed if empty.
      ├── saved\              #   - Auto-saved review history: one MD per successful AI review (YYYYMMDD_HHMMSS_<title>.md)
      └── exported\           #   - User exports: MD (knowledge-base input for downstream agents) + Word (team distribution), same base name
```

---

## 4. Data Pipeline & Implementation Details (`update_mdr.py`)

### 4.1. Automated Version Detection & Sync (`check_for_updates()`, no LLM tokens)
1. **EU MDR Consolidated Text (EU Publications Office "Cellar"):**
   - **Do not scrape eur-lex.europa.eu.** Since Nov 2026 it returns an empty `HTTP 202` bot challenge (AWS WAF) to automated clients. The old scraper silently fell back to the 2017 original text, which could wipe the law from the DB.
   - The latest version comes from the Cellar SPARQL endpoint `https://publications.europa.eu/webapi/rdf/sparql`, which lists CELEX IDs `02017R0745-YYYYMMDD`; the system takes the maximum date.
   - The full text comes from `http://publications.europa.eu/resource/celex/<CELEX>` with `Accept: application/xhtml+xml` and `Accept-Language: eng` (~1.6 MB XHTML).
   - Validation: HTTP 200, an HTML content type, at least 500 KB, and at least 200 parsed sections (`MIN_MDR_CHUNKS`; a normal parse gives about 290). Any failure raises an error. **There is no silent fallback.**
   - The human-readable link stored in metadata is `https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:<CELEX>`.
2. **MDCG & EC Guidance Documents (European Commission):**
   - Fetches `https://health.ec.europa.eu/medical-devices-sector/new-regulations/guidance-mdcg-endorsed-documents-and-other-guidance_en` once per check.
   - **Tracked documents:** `TRACKED_GUIDANCES` in `update_mdr.py` holds 17 core documents, plus `extra_tracked` in metadata for newly added MDCG documents. A tracked entry may define `landing` + `file_hint` when the EC page links to a publication page instead of a PDF. For example, the Language Requirements PDF lives on a separate publication page.
   - **Update reasons:** `missing` (no local file), `revision` (the download URL/UUID changed), or `unverified` (the metadata comes from an older build without `guidance_urls`, so the file is re-downloaded once to guarantee it is current).
   - **New guidance discovery:** `MDCG 20XX-XX` codes on the page that are not in `known_mdcg_codes`. IVDR-only documents are excluded. On the first build, every code on the page becomes the baseline.
   - Tracked documents that cannot be found on the page are reported as `unmatched`. Their existing local files are kept.
3. **Local changes:** if the PDFs in `mdcg_pdfs\` differ from `pdf_files` in the metadata (for example, after a user adds a company PDF), the check reports that a re-index is needed.
4. **Parser upgrades:** if `parser_version` in the metadata is lower than `PARSER_VERSION` (a missing value counts as 1), the check sets `parser_outdated` and offers a re-index.

### 4.4. Safe Build (`build_database()`)
1. Acquires `.build.lock`. A second concurrent build is refused.
2. Downloads PDFs. Each PDF is validated by its `%PDF-` signature, written to `*.part`, and then moved into place atomically. A failed file is reported and retried at the next check, because its URL is not recorded.
3. Fetches and parses the MDR text, then parses all PDFs.
4. Embeds everything into the collection `eu_mdr_2017_745__staging`, and verifies the count.
5. Swaps: deletes `eu_mdr_2017_745`, then renames the staging collection. If the process is interrupted between these two steps, `recover_collection()` restores the collection from staging on the next start.
6. Writes `db_version.json` atomically. The fields are `version_date`, `celex`, `source_url`, `built_at`, the chunk counts, `pdf_files`, `guidance_urls`, `extra_tracked`, and `known_mdcg_codes`.
- Any failure raises `BuildError`. The existing collection and metadata remain untouched.
- CLI: `py -3.14 update_mdr.py [--yes] [--rebuild]` (inside `.venv`: `.venv\Scripts\python.exe update_mdr.py`).

### 4.2. Hierarchical Chunking Strategy
- **EU MDR XHTML (`doc_type: "EU_MDR"`), `PARSER_VERSION = 2`:**
  - Parsed with `BeautifulSoup` (`lxml`) along structural boundaries: `Preamble/Recitals`, `CHAPTER`, `Article N`, and `ANNEX I–XVII`.
  - **Innermost blocks only** (`_iter_text_blocks()`): the Cellar XHTML nests `<div>`s deeply. Reading every `div` duplicated list items 3–4× and dumped the whole regulation into the first chunk (parser v1: 2.6M chars, Recitals chunk 593k chars). v2 yields each innermost block once. A `table` or a list row (`div.grid-container`, marker column + text column) is yielded as one line, such as `— class IIb if …`.
  - Text normalization: NBSP becomes a space, and consolidation markers (`▼B`, `▼M1`, `►C1`, `◄`) are removed.
  - Chunking (`PARSER_VERSION = 3`): split at a sub-clause boundary (`(1)`, `1.`, `a)`, `(a)`, `Rule N`, `—`) once a chunk exceeds `CHUNK_TARGET` = 1,000 chars, and unconditionally before `CHUNK_HARD_MAX` = 1,800 chars. The parent section title is kept on each continuation chunk. The 1,000-char target was chosen by evaluation (§4.5) to fit the embedding model's 512-token window.
  - Verified result (CELEX 02017R0745-20261109, v2 parse): about 604k chars in total. The parse covers 99.92% of the body words (the missing words are CHAPTER headings, which are kept in `chapter` metadata). Articles 1–123, Annexes I–XVII, and Rules 1–22 are all present. The v3 chunking yields 545 MDR chunks and 1,813 chunks in total.
  - **When you change the parsing or chunking, increment `PARSER_VERSION`.** Existing installations then see "문서 처리 방식이 개선되었습니다 (재색인 필요)" in the startup update panel and can re-index with one click.
  - The earlier limitation is resolved. The old MiniLM model read only 128 tokens of a ~2.5k-char chunk, so mid-chunk clauses such as Annex VIII Rule 19 ranked below 50th. With E5-large and 1,000-char chunks, a natural-language question finds Rule 19 at rank 1.
- **MDCG / EC PDFs (`doc_type: "MDCG_GUIDANCE"`):**
  - Extracted page by page with `pypdf.PdfReader`.
  - Pages longer than `PDF_CHUNK_SIZE` = 1,000 characters are split with a `PDF_CHUNK_OVERLAP` = 150-character overlap. Each chunk keeps `source_name` (the filename) and `Page X` metadata for exact citation.

### 4.3. Embedding & Vector Storage
- **Database:** `chromadb.PersistentClient(path=os.path.join(BASE_DIR, "mdr_chroma_db"))` (`get_client()` in `update_mdr.py`)
- **Collection Name:** `eu_mdr_2017_745`
- **Embedding Model:** `intfloat/multilingual-e5-large` (`EMBED_MODEL` in `update_mdr.py`). It runs locally on CPU through `sentence-transformers`, is about 2.2GB, and is downloaded from HuggingFace on first use. It supports cross-lingual retrieval between **Korean queries** and **English regulatory texts** without external API calls (R1, R11).
  - E5 requires the prefixes `query: ` for questions and `passage: ` for documents. `EMBED_MODELS` (the registry) holds the prefix and `max_seq` (512) for each model.
  - Embeddings are computed by the `Embedder` class and passed explicitly (`add(embeddings=...)`, `query(query_embeddings=...)`). The collection is created with `embedding_function=None` and `hnsw:space = cosine`, and the vectors are L2-normalized.
  - **A query must use the model that built the index.** `db_version.json` records `embed_model`, and the app selects its query model through `query_embedder_spec()`. A legacy DB without `embed_model` used un-normalized MiniLM vectors, so it is queried the same way until it is re-indexed.
  - Changing `EMBED_MODEL` (or `PARSER_VERSION`) makes the startup check report "검색 정확도 개선이 있습니다 (재색인 필요)" (a search-accuracy improvement is available, re-index needed).

### 4.5. Retrieval Evaluation (`tools\`)
- `tools\eval_retrieval.py` holds a gold set of 30 Korean RA questions, each mapped to its correct EU MDR article, annex, or rule, or MDCG document. It reports Recall@6 and MRR. `tools\eval_db.py` runs the same gold set against a built DB through the app's actual query path:
  ```powershell
  .venv\Scripts\python.exe tools\eval_db.py .
  ```
- Results on 2026-11-18 (CPU, 1,813 or 982 chunks):

  | Model | 2,500-char chunks | 1,000-char chunks | Model size | Full re-index time |
  |---|---|---|---|---|
  | paraphrase-multilingual-MiniLM-L12-v2 (old) | 70% | 70% | 470MB | ~10 s |
  | intfloat/multilingual-e5-base | 77% | 83% | 1.1GB | ~80 s |
  | **intfloat/multilingual-e5-large (adopted)** | 90% | **93% (MRR 0.84)** | 2.2GB | ~5–6 min |
  | BAAI/bge-m3 | 90% | 93% (MRR 0.84) | 2.3GB | ~5 min |
- Verified on the real pipeline: Recall@6 = 93%, MRR = 0.843, average query embedding plus search time = 43 ms.
- **Run `eval_db.py` before and after any change to chunking, the embedding model, or retrieval logic.** Do not ship a change that lowers Recall@6.
- **Metadata Schema per Chunk:**
  ```json
  {
    "doc_type": "EU_MDR | MDCG_GUIDANCE",
    "source_name": "EU MDR (2017/745) | <PDF_Filename_Without_Ext>",
    "chapter": "<Chapter Name | MDCG / EC Guidance>",
    "section": "<Article N | Annex X | Page N>",
    "title": "<Full Section or Document-Page Title>"
  }
  ```

---

## 5. RAG Query & Export Workflow (`app.py`)

### 5.0. Startup Flow & UI
1. On every new session, `cached_update_check(signature)` runs `check_for_updates()`. The result is cached for 6 h, and the cache key is a signature of the metadata version, the build time, and the local PDF list. The first render therefore waits about 10 s; later reruns are instant.
2. `render_update_panel()` shows one of the following:
   - **No DB:** a "검색 데이터 초기 구축" panel with a single **구축 시작** button. The search UI is hidden until the build finishes.
   - **Updates available:** a list of tagged items (EU MDR, 개정판, 누락, 버전 재확인, 신규), a checkbox for new MDCG documents, **지금 업데이트**, and **나중에**.
   - **Check failed:** a warning that the app continues on the existing DB.
3. A build runs inside `st.status` with a progress bar and a live log. After it succeeds, the check cache is cleared and the app reruns.
4. Branding: `assets/logo_mark.svg` is an original mark: a regulation document with a gold magnifier and check, meaning "find and verify the source text". It deliberately does not use the EU flag or the 12-star emblem, to avoid implying official EU status.
   - The mark appears **only in the main header**, next to the title. A sidebar brand card was tried and then removed at the user's request, because it duplicated the header and cost about 170px of sidebar height.
   - **`st.html` strips `<svg>` tags**, so the logo is embedded as `<img class="ra-logo" src="data:image/svg+xml;base64,…">` (`_load_logo_img()`). If the file is missing, the app still runs without the logo.
   - Section labels (`.ra-label`) carry an **accent bar** ("포인트 바"): a solid `#4A7BD0` mid-tone blue, 3px × 0.8rem, which reads well in both themes. The earlier navy-to-gold gradient was rejected by the user as dated, so don't reintroduce gradients there. The label text uses a mid-gray that works in both themes.
5. **Target resolution: 1920×1080.** The usable browser viewport is about **1920×945**. Nothing may scroll horizontally. Long content (the AI answer, a saved record) may scroll vertically.
   - **Top padding:** `.block-container { padding-top: 4.2rem }` must stay larger than Streamlit's top header bar. The bar is about 56px tall, painted in the page color, and drawn above the content (z-index 999990). With 2.4rem, the bar covered the top 7px of the header logo. Now the logo starts at y = 76px, 20px clear of the bar.
   - **In effect:** code blocks in the main area wrap (`[data-testid="stMain"] pre { white-space: pre-wrap }`), so the record viewer no longer scrolls sideways. Long MDR lines previously produced blocks 2,988px wide.
   - **Tried and reverted (2026-11-18):** a compressed layout was tested. It moved saved records to the top of the sidebar, put scope and buttons on one row with a 72px question box, used a fixed-height scrolling source panel, and widened the main area to 1,640px. It fit 1920×945 with no page or sidebar scroll, but **the user rejected it** and the previous layout was restored. Do not reintroduce it without asking.
   - With the current layout at 1920×945, the sidebar scrolls (about 1,390px of content) and the "저장된 검토 기록" list sits below the fold. The search-results page also scrolls vertically. These are known, accepted tradeoffs.
6. UI conventions: theme colors come from `.streamlit/config.toml` (`[theme.light]` and `[theme.dark]`). Custom HTML uses the `.ra-*` CSS classes defined at the top of `app.py` and translucent colors, so it works in both themes. Icons use Streamlit Material icons (`:material/...:`), not emoji. Every dynamic text inserted into HTML must go through `esc()` (`html.escape`).

### 5.1. Retrieval & Filtering
- `retrieve()` in `app.py` returns `top_k` sources (default 6), filtered by the scope selected in the segmented control: **전체**, **EU MDR 법령**, or **MDCG 가이던스**. The `SCOPES` dict maps each label to the scope name written into saved records and to the `where` filter.
- **Identifier pinning (deterministic, no tokens):** `extract_identifiers()` detects `Rule N`/`N번 규칙`, `Article N`/`제N조`, `Annex X`/`부속서 X`, and `MDCG YYYY-N` in the question. The matching chunks are fetched exactly and placed first with a 📌 mark, up to `MAX_PINNED` = 4:
  - a Rule is fetched with a `where_document` `$regex` on the rule's heading line;
  - an Article or Annex is fetched through `section` metadata (an Annex is skipped when a Rule is also given);
  - an MDCG code is resolved by a semantic search restricted to that document's `source_name`.
- The remaining slots are filled by semantic search (E5 query embedding), skipping duplicates.

### 5.1b. Two-Step Search → Analysis Flow (search-only mode)
- The form has two buttons, ordered by primary function because this is fundamentally an AI-review tool:
  1. **검색 + AI 검토** (search + AI review) is first and is the primary (emphasized) button. Ctrl+Enter triggers it, since Streamlit binds Ctrl+Enter to the form's first submit button. It searches and analyzes in one step.
  2. **근거 검색** (search only) is second and is a secondary button. It runs only the local search, costs no LLM tokens, and takes about a second.
  - Keep this order. The user explicitly asked for the AI analysis to come first.
- **Terminology (user decision):** every user-facing label uses **검토** (review), never 분석 (analysis). This matches the program name (규제 검토 어시스턴트), the result labels (검토 답변, 검토 결과 저장), and RA usage (규제 검토 의견). Internal identifiers such as `run_analysis` and `analyzed_at` are unchanged.
- Each source has a checkbox, checked by default. **선택한 근거 N건으로 AI 검토** (review with the N selected sources) sends only the checked sources to the LLM. After an analysis, changing the selection offers **다시 분석** (re-analyze) without searching again.
- The prompt numbers each excerpt `[n]` with its on-screen number and asks the model to cite those numbers. When the excerpts cannot answer the question, the model must say "제공된 원문에서 확인되지 않음" (not found in the provided sources).
- State lives in `st.session_state["last_qa"]` (`query`, `scope`, `results` [`id`, `doc`, `meta`, `pinned`], `nonce`, `searched_at`, `search_sec`, `answer`, `model`, `provider`, `used`, `analyzed_at`, `analysis_sec`, `error`). Checkbox keys are `use_{nonce}_{i}`.
- **Run feedback:** an identical repeated search returns identical results, so each run shows its own timestamp. A caption above the sources reads `HH:MM:SS 검색 · N.N초 · 근거 N건`, and the answer chip appends `· HH:MM:SS 검토 · N.N초`. No run counter or result diffing is used; the user judged the timestamp and elapsed time sufficient.

### 5.2. LLM Synthesis (Stateless RAG) & Model Policy
- Uses `google.genai.Client` with a strict grounding prompt. The prompt requires explicit citations of both binding EU MDR articles/annexes and interpretive MDCG guidance pages (R2, R3, R5).
- **Model list (single source of truth):** the constant `GEMINI_MODELS` near the sidebar section of `app.py`.
  ```python
  GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash"]  # [0] = default, others = same-tier fallbacks
  ```
  - To change models, edit **only** this list. The sidebar display and the call loop both read it, so the UI updates on the next Streamlit rerun.
  - **[MUST]** Only high-capability models may be listed. Never add `*-lite` or lower-tier models (R6).
- **Persisted user settings (`.env`):** `LLM_PROVIDER` (`gemini_cli`/`vertex`/`gemini`/`openai`), `GEMINI_CLI_MODEL`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` (default `global`), `GEMINI_API_KEY`, `OPENAI_API_KEY`, `OPENAI_MODEL`, and `TOP_K` (the "근거 문서 수" slider, 3–12, default 6). They are written with `write_env_value()`, which preserves other lines, and restored on the next start. Any new sidebar setting that should survive a restart must follow the same pattern.
- **Company-account connection methods (no API key):**
  - **Gemini CLI (`gemini_cli`).** The app runs the installed `gemini` command (`shutil.which`) once per request: `gemini -p "Follow the instructions above and answer." -o json --approval-mode plan -m <model>`, with the full prompt on stdin (`-p` text is appended to stdin). Safety and correctness rules:
    - It runs in a **fresh empty temp folder** with `GEMINI_CLI_TRUST_WORKSPACE=true`, which headless mode requires. This keeps project files out of reach. `--approval-mode plan` is read-only, so no edits and no shell commands. There is a 300 s timeout.
    - `GOOGLE_CLOUD_PROJECT` is passed through when set; some company logins need it.
    - The answer comes from the JSON `response`. The **actually used model** comes from `stats.models` (the model with successful requests and the most candidate tokens), because the CLI can silently switch models. If the used model differs from the requested one, the answer chip shows a warning. **If the used model is a lite model, the answer is discarded (R6).**
    - The model list comes from `gemini --list-models`, with `auto` and `*lite*` excluded and newest first. A **직접 입력** (manual entry) option exists because the list can include models the account cannot use (observed: `gemini-2.5-pro` listed but "no longer available"); lite names are rejected there too.
    - `_classify_cli_error()` reads the CLI's JSON error (often on **stderr**) and maps it, in this order, to daily quota exhausted → choose another model; model not available → choose or enter another model; 429/503 → transient, retry later; auth → run `gemini`, choose **Login with Google**, sign in with the company account (SSO).
    - Costs observed on 2026-11-18: about **9k tokens of CLI system-prompt overhead per call**; about 27 s for a connection test and 33 s for a 7-source review.
    - The status check reads only `selectedType` in `~/.gemini/settings.json` (the auth method). It never reads credential files, and the app **never reuses the CLI's stored OAuth tokens directly**, which would be unofficial and fragile.
  - **Vertex AI (`vertex`).** `genai.Client(vertexai=True, project=GOOGLE_CLOUD_PROJECT, location=GOOGLE_CLOUD_LOCATION or "global")` with Application Default Credentials from `gcloud auth application-default login` (company SSO).
    - The status shows whether gcloud is installed and whether ADC is found (`google.auth.default()`).
    - `_classify_vertex_error()` maps errors: no credentials → login command; 403 → no Vertex AI permission, likely a Gemini CLI-only contract; SERVICE_DISABLED → ask IT; 404 → model or region.
    - **Not yet verified against a real company project**; no company GCP account was available.
  - Verified on 2026-11-18 on the development PC, whose CLI used API-key auth, through the same CLI path: connection test OK; a real Korean review with [n] citations was returned and auto-saved; the quota error and the model-not-found error showed the correct messages.
- **AI provider (R19):** the sidebar has a segmented control with **Google Gemini** and **OpenAI**. The choice is saved as `LLM_PROVIDER=gemini|openai` in `.env`.
  - **OpenAI** uses the OpenAI Platform API key (`OPENAI_API_KEY`, `sk-…`). This key is separate from a ChatGPT subscription. Calls go through the Responses API (`client.responses.create(model, input=prompt)`, read via `.output_text`), with no `temperature` because GPT-5-class reasoning models reject it.
  - The OpenAI model list is fetched live with the user's key (`models.list()`, cached for 1 h). The list keeps only IDs matching `^gpt-<version>$`, which excludes mini/nano models, dated snapshots, and audio/realtime/codex/specialized models (R6), and sorts them newest first. The selected model is saved as `OPENAI_MODEL`. If the list cannot be fetched, a manual model-name field is shown.
  - "Codex" is OpenAI's coding agent product, not the general text model this app needs. Use the general GPT models through the same API key.
  - All provider logic lives in `generate_answer(provider, prompt, ...)` and `_with_retry()` / `_is_transient()` in `app.py`.
- **Retry & fallback:** each model is tried up to 4 times, with exponential backoff (2s, 4s, 8s), on transient errors (`503`/`UNAVAILABLE`, `429`/`RESOURCE_EXHAUSTED`, `500`, `504`). After that, the next model in `GEMINI_MODELS` is tried. Non-transient errors (e.g., an invalid API key) are raised immediately. If every model fails, `st.error("API 호출 오류: ...")` is shown.
- **Model display (R7):**
  - Sidebar: an info box shows the default model and the fallback models.
  - Above the answer: `🤖 응답 모델: <model>`. If the answering model is not `GEMINI_MODELS[0]`, a warning says that a fallback occurred.
  - The model that produced each answer is stored in `st.session_state["last_qa"]["model"]`.

### 5.3. Human-in-the-Loop Dual Export
- Successful AI reviews are auto-saved to `saved\` (MD only; the answer shows "자동 저장됨 · <file>"). Exports to `exported\` happen only on the user's click (R9).
- After an export, the app stores the file paths in `st.session_state["save_notice"]` and calls `st.rerun()`. The sidebar's records list is drawn before the export button runs, so without the rerun the new file would not appear until the next interaction. The same applies to auto-saves from "검색 + AI 검토": the app reruns after saving so the new record shows immediately.
- **MD + Word 내보내기** writes two files with the same base name `YYYYMMDD_HHMMSS_<Sanitized_Title>` into `results\exported\`:
  - `*.md`: Clean, structured Markdown containing the query, the AI review, and the verbatim retrieved source blocks. Designed for ingestion by external AI agents.
  - `*.docx`: Styled Microsoft Word report (`python-docx`, Malgun Gothic font, metadata table, indented verbatim citations), ready for stakeholder distribution.
  - Both files record the **검토 모델** (review model; records saved before 2026-11-18 say 분석 모델), e.g. `Google Gemini · gemini-3.8-flash`, or "없음 (근거 검색만 수행)" for a search-only record.
  - **Search-only records can be saved.** When there is no analysis, the checked sources are saved and the analysis section says "(AI 검토 없음 — 검색된 원문 근거만 저장한 기록입니다.)". When there is an analysis, the sources that were actually used for it are saved.
  - Saved sources keep their **on-screen numbers** (`source_numbers`), so the `[n]` citations in the analysis still match after some sources were unchecked.

---

## 6. How Another AI Agent Can Access & Extend This System

### 6.1. Direct Data Access (Read-Only / Analysis)
- **To read RA review history:** curated exports are in `results\exported\*.md`; the full automatic history is in `results\saved\*.md`. Each file is self-contained, with regulatory version stamps and verbatim source excerpts.
- **To query the vector DB programmatically (Python 3.14):**
  ```python
  import chromadb
  from chromadb.utils import embedding_functions

  # run from the repository root
  client = chromadb.PersistentClient(path="mdr_chroma_db")
  emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
      model_name="paraphrase-multilingual-MiniLM-L12-v2"
  )
  col = client.get_collection(name="eu_mdr_2017_745", embedding_function=emb_fn)
  results = col.query(query_texts=["Annex VIII Rule 10 ionising radiation"], n_results=5)
  ```
- **To check the current DB version and the indexed documents:** Read `db_version.json` in the repository root.

### 6.2. Execution Commands (always use the project `.venv`)
- **Launch the web UI (end users):** double-click `Start_EU_MDR_Review_Assistant.bat`. On first run, it creates `.venv` and installs the dependencies, which takes several minutes. The data download and DB build are then started from the web UI.
  - The launcher filename is deliberately **ASCII** (renamed from the Korean `EU_MDR_검색기_실행.bat` on 2026-11-18), so it does not get garbled when copied via zip or e-mail to other PCs. For a Korean label, create a desktop **shortcut** named "EU MDR 규제 검토 어시스턴트" on each PC; shortcut names are created locally and do not get garbled.
- **Launch the web UI (command line):**
  ```powershell
  .venv\Scripts\python.exe -m streamlit run app.py
  ```
- **Check, sync, or rebuild the vector DB from the command line:**
  ```powershell
  .venv\Scripts\python.exe update_mdr.py            # check, then ask before updating
  .venv\Scripts\python.exe update_mdr.py --yes      # update without asking (includes new MDCG docs)
  .venv\Scripts\python.exe update_mdr.py --rebuild  # force a full rebuild
  ```
- **Reinstall the environment from scratch:** delete the `.venv` folder and run the `.bat` again.
- **Verified blank-state test (2026-11-18):** with only the 4 code files in an empty folder, the `.bat` created `.venv`, installed the pinned packages, and started the app. **구축 시작** then downloaded 17 PDFs and the MDR consolidated text `02017R0745-20261109`, and built 1,070 sections in about 1 minute. The embedding model was already in the HuggingFace cache, so that download was not part of the test.

### 6.2b. Repository & Deployment (GitHub)
- Public repository: `jinny-cho/EU_MDR_Review_Assistant`. Only **source and docs** are committed (14 files on 2026-11-18): `app.py`, `update_mdr.py`, `requirements.txt`, `Start_EU_MDR_Review_Assistant.bat`, `README.md`, `GEMINI.md`, `.streamlit/config.toml`, `assets/logo_mark.svg`, `tools/*.py`, `.gitignore`, `.gitattributes`, `LICENSE`, `docs/index.html`.
- **Never committed** (see `.gitignore`): `.env` (real API keys and settings), `.venv/`, `mdr_chroma_db/`, `mdcg_pdfs/`, `db_version.json` (all regenerated on first run from official sources, R15), `results/` (users' review records), caches, and local tool artifacts.
- **Introduction page:** `docs/index.html` is a standalone, Korean-language page for people new to the project. It covers the problem, requirements, approach, and how the tool works, with an 8-step animation of how one question flows from identifier check to embedding, vector search, source selection, AI review, and auto-save. It also shows measured results and limitations. It needs only Google Fonts online and has no other dependencies. To publish it, enable GitHub Pages with the `/docs` folder. Its source titles and similarity values are labeled as simplified examples. **Update it when the numbers it cites change** (chunk counts, Recall@6, timings).
- A fresh clone works with no data: run the `.bat`, which creates `.venv` and installs packages, then click **구축 시작** (start build) in the UI.
- Before every push, check the outgoing file list (`git status`) and scan it for key-like strings. **Never write machine-specific absolute paths or personal names into committed files.**

### 6.3. Safe Extension Guidelines for Future Agents
- **Adding custom company documents (SOPs, technical docs, harmonised standards):** Place additional `.pdf` files in `mdcg_pdfs\`. The next startup check detects the change and offers a one-click re-index. You can also click **재구축** in the sidebar, or run `update_mdr.py --rebuild`.
- **Adding a tracked MDCG document:** add an entry to `TRACKED_GUIDANCES` in `update_mdr.py`. New MDCG documents that the user accepts through the UI are tracked automatically through `extra_tracked`.
- **Never reintroduce silent fallbacks** in the update path. If the latest data cannot be verified, raise `UpdateCheckError` or `BuildError` and keep the existing DB.
- **Preserving downstream compatibility:** Do not change the `results\saved\` / `results\exported\` separation. Do not change the `doc_type` metadata values (`EU_MDR`, `MDCG_GUIDANCE`) in ChromaDB, because the filters in `app.py` rely on these exact values.
- **Changing the LLM model:** Edit only `GEMINI_MODELS` in `app.py`, and follow the rules in §5.2.
- **Before any change:** Re-read Section 2 and confirm that the change does not violate any [MUST] requirement.

---

## License

This project is licensed under the **MIT License**; see `LICENSE`. The EU MDR text and MDCG/EC guidance documents are **not** included in the repository. They are downloaded at runtime from their official sources (EU Publications Office, European Commission) and remain subject to their own terms of reuse.
