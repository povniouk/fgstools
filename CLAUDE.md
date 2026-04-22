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

### Tool 1 — Spec Q&A (`spec-qa`) — COMPLETE v1
**Purpose:** Query spec PDFs in plain language and get answers with exact clause references.

**Example:** *"What is the required voting logic for confirmed gas detection in a compressor building?"*
→ Returns answer + spec name, revision, section reference.

**Location:** `tool1_spec_qa/` in this repo; deployed to `~/spec-qa/` on `fgstools` LXC
**Access:** `http://192.168.8.117:5000`
**Stack:** Flask + pdfplumber + scikit-learn TF-IDF + Ollama API (`http://192.168.8.200:11434`)

**Features implemented:**
- Two-page UI: Q&A (default) and Manage Specs
- Spec library table with editable doc number, title, revision, revision date (persisted per-spec)
- PDF upload via drag & drop on Manage Specs page
- PDFs auto-loaded from `~/spec-qa/specs/` on startup
- Model selector in header (queries Ollama for available models)
- Thinking toggle in header (off by default for speed; status bar shows `Thinking... N chars, Xs` when on)
- TF-IDF index (scikit-learn) for fast relevant chunk retrieval
- Real-time log panel (bottom bar, collapsible, SSE stream) with response time
- Markdown rendering in answer box (marked.js bundled locally — no CDN)
- VS Code dark theme
- **Streaming responses** — backend uses Ollama stream mode + SSE; frontend renders tokens as they arrive (first-token latency shown to user)
- **Generation tuning** — temperature `0.2`, `repeat_penalty 1.5`, `repeat_last_n 512`, `top_p 0.9`, `num_predict` 1024 (×4 when thinking on)
- **Memory-cached specs** — chunks + metadata cached in process memory keyed by file mtime; disk re-read only when a spec file changes
- **TF-IDF index** rebuilt only when the cache key (set of file mtimes) changes — not per query
- **Cross-page chunking** — PDF chunks span page boundaries (700 words / 150 overlap) to avoid mid-sentence cuts
- **Thinking mode** — `TOP_K` doubled when thinking is on; thinking status shown in status bar (not streamed to answer box)
- **Loop guard** — server-side repetition detector aborts stream if a 20-char snippet repeats 4+ times in prior 500 chars; `MAX_THINK_CHARS=6000` cap on thinking tokens

**Tunables (env vars):** `OLLAMA_URL`, `OLLAMA_MODEL`, `SPECS_DIR`, `TOP_K` (default 8), `TEMPERATURE`, `NUM_PREDICT`, `MAX_THINK_CHARS`

**Known issues to revisit:**
- `gemma4:26b` with thinking enabled occasionally degenerates into repetition loops (e.g. `000-000-000-000...`). All mitigations above are in place but do not fully resolve it. Root cause: 22% CPU spill (26B doesn't fit in 16GB VRAM) causes sampler instability that thinking mode amplifies.
- **Next thing to try:** pull `gemma4:26b-nvfp4` (Blackwell-native FP4 — should fit fully in VRAM, no CPU spill). Loses vision/OCR — acceptable since pdfplumber extracts text.
- `gemma4:latest` (12B) is stable for production use.

---

### Tool 5 — Email Tracker — NEXT TO BUILD
**Purpose:** Track deliverables, action items, and blocking points received by email from other engineering teams (HSED HOC, HSED BoOC, Civil, Piping, Vendors, etc.).

**Design decisions:**
- User **forwards** project emails to a dedicated Gmail account (e.g. `cwlng-fgs@gmail.com`)
- App polls Gmail via **IMAP** (Python `imaplib`) on a configurable interval
- Ollama extracts structured items per email: discipline/sender, document references, action required, blocking point, deadline, category, suggested priority
- User sees a **draft preview** (one card per extracted item) and **approves, edits, or discards** each — AI suggests, human confirms
- Approved items land in `cwlng.db` (SQLite) as action register entries
- Action register is filterable by discipline, category, status (Open / In Progress / Closed)
- Status flipped manually by user as items resolve

**Why Gmail forwarding instead of direct company mailbox:** Company mailbox has no API access and admin restrictions. Gmail IMAP with an app password requires no OAuth, no admin rights, just a forward rule from the company inbox.

**Why AI-suggested priority with human approval:** Avoids getting lost in history; user stays in control of what matters.

**Categories:** Comment response, IFR submittal, Technical query, Information request, Meeting action, Blocking point

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
- **Dedicated to AI inference only — no app code runs here**

**Quantization notes:**
- For Blackwell GPUs (RTX 50 series), `*-nvfp4` variants leverage native FP4 tensor cores — smaller, faster, higher quality than INT4 quants. Worth trying for any model that doesn't fit at default quant.
- NVFP4 builds typically drop the vision adapter — text-only. Fine for spec Q&A (PDFs go through pdfplumber text extraction, no OCR).

### App LXC — `fgstools` — `192.168.8.117`
- OS: Debian 13 (headless, SSH only)
- Hosts all web application tools
- Calls Ollama on `gemma4` at `http://192.168.8.200:11434`
- App folder: `~/spec-qa/` (will be reorganised to `~/fgstools/` when dashboard rebuild begins)
- Spec PDFs stored in `~/spec-qa/specs/`
- SQLite database will live here: `~/fgstools/cwlng.db`
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
- [ ] Convert app to systemd service on `fgstools` LXC
- [ ] Dashboard rebuild — multi-tab architecture
- [ ] Tool 5 (Email Tracker) — next to build
- [ ] Tool 2 (SPI Consistency Checker) — waiting for complete SPI data from HSED HOC/BoOC
- [ ] Tool 3 (C&E vs Spec Checker) — waiting for first C&E draft
- [ ] Tool 4 (Revision Delta Tracker) — not started
