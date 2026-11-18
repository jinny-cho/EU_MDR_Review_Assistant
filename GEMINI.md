# GEMINI.md — AI Agent Operational & Documentation Guidelines

> **System Notice for Gemini Agents:** This file defines mandatory project-level instructions for this repository (paths below are relative to the repository root). Always read `README.md` before modifying any code, and strictly enforce the documentation synchronization rules below whenever changes are made.

---

## 1. Project Context & Core Architecture Reference
- **Primary Reference:** `README.md` in the repository root is the single source of truth for requirements (Section 2, R1–R19), architecture, ChromaDB schema, data pipelines, and UI workflows. Read it before planning or executing code edits.
- **Core Tech Stack:** Python 3.14 in a project-local `.venv` (created by `Start_EU_MDR_Review_Assistant.bat`), Streamlit (`app.py`), BeautifulSoup4 + pypdf + ChromaDB + sentence-transformers (`update_mdr.py`), Google GenAI SDK (`google-genai`), OpenAI SDK, optional Gemini CLI / Vertex AI for company-account access.

---

## 2. Mandatory Rule: Automatic `README.md` Synchronization
Whenever you modify, add, or remove any software implementation in this repository, **you MUST update `README.md` in the same turn/task without waiting for the user to ask.**

### 2.1. Triggers Requiring Immediate `README.md` Update
1. **File / Directory Changes:** adding, renaming, or deleting scripts, configuration files, or data folders (e.g., `results/saved/`, `results/exported/`, `mdcg_pdfs/`, `.env` keys, `.gitignore`).
2. **Database & Metadata Schema Changes (`update_mdr.py`):** collection name, `EMBED_MODEL`, chunk sizes/overlaps, `PARSER_VERSION`, or chunk metadata keys (`doc_type`, `source_name`, `chapter`, `section`, `title`).
3. **LLM / RAG Behavior Changes (`app.py`):** AI connection methods (`PROVIDERS`), model lists (`GEMINI_MODELS`), prompt rules, retrieval logic (`retrieve()`, identifier pinning), or filter options.
4. **Save / Export / UI Workflow Changes (`app.py`):** how review records are auto-saved or exported, their naming, or UI layout and wording.
5. **Dependencies & Execution Commands:** packages in `requirements.txt` or launcher/CLI commands.

### 2.2. Documentation Update Standards
- **Keep It Accurate & Concise:** update only the affected sections while preserving the existing structure.
- **Never write machine-specific absolute paths** (e.g., `C:\Users\...`, `D:\...`) into `README.md` or this file — this repository is public; use paths relative to the repository root.
- **Confirm in Final Response:** explicitly tell the user that `README.md` has been synchronized with the code changes.

---

## 3. Non-Negotiable Architectural Constraints
Do not violate these design decisions unless explicitly instructed by the user (details and rationale are in `README.md` Section 2):
1. **No Low-Tier Model Fallbacks (R6):** never use or fall back to `*-lite`, `*-mini`, `*-nano`, or similar lightweight models. Gemini CLI answers produced by a lite model are discarded.
2. **Grounded answers only (R2–R4):** answers must be based solely on retrieved excerpts, cite sources by their on-screen `[n]` numbers, and keep binding EU MDR provisions separate from interpretive MDCG guidance.
3. **Saved vs. exported records (R9–R10):**
   - Every *successful* AI review is **auto-saved** as Markdown to `results/saved/` (always on).
   - **Exports** (Markdown + Word side by side) are written to `results/exported/` only when the user clicks the export button.
   - Filenames are `YYYYMMDD_HHMMSS_<title>` (to the second).
4. **Never degrade the DB or data silently (R18):** if a remote check fails, report "확인 불가" and keep the existing DB; builds go through the staging collection and are validated before the swap.
5. **Terminology:** user-facing labels use **검토** (review), never 분석 (analysis).
6. **Domain Scope:** keep regulatory crawling and guidance tracking focused on **EU MDR (2017/745)** and general/active medical devices (**exclude IVDR 2017/746**).
7. **Secrets:** never commit `.env` or any API key; never print keys in logs or UI.
