import os
import re
import json
import queue
import datetime
import requests
from collections import deque
from flask import Flask, request, jsonify, send_from_directory, Response
from pdf_loader import load_pdf_chunks
from retriever import find_relevant_chunks
import retriever as _retriever

app = Flask(__name__, static_folder="static")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:latest")
SPECS_DIR = os.environ.get("SPECS_DIR", "specs")
TOP_K = int(os.environ.get("TOP_K", "4"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.2"))
NUM_PREDICT = int(os.environ.get("NUM_PREDICT", "1024"))
MAX_THINK_CHARS = int(os.environ.get("MAX_THINK_CHARS", "6000"))

current_model = {"name": MODEL, "think": False}

# Log buffer — keeps last 200 entries, streams to connected clients
_log_buffer = deque(maxlen=200)
_log_subscribers = []


def log(level, msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "level": level, "msg": msg}
    _log_buffer.append(entry)
    line = f"data: {json.dumps(entry)}\n\n"
    for q in list(_log_subscribers):
        try:
            q.put_nowait(line)
        except Exception:
            pass
    print(f"[{ts}] {level.upper()}: {msg}")


def log_info(msg):  log("info", msg)
def log_warn(msg):  log("warn", msg)
def log_error(msg): log("error", msg)


# --- Metadata helpers ---

def parse_filename_metadata(filename):
    name = filename.removesuffix(".pdf")
    doc_number, revision, title = "", "", name
    match = re.match(r"^([A-Z0-9\-]+)_(\w+)\s+(.+)$", name)
    if match:
        doc_number = match.group(1)
        revision = match.group(2)
        title = match.group(3).title()
    return {"doc_number": doc_number, "title": title, "revision": revision, "rev_date": ""}


def save_metadata(filename, meta):
    meta_path = os.path.join(SPECS_DIR, filename + ".meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f)


def load_metadata(filename):
    meta_path = os.path.join(SPECS_DIR, filename + ".meta.json")
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            return json.load(f)
    meta = parse_filename_metadata(filename)
    save_metadata(filename, meta)
    return meta


# --- In-memory cache for specs ---
# Each entry: {chunks, meta, version: (chunks_mtime, meta_mtime)}
_specs_cache = {}
_combined_chunks = []
_cache_key = ()


def refresh_cache():
    """Reload only specs whose chunks/meta files changed on disk."""
    global _combined_chunks, _cache_key

    if not os.path.exists(SPECS_DIR):
        return

    files = sorted(f for f in os.listdir(SPECS_DIR) if f.endswith(".pdf"))
    new_key_entries = []
    changed = False

    for fname in files:
        chunks_path = os.path.join(SPECS_DIR, fname + ".chunks.json")
        meta_path = os.path.join(SPECS_DIR, fname + ".meta.json")
        if not os.path.exists(chunks_path):
            continue
        chunks_mtime = os.path.getmtime(chunks_path)
        meta_mtime = os.path.getmtime(meta_path) if os.path.exists(meta_path) else 0.0
        version = (chunks_mtime, meta_mtime)
        new_key_entries.append((fname, chunks_mtime, meta_mtime))

        existing = _specs_cache.get(fname)
        if not existing or existing["version"] != version:
            with open(chunks_path) as f:
                chunks = json.load(f)
            meta = load_metadata(fname)
            _specs_cache[fname] = {"chunks": chunks, "meta": meta, "version": version}
            changed = True

    # Drop deleted specs
    present = {f for f in files if os.path.exists(os.path.join(SPECS_DIR, f + ".chunks.json"))}
    for fname in list(_specs_cache.keys()):
        if fname not in present:
            del _specs_cache[fname]
            changed = True

    new_key = tuple(new_key_entries)
    if changed or _cache_key != new_key:
        _cache_key = new_key
        combined = []
        for fname, data in _specs_cache.items():
            meta = data["meta"]
            doc_number = meta.get("doc_number") or fname
            title = meta.get("title") or fname
            revision = meta.get("revision", "")
            for chunk in data["chunks"]:
                combined.append({
                    "text": chunk["text"],
                    "section": chunk.get("section", ""),
                    "page": chunk.get("page", 0),
                    "has_table": chunk.get("has_table", False),
                    "source": fname,
                    "doc_number": doc_number,
                    "title": title,
                    "revision": revision,
                })
        _combined_chunks = combined


# --- Routes ---

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/logs/stream")
def logs_stream():
    def generate():
        for entry in list(_log_buffer):
            yield f"data: {json.dumps(entry)}\n\n"
        q = queue.Queue()
        _log_subscribers.append(q)
        try:
            while True:
                try:
                    yield q.get(timeout=30)
                except queue.Empty:
                    yield "data: {\"ts\":\"\",\"level\":\"ping\",\"msg\":\"\"}\n\n"
        finally:
            try:
                _log_subscribers.remove(q)
            except ValueError:
                pass

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/models", methods=["GET"])
def list_models():
    try:
        res = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        res.raise_for_status()
        models = [m["name"] for m in res.json().get("models", [])]
        log_info(f"Available models: {', '.join(models)}")
    except Exception as e:
        log_warn(f"Could not fetch model list from Ollama: {e}")
        models = [current_model["name"]]
    return jsonify({"models": models, "current": current_model["name"], "think": current_model["think"]})


@app.route("/api/models/select", methods=["POST"])
def select_model():
    data = request.json
    name = data.get("model", "").strip()
    if not name:
        return jsonify({"error": "model name required"}), 400
    current_model["name"] = name
    log_info(f"Model switched to: {name}")
    return jsonify({"current": current_model["name"], "think": current_model["think"]})


@app.route("/api/models/think", methods=["POST"])
def set_think():
    data = request.json
    current_model["think"] = bool(data.get("think", False))
    state = "enabled" if current_model["think"] else "disabled"
    log_info(f"Model thinking {state}")
    return jsonify({"think": current_model["think"]})


@app.route("/api/specs", methods=["GET"])
def list_specs():
    refresh_cache()
    result = [{"filename": fname, **data["meta"]} for fname, data in _specs_cache.items()]
    return jsonify({"specs": result})


@app.route("/api/specs/<path:filename>/metadata", methods=["PUT"])
def update_metadata(filename):
    data = request.json
    meta = load_metadata(filename)
    for field in ("doc_number", "title", "revision", "rev_date"):
        if field in data:
            meta[field] = data[field]
    save_metadata(filename, meta)
    refresh_cache()
    log_info(f"Metadata updated for {filename}")
    return jsonify({"ok": True, "meta": meta})


@app.route("/api/upload", methods=["POST"])
def upload_spec():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename.endswith(".pdf"):
        return jsonify({"error": "Only PDF files are accepted"}), 400
    save_path = os.path.join(SPECS_DIR, f.filename)
    f.save(save_path)
    log_info(f"Uploaded: {f.filename} — parsing PDF...")
    chunks = load_pdf_chunks(save_path)
    cache_path = save_path + ".chunks.json"
    with open(cache_path, "w") as fp:
        json.dump(chunks, fp)
    log_info(f"Parsed {f.filename}: {len(chunks)} chunks indexed")
    meta = load_metadata(f.filename)
    refresh_cache()
    return jsonify({"loaded": f.filename, "chunks": len(chunks), "meta": meta})


@app.route("/api/query", methods=["POST"])
def query():
    data = request.json
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "question required"}), 400

    log_info(f"Query received: {question[:80]}{'...' if len(question) > 80 else ''}")

    refresh_cache()
    if not _combined_chunks:
        return jsonify({"error": "No specs loaded. Upload a PDF first."}), 400

    # Thinking mode benefits from more context to reason over
    top_k = TOP_K * 2 if current_model["think"] else TOP_K
    relevant = find_relevant_chunks(question, _combined_chunks, _cache_key, top_k=top_k)
    sections = ", ".join(f"{c['section']}{'[T]' if c.get('has_table') else ''}" for c in relevant)
    log_info(f"Retrieved {len(relevant)} chunks: {sections}")

    context = "\n\n".join(
        f"[Doc: {c['doc_number']} Rev.{c['revision']} | Section: {c['section']}]\n{c['text']}"
        for c in relevant
    )

    prompt = f"""You are a Fire and Gas (F&G) engineering assistant. Answer based strictly on the excerpts below.

Rules:
- The FIRST excerpt is the most relevant — read it carefully before the others.
- Bullet points (•) in the excerpts are table rows extracted from the specification. Read every bullet.
- When a sentence ends with "the following set points:" or "the following levels:", the bullets immediately after are the answer.
- Quote the section reference (document number, revision, section).
- If the answer is not in the excerpts, say "Not found in the provided specifications." Do not guess.
- Be direct. List set points or thresholds as bullet points.

SPECIFICATION EXCERPTS (most relevant first):
{context}

QUESTION: {question}

ANSWER:"""

    sources = [
        {
            "filename": c["source"],
            "doc_number": c["doc_number"],
            "title": c["title"],
            "revision": c["revision"],
            "section": c["section"],
        }
        for c in relevant
    ]

    # Thinking mode needs a bigger budget — thinking tokens count against num_predict
    num_predict = NUM_PREDICT * 4 if current_model["think"] else NUM_PREDICT
    log_info(f"Sending prompt to model: {current_model['name']} (think={current_model['think']}, temp={TEMPERATURE}, max={num_predict})")

    def stream():
        # Send sources first so the UI can display them immediately
        yield f"event: sources\ndata: {json.dumps(sources)}\n\n"

        t_start = datetime.datetime.now()
        total_chars = 0
        think_chars = 0
        recent_tokens = deque(maxlen=200)
        try:
            with requests.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": current_model["name"],
                    "prompt": prompt,
                    "stream": True,
                    "think": current_model["think"],
                    "options": {
                        "temperature": TEMPERATURE,
                        "num_predict": num_predict,
                        "repeat_penalty": 1.5,
                        "repeat_last_n": 512,
                        "top_p": 0.9,
                        "top_k": 40,
                    },
                },
                stream=True,
                timeout=300,
            ) as r:
                r.raise_for_status()
                for raw in r.iter_lines(decode_unicode=True):
                    if not raw:
                        continue
                    obj = json.loads(raw)
                    thinking = obj.get("thinking", "")
                    if thinking:
                        think_chars += len(thinking)
                        yield f"event: thinking\ndata: {json.dumps({'t': thinking})}\n\n"
                        if think_chars > MAX_THINK_CHARS:
                            log_warn(f"Thinking exceeded {MAX_THINK_CHARS} chars — aborting (likely loop)")
                            r.close()
                            yield f"event: error\ndata: {json.dumps({'error': f'Model stuck in thinking loop (>{MAX_THINK_CHARS} chars). Try disabling thinking or rephrasing.'})}\n\n"
                            return
                    token = obj.get("response", "")
                    if token:
                        total_chars += len(token)
                        yield f"event: token\ndata: {json.dumps({'t': token})}\n\n"
                        recent_tokens.append(token)
                        # Loop guard: last 20-char snippet repeats 5+ times in prior 500 chars
                        if total_chars > 600:
                            buf = "".join(recent_tokens)
                            snippet = buf[-20:]
                            window = buf[-500:-20]
                            if len(snippet.strip()) >= 5 and window.count(snippet) >= 4:
                                log_warn(f"Detected repetition loop on snippet: {snippet!r} — aborting")
                                r.close()
                                yield f"event: error\ndata: {json.dumps({'error': 'Model entered repetition loop. Try rephrasing or disabling thinking.'})}\n\n"
                                return
                    if obj.get("done"):
                        elapsed = (datetime.datetime.now() - t_start).total_seconds()
                        log_info(f"Model responded in {elapsed:.1f}s ({total_chars} chars, {think_chars} think chars)")
                        yield f"event: done\ndata: {json.dumps({'elapsed': elapsed})}\n\n"
                        return
        except requests.exceptions.ReadTimeout:
            log_error("Ollama request timed out (>300s)")
            yield f"event: error\ndata: {json.dumps({'error': 'Model timed out. Try again.'})}\n\n"
        except Exception as e:
            log_error(f"Ollama request failed: {e}")
            yield f"event: error\ndata: {json.dumps({'error': 'Failed to reach AI model.'})}\n\n"

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/debug/chunks")
def debug_chunks():
    """Return all chunks as plain text for review — Admin use only."""
    refresh_cache()
    filename = request.args.get("file", "")
    out = []
    for fname, data in _specs_cache.items():
        if filename and fname != filename:
            continue
        out.append(f"{'='*70}\nFILE: {fname}\n{'='*70}\n")
        for i, chunk in enumerate(data["chunks"]):
            out.append(
                f"--- Chunk {i+1} | Page {chunk.get('page','?')} | "
                f"Section: {chunk.get('section','?')} ---\n"
                f"{chunk['text']}\n"
            )
    return Response("\n".join(out), mimetype="text/plain")


def preload_specs():
    if not os.path.exists(SPECS_DIR):
        return
    for fname in sorted(os.listdir(SPECS_DIR)):
        if not fname.endswith(".pdf"):
            continue
        path = os.path.join(SPECS_DIR, fname)
        cache_path = path + ".chunks.json"
        if not os.path.exists(cache_path):
            log_info(f"Pre-loading {fname}...")
            chunks = load_pdf_chunks(path)
            with open(cache_path, "w") as f:
                json.dump(chunks, f)
            log_info(f"Pre-loaded {fname}: {len(chunks)} chunks")
        load_metadata(fname)
    refresh_cache()
    log_info(f"Cache built: {len(_specs_cache)} spec(s), {len(_combined_chunks)} chunks total")


if __name__ == "__main__":
    os.makedirs(SPECS_DIR, exist_ok=True)
    _retriever._log = log_info  # route embedding progress to browser log panel
    log_info(f"Starting F&G Spec Q&A — Ollama: {OLLAMA_URL} — Model: {MODEL}")
    preload_specs()
    log_info("App ready.")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
