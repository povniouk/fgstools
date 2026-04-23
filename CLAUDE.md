# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Context

Greenfield LNG facility EPC project. The owner of this repo is a **control systems engineer** responsible for the **Fire & Gas (F&G) scope**, working as part of the EPC contractor team. The client (Owner) submits deliverables for review; HSED is the internal department that owns Fire & Safety design and provides the governing specifications.

The goal of this project is to build AI-assisted tools that automate verification, cross-checking, and consistency work that is currently done manually across large documents and datasets.

**GitHub repo:** `https://github.com/povniouk/fgstools` (private)
**SSH key for GitHub:** `~/.ssh/github_fgstools` (ed25519, on dev VM 192.168.8.229)

---

## Domain Knowledge

### Standards
- IEC 61511 — Functional safety for process industry
- IEC 60079 — Hazardous area classification
- Project specs always take precedence; every check must be traceable to a spec clause

### Key Interfaces
- **HSED** → provides governing specs and FGS layouts; defines interfaces with PA/GA, HVAC, FACP
  - HSED is split across **HOC** (Houston Operating Center) and **BoOC** (Bogota Operational Center)
- **PA/GA** — Public Address / General Alarm system
- **HVAC** — interfaces triggered by F&G detection events
- **FACP** — Fire Alarm Control Panel (building level), sends confirmed fire signal to FGS controller
- **FGS controller** — master Fire & Gas System controller; receives FACP signals, triggers PA/GA, etc.

### Typical Logic Chain (example)
Confirmed fire in building → FACP sends signal to FGS controller → FGS enables PA/GA activation
Every link in this chain must be traceable to a spec clause.

### SPI Data Limitation (important context)
SPI exports are currently incomplete — data from HSED HOC and HSED BoOC is not yet fully reflected. Do not treat the SPI as a complete source of truth until this is resolved. The email tracker tool is being prioritised first for this reason.

---

## Input Documents

### Spec PDFs (from HSED — source of truth for all verification)
- Fire & Safety Philosophy
- Fire & Safety Specification Buildings
- Fire and Gas System (FGS) Specification
- Format: PDF, typically <100 pages each
- Revised during project execution; revisions tracked via color track changes in the document
- Stored in `FGS_SPEC/` folder in this repo; copied to `~/spec-qa/specs/` on the `fgstools` LXC for serving

### FGS Layouts
- Plot plans showing physical location of F&G detectors
- Provided by HSED

### Cause & Effect (C&E) Matrix
- Working format: **Excel** (exported to PDF for Owner submission)
- Structure: causes (inputs) on rows, actions (outputs) on columns, intersection marked with X or 1
- Actions grouped by system (FGS, PA/GA, HVAC, FACP, etc.)
- Not yet released at project start — to be developed and verified against specs

### P&IDs
- Firewater system P&IDs provided by HSED

### SPI (SmartPlant Instrumentation) Export
- Source: SPI database managed by the SPI team
- Delivery: weekly Excel export sent by email
- Scale: ~2500+ F&G I/O tags in scope
- Key columns: Tag Number, Loop Number, Software Typical, Service Description, Fire Zone, FGS Layout, Detector Type, System1 (FGS), IO Type 1 (Input/Output)

---

## Target Architecture — Integrated Dashboard

All tools will live under a **single Flask app** (port 5000 on `fgstools` LXC) with a tabbed dashboard as the home page. Each tool is a Python module. One shared SQLite database (`cwlng.db`) for all structured data.

**Dashboard tabs (planned):**

| Tab | Status | Notes |
|-----|--------|-------|
| Overview | Planned | KPIs: open actions, pending reviews, last SPI import date |
| Spec Q&A | Complete (Tool 1) | Current standalone app, to be integrated |
| Email Tracker | Next to build | Deliverable tracking from forwarded emails |
| SPI Checker | Planned (Tool 2) | Waiting for complete SPI data |
| C&E Checker | Planned (Tool 3) | Waiting for first C&E draft |

**Operational requirements (non-negotiable):**
- App must run as a **systemd service** — starts on boot, restarts on crash, no terminal needed for day-to-day operation
- Admin tab in the dashboard for: restart, view logs, trigger update
- User never needs SSH or terminal for normal use — browser only

---

## Tools to Build

### Tool 1 — Spec Q&A (`spec-qa`) — COMPLETE v2
**Purpose:** Query spec PDFs in plain language and get answers with exact clause references.

**Example:** *"What is the required voting logic for confirmed gas detection in a compressor building?"*
→ Returns answer + spec name, revision, section reference.

**Location:** `tool1_spec_qa/` in this repo; deployed to `~/spec-qa/` on `fgstools` LXC
**Access:** `http://192.168.8.117:5000`
**Stack:** Flask + pdfplumber + scikit-learn TF-IDF + Ollama API (`http://192.168.8.200:11434`)

**Features implemented (v3):**
- Integrated dashboard with tabs: Overview, Spec Q&A, Email Tracker, SPI Checker (placeholder), C&E Checker (placeholder), Admin
- Systemd service (`fgstools.service`) — auto-start on boot, restart on crash
- Admin tab: Chunk Inspector, Infrastructure info, Re-index email memory button
- **Retrieval stack:** BM25 + TF-IDF + `nomic-embed-text` embeddings via Ollama, merged via Reciprocal Rank Fusion. Cross-encoder re-ranker (`bge-reranker-base`) as second pass — retrieves 3×top_k candidates then re-scores to top_k.
- **PDF parsing:** pdfplumber `find_tables()` + inline extraction; tables rendered as bullet points (`• cell — cell`). Boilerplate stripped per page.
- **Table-aware chunking:** tables are atomic chunks with preceding prose as context prefix; prose word-chunked with overlap between tables
- **Clickable source tags** — spec sources open the PDF at the cited page (`/pdf/<filename>#page=N`); email sources open a modal showing the full email
- **Email history toggle** — off by default; when on, approved email bodies are searched alongside specs and cited as `[Email — Sender, Date]`
- Two-page UI: Q&A (default) and Manage Specs (upload, edit metadata, delete)
- Model selector in header; embedding models filtered out of the list
- Thinking toggle (off by default); Email history toggle
- Real-time log panel (SSE stream), Markdown rendering (marked.js bundled locally)
- VS Code dark theme; Enter to send query, Shift+Enter for newline
- **Streaming responses** — SSE; sources sent after stream completes, matched to what the model actually cited
- **Generation tuning** — temperature `0.2`, `repeat_penalty 1.5`, `repeat_last_n 512`, `top_p 0.9`, `num_predict` 1024
- **Memory-cached specs** — chunks + metadata cached keyed by file mtime
- **Loop guard** — repetition detector aborts stream; `MAX_THINK_CHARS=6000` thinking cap

**Tunables (env vars):** `OLLAMA_URL`, `OLLAMA_MODEL`, `SPECS_DIR`, `TOP_K` (default 6), `TEMPERATURE`, `NUM_PREDICT`, `MAX_THINK_CHARS`

**Known issues:**
- `gemma4:26b` with thinking enabled occasionally degenerates into repetition loops. Root cause: 22% CPU spill (26B doesn't fit in 16GB VRAM). Next to try: `gemma4:26b-nvfp4` (Blackwell FP4, fits in VRAM).
- `gemma4:latest` (12B) stable for production use.

**RAG pipeline — status:**

| Challenge | Status | Notes |
|-----------|--------|-------|
| PDF table extraction | ✅ Done | pdfplumber + bullet-point format |
| Table atomicity | ✅ Done | Atomic chunks with prose context prefix |
| Semantic embeddings | ✅ Done | `nomic-embed-text` via Ollama, cached to `.npy` |
| Cross-encoder re-ranker | ✅ Done | `bge-reranker-base` via `sentence-transformers` on fgstools LXC (CPU); install in progress |
| Email memory (RAG) | ✅ Done | Approved emails chunked + embedded in `cwlng.db`; searchable via Email history toggle |
| Word-count chunking | ⚠️ Partial | Prose chunks word-count based; semantic chunking deferred |
| Vocabulary mismatch | ⚠️ Partial | Synonym dict + embeddings. Works for known terms. |

**Re-ranker install status:**
- `bge-reranker-base` runs locally on fgstools LXC via `sentence-transformers`
- `torch` (CPU-only) install in progress on LXC — was killed by OOM at 2GB RAM; RAM bumped to 2GB, retry in progress
- Once installed: no restart needed — retriever loads model lazily on first query, falls back gracefully if unavailable
- Retriever already wired: fetches 3×top_k candidates, re-ranks to top_k

---

### Tool 5 — Email Tracker — COMPLETE (M1–M7)
**Purpose:** Track deliverables, action items, and blocking points from project emails. Secondary purpose: build a searchable project memory from email history to complement the spec Q&A tool.

**Ingestion method (decided):** User drags email from Outlook to Desktop → saves as `.eml` → drags `.eml` to the browser drop zone. Also accepts `.msg`. No Gmail/IMAP needed — no external accounts, fully local.

**Stack:** `email_tracker.py` Flask Blueprint, SQLite (`cwlng.db`), Ollama extraction via streaming API.

**Disciplines:** HSED, ICSS, Electrical, HVAC, Telecom, Instrumentation, Other

**Scope tags (replaces document_ref):** SPI, C&E, FGS Layouts, Document Review, Interface, General, Other

**Categories:** Comment response, IFR submittal, Technical query, Information request, Meeting action *(blocking point is a separate checkbox, not a category)*

**Status values:** Open / In Progress / Closed

---

#### Tool 5 — Milestone Plan

**M1 — Basic import + action register** ✅ DONE
- `.eml` / `.msg` parsing (Python `email` lib + `extract-msg`)
- Ollama extraction via streaming with `think=False`
- Draft cards: approve / discard per item or all at once
- SQLite tables: `emails`, `action_items`
- Action register: filterable by status/discipline, inline status change, delete

**M2 — Card + register field cleanup** ✅ DONE
- Simplified draft card to 5 fields: Action, Discipline, Scope, Priority, Deadline (blocking point as header checkbox only)
- New discipline list: HSED, ICSS, Electrical, HVAC, Telecom, Instrumentation, Other
- Replaced document_ref with Scope dropdown: SPI, C&E, FGS Layouts, Document Review, Interface, General, Other
- Removed "Blocking point" from category list
- Updated Ollama extraction prompt to match new fields

**M3 — Register table layout fix** ✅ DONE
- Slimmed to 7 columns: Priority | Discipline | Action | Scope | Deadline | Status | ×
- Added overflow-x:auto wrapper to prevent button overflow

**M3.5 — Contacts directory** ✅ DONE
- New sub-tab in Email Tracker: Import | Action Register | **Contacts**
- New DB table: `contacts (id, name, email, position, operating_center, discipline, notes, source, created_at, updated_at)`
- **Operating centers:** POC (Paris), HOC (Houston), BoOC (Bogota), Owner, Vendor, Other
- **Auto-extraction on email import:** Ollama extracts sender contact details (name, email, position, operating center) from the email signature in the same extraction call. Matched by email address — updates existing record rather than duplicating.
- **Manual add/edit:** Form to add or edit contacts not coming from email
- **Contact table view:** Name | Position | Operating Center | Discipline | Email
- Click row → inline edit or simple modal
- Future link to M4 side panel: "open actions from this person" count shown in contact row

**M4 — Side detail / edit panel** ✅ DONE
- Click a register row → right-side panel slides in
- Panel shows all fields, fully editable
- Save button writes changes back to DB
- Close/dismiss returns to register
- Delete button in panel footer

**M5 — Append-only notes log** ✅ DONE
- Notes section inside detail panel
- Each entry is timestamped and appended — history never overwritten
- Supports plain text; renders as a log (newest at top)
- New DB column: `notes` (JSON array of `{ts, text}`) or separate `item_notes` table

**M6 — File attachments per action item** ✅ DONE
- Drag & drop or file picker in the detail panel: PNG/JPG screenshots, PDF, `.eml`, Word docs
- Files stored on LXC filesystem: `~/spec-qa/attachments/<item_id>/`
- New DB table: `attachments (id, item_id, filename, original_name, uploaded_at)`
- Listed in panel with filename + upload date; click to download
- Close action button: sets status to Closed, records timestamp in notes log

**M7 — Email as project memory (RAG integration)** ✅ DONE
- Cleaned email bodies chunked and embedded alongside spec chunks in the retrieval pool
- Each email chunk tagged with sender, date, discipline (cites as `[Email — Sender, Date]`)
- Q&A tool searches specs + email history simultaneously
- Decisions, interface clarifications, and responsibility assignments become findable by natural language query
- Trigger: any approved email is automatically indexed; re-index on demand from Admin tab

---

### Tool 2 — SPI Consistency Checker — PLANNED
**Purpose:** Ingest weekly SPI Excel export, run rule-based checks derived from specs, output anomaly report. Store each weekly import in SQLite with timestamp; diff against prior week to surface new issues, resolved issues, and changed fields.

**Note:** Deprioritised until SPI data from HSED HOC and BoOC is complete.

**Example checks:**
- Detector type matches spec requirement for the declared Fire Zone
- All FGS input tags have Software Typical populated
- Tags in System1=FGS have a Fire Zone assigned
- No duplicate tag numbers

**Input:** SPI Excel export (~2500 rows)
**Output:** Delta report (new/changed/resolved) + full anomaly list with tag reference and rule violated

---

### Tool 3 — C&E vs Spec Checker — PLANNED
**Purpose:** Read C&E Excel matrix and verify cause-action pairs are consistent with spec requirements.

**Example checks:**
- Confirmed fire in building X → FACP signal to FGS → PA/GA activation: is the X present in the matrix?
- No actions present in C&E that are not supported by spec

**Input:** C&E Excel (available once first draft is released)

---

### Tool 4 — Revision Delta Tracker — PLANNED
**Purpose:** Compare two revisions of a spec PDF, output list of changed clauses, flag which Tool 2 rules or Tool 3 checks are potentially invalidated. Triggered automatically when a new spec is uploaded.

**Input:** Two PDF revisions of the same spec document

---

### Additional tools considered (brainstorm, not yet scoped)
- **Document submittal register** — track IFR / IFC / IFA status per document, highlight overdue items
- **Meeting minutes action extractor** — paste or forward meeting minutes, AI extracts action items with owners and deadlines
- **Overview / KPI dashboard** — open actions count, pending reviews, last SPI import, spec revision alerts

---

## Infrastructure

### Developer Machine
- VM at `192.168.8.229` — dev environment, VS Code, Claude Code
- Company laptop has no admin rights — accesses all tools via browser only
- Git repo at `/home/povniouk/my-projects/CWLNG/`

### Proxmox Server
- Hardware: AMD Ryzen 9 5950X, 64GB RAM, RTX 5060 Ti 16GB VRAM
- IOMMU enabled

### AI VM — `gemma4` (VM 102) — `192.168.8.200`
- OS: Ubuntu 22.04 Server (headless, SSH only)
- GPU: RTX 5060 Ti passed through via VFIO (IDs: `10de:2d04`, `10de:22eb`)
- QEMU args: `-cpu host,kvm=off`
- AI runtime: **Ollama**, default model **`gemma4:latest`** (12B, fits 100% in VRAM); `gemma4:26b` available but spills to CPU (22% CPU / 78% GPU)
- Ollama listens on `0.0.0.0:11434`, env: `OLLAMA_KEEP_ALIVE=24h` (model stays warm in VRAM)
- **Reranker service (pending install):** `BAAI/bge-reranker-base` via sentence-transformers, port `11435`. Service file at `reranker/reranker.service` in repo. Install: create `~/reranker/`, venv, `pip install flask sentence-transformers`, copy `reranker/app.py`, enable systemd service.
- Models pulled: `gemma4:latest`, `gemma4:26b`, `nomic-embed-text:latest`

**Quantization notes:**
- For Blackwell GPUs (RTX 50 series), `*-nvfp4` variants leverage native FP4 tensor cores — smaller, faster, higher quality than INT4 quants. Worth trying for any model that doesn't fit at default quant.
- NVFP4 builds typically drop the vision adapter — text-only. Fine for spec Q&A (PDFs go through pdfplumber text extraction, no OCR).

### App LXC — `fgstools` — `192.168.8.117`
- OS: Debian 13 (headless, SSH only)
- Hosts all web application tools
- Calls Ollama on `gemma4` at `http://192.168.8.200:11434`
- App folder: `~/spec-qa/`
- Spec PDFs stored in `~/spec-qa/specs/`
- SQLite database: `~/spec-qa/cwlng.db` (emails, action_items, contacts, attachments, email_chunks)
- File attachments stored in `~/spec-qa/attachments/<item_id>/`
- RAM: 2048MB (bumped from 1024MB to support sentence-transformers install)
- **App must run as systemd service** — not manually from terminal

### Why local AI instead of cloud API
Spec PDFs and project data are confidential EPC deliverables. Sending them to any external API is a security and contractual risk. All AI inference runs locally on the Proxmox server.

### GPU not used by Ollama — recovery
Symptom: `ollama ps` shows `100% CPU`, queries are very slow (>2 min), `nvtop`/`nvidia-smi` show no GPU activity.
Cause seen on this setup: Ollama loaded a model into CPU before its CUDA detection initialised properly (Blackwell GB206 + CUDA 13).
Fix: `sudo systemctl restart ollama`, then send a fresh query. Verify with `ollama ps` — should show `100% GPU`. Inspect startup logs with `journalctl -u ollama --since "1 minute ago"` — look for `inference compute ... library=CUDA ... description="NVIDIA GeForce RTX 5060 Ti"`. If absent, the GPU isn't being discovered.

---

## Stack

- **Backend:** Python + Flask
- **AI runtime:** Ollama on `gemma4` VM — model: `gemma4:latest`
- **Frontend:** Simple HTML/JS, no framework, no external CDNs (company browser blocks them — always bundle locally)
- **Document parsing:** pdfplumber
- **Data storage:** SQLite (`cwlng.db`) — shared across all tools
- **Email polling:** Python `imaplib` against a dedicated Gmail account
- **Data handling:** pandas (for Excel tools)
- **Deployment:** LXC `fgstools`, accessed via browser from laptop; systemd service for auto-start
- **Source control:** GitHub `https://github.com/povniouk/fgstools` (private)

---

## Setup Status

- [x] Proxmox IOMMU confirmed enabled
- [x] GPU bound to vfio-pci on Proxmox host (`Kernel driver in use: vfio-pci`)
- [x] `gemma4` VM (102) created, Ubuntu 22.04 Server
- [x] NVIDIA drivers installed (driver 580.126.09, CUDA 13.0)
- [x] Ollama installed, `gemma4:latest` pulled and tested
- [x] `fgstools` LXC created, Debian 13, user `povniouk` with sudo
- [x] Python venv created in `~/spec-qa/`, dependencies installed
- [x] App files deployed to `fgstools` and in sync with dev VM
- [x] Tool 1 (Spec Q&A) complete and running at `http://192.168.8.117:5000`
- [x] GitHub repo initialised (`fgstools`), SSH key set up, initial commit pushed
- [x] Convert app to systemd service on `fgstools` LXC (`/etc/systemd/system/fgstools.service`, enabled, starts on boot)
- [x] Dashboard rebuild — multi-tab architecture (Overview, Spec Q&A, Email Tracker, SPI Checker, C&E Checker, Admin)
- [ ] Reranker (`bge-reranker-base`) on fgstools LXC — `sentence-transformers` + CPU torch install in progress; retriever already wired, falls back gracefully if not yet installed
- [x] Tool 5 M1–M7 — Email Tracker complete: import, action register, contacts, side panel, notes log, attachments, email memory RAG
- [ ] Tool 2 (SPI Consistency Checker) — waiting for complete SPI data from HSED HOC/BoOC
- [ ] Tool 3 (C&E vs Spec Checker) — waiting for first C&E draft
- [ ] Tool 4 (Revision Delta Tracker) — not started
