import os
import re
import json
import datetime
import sqlite3
import tempfile
import threading
import requests
import numpy as np
import email as email_lib
from email import policy as email_policy
from email.utils import parseaddr
from flask import Blueprint, request, jsonify, send_from_directory

bp = Blueprint("email_tracker", __name__)

DB_PATH        = os.environ.get("DB_PATH", "cwlng.db")
OLLAMA_URL     = os.environ.get("OLLAMA_URL", "http://localhost:11434")
ATTACHMENTS_DIR = os.environ.get("ATTACHMENTS_DIR", "attachments")
MEETINGS_DIR   = os.environ.get("MEETINGS_DIR", "meetings")

# Shared with app.py current_model dict after registration — set by app.py
_current_model = {"name": os.environ.get("OLLAMA_MODEL", "gemma4:latest")}

# Log callback — app.py sets this to log_info so output reaches the browser log panel
_log = print

DISCIPLINES = ["HSED", "ICSS", "Electrical", "HVAC", "Telecom", "Instrumentation", "Other"]
SCOPES     = ["SPI", "C&E", "FGS Layouts", "Document Review", "Interface", "General", "Other"]
CATEGORIES = ["Comment response", "IFR submittal", "Technical query",
              "Information request", "Meeting action"]
PRIORITIES = ["Low", "Medium", "High", "Critical"]
STATUSES   = ["Open", "In Progress", "Closed"]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _run_migration(db, key, fn):
    db.execute("CREATE TABLE IF NOT EXISTS _migrations (key TEXT PRIMARY KEY)")
    db.commit()
    if not db.execute("SELECT 1 FROM _migrations WHERE key=?", (key,)).fetchone():
        fn(db)
        db.execute("INSERT OR IGNORE INTO _migrations (key) VALUES (?)", (key,))
        db.commit()


def _mig_email_unique_sender(db):
    """Deduplicate emails by (sender, subject, sent_date), then add UNIQUE constraint."""
    db.execute("PRAGMA foreign_keys = OFF")
    # Find duplicates — keep lowest id per combination
    dupes = db.execute("""
        SELECT id FROM emails WHERE id NOT IN (
            SELECT MIN(id) FROM emails
            GROUP BY COALESCE(sender,''), COALESCE(subject,''), COALESCE(sent_date,'')
        )
    """).fetchall()
    if dupes:
        ids = [r[0] for r in dupes]
        ph = ','.join('?' * len(ids))
        # Reassign action_items and chunks to the surviving email_id
        for dup_id in ids:
            row = db.execute(
                "SELECT sender, subject, sent_date FROM emails WHERE id=?", (dup_id,)
            ).fetchone()
            if row:
                keep = db.execute(
                    "SELECT id FROM emails WHERE sender=? AND subject=? AND sent_date=? "
                    "AND id != ? ORDER BY id LIMIT 1",
                    (row['sender'], row['subject'], row['sent_date'], dup_id)
                ).fetchone()
                if keep:
                    db.execute("UPDATE action_items SET email_id=? WHERE email_id=?",
                               (keep['id'], dup_id))
                    db.execute("DELETE FROM email_chunks WHERE email_id=?", (dup_id,))
        db.execute(f"DELETE FROM emails WHERE id IN ({ph})", ids)
        _log(f"[Email migration] Removed {len(ids)} duplicate email(s)")
    db.execute("""
        CREATE TABLE emails_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filename    TEXT,
            sender      TEXT,
            subject     TEXT,
            sent_date   TEXT,
            body_text   TEXT,
            imported_at TEXT,
            UNIQUE(sender, subject, sent_date)
        )
    """)
    db.execute("INSERT INTO emails_new SELECT * FROM emails")
    db.execute("DROP TABLE emails")
    db.execute("ALTER TABLE emails_new RENAME TO emails")
    db.execute("PRAGMA foreign_keys = ON")


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS emails (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filename    TEXT,
            sender      TEXT,
            subject     TEXT,
            sent_date   TEXT,
            body_text   TEXT,
            imported_at TEXT
        );
        CREATE TABLE IF NOT EXISTS action_items (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id       INTEGER,
            discipline     TEXT    DEFAULT '',
            scope          TEXT    DEFAULT '',
            document_ref   TEXT    DEFAULT '',
            action         TEXT    DEFAULT '',
            blocking_point INTEGER DEFAULT 0,
            deadline       TEXT    DEFAULT '',
            category       TEXT    DEFAULT '',
            priority       TEXT    DEFAULT 'Medium',
            status         TEXT    DEFAULT 'Open',
            notes          TEXT    DEFAULT '',
            created_at     TEXT,
            FOREIGN KEY (email_id) REFERENCES emails(id)
        );
    """)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS email_chunks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id   INTEGER,
            chunk_idx  INTEGER,
            text       TEXT,
            sender     TEXT,
            sent_date  TEXT,
            discipline TEXT,
            embedding  TEXT,
            FOREIGN KEY (email_id) REFERENCES emails(id)
        );
        CREATE TABLE IF NOT EXISTS attachments (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id       INTEGER,
            filename      TEXT,
            original_name TEXT,
            uploaded_at   TEXT,
            FOREIGN KEY (item_id) REFERENCES action_items(id)
        );
        CREATE TABLE IF NOT EXISTS contacts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT DEFAULT '',
            email            TEXT DEFAULT '',
            position         TEXT DEFAULT '',
            operating_center TEXT DEFAULT '',
            discipline       TEXT DEFAULT '',
            notes            TEXT DEFAULT '',
            source           TEXT DEFAULT 'manual',
            created_at       TEXT,
            updated_at       TEXT
        );
    """)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meetings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            title         TEXT    DEFAULT '',
            recorded_date TEXT    DEFAULT '',
            transcript    TEXT    DEFAULT '',
            created_at    TEXT
        );
    """)
    os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
    os.makedirs(MEETINGS_DIR, exist_ok=True)
    # Column migrations (idempotent — silently skipped if column already exists)
    for col in [
        "ALTER TABLE action_items ADD COLUMN scope TEXT DEFAULT ''",
        "ALTER TABLE action_items ADD COLUMN meeting_id INTEGER",
    ]:
        try:
            conn.execute(col)
        except Exception:
            pass
    conn.commit()
    _run_migration(conn, 'email_unique_sender_subject_date', _mig_email_unique_sender)
    conn.close()


def strip_html(html):
    if not html:
        return ""
    text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<p[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def clean_body(text):
    """Remove URL artifacts and email signature noise from parsed body text."""
    # Strip file:// / http:// / mailto: URLs that appear in angle brackets (Outlook plain-text links)
    text = re.sub(r'<(?:file|https?|mailto)[^>]*>', '', text)
    # Strip bare URLs
    text = re.sub(r'https?://\S+', '', text)
    # Strip [cid:...] inline image references
    text = re.sub(r'\[cid:[^\]]*\]', '', text)
    # Strip Windows UNC/share paths — long noise from Outlook shared folders
    text = re.sub(r'\\\\[^\s]+', '', text)
    # Strip lines that are only a Windows path (H:\ or similar) after the above
    text = re.sub(r'^[A-Z]:\\[^\n]*$', '', text, flags=re.MULTILINE)
    # Collapse blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def parse_eml(path):
    with open(path, "rb") as f:
        msg = email_lib.message_from_binary_file(f, policy=email_policy.default)
    sender = str(msg.get("From", ""))
    subject = str(msg.get("Subject", ""))
    date_str = str(msg.get("Date", ""))
    body_text = ""
    body_part = msg.get_body(preferencelist=("plain", "html"))
    if body_part:
        content = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            content = strip_html(content)
        body_text = clean_body(content)
    return sender, subject, date_str, body_text


def parse_msg(path):
    import extract_msg as emsg
    msg = emsg.openMsg(path)
    sender = str(msg.sender or "")
    subject = str(msg.subject or "")
    date_str = str(msg.date) if msg.date else ""
    html_body = msg.htmlBody
    if isinstance(html_body, bytes):
        html_body = html_body.decode("utf-8", errors="replace")
    body_text = strip_html(html_body) if html_body else str(msg.body or "")
    return sender, subject, date_str, clean_body(body_text)


def extract_items(body_text, subject, sender):
    prompt = f"""You are an assistant tracking action items for a Fire & Gas control systems engineer on a greenfield LNG EPC project.

Extract EVERY item that requires any attention or follow-up from this project email. Cast a wide net — include:
- Tasks and deliverables ("submit X by date Y", "issue document Z")
- Read / review / familiarise requests ("please read spec X", "familiarise yourself with Y")
- People or roles to note or contact ("X is the PIC for Y", "coordinate with Z")
- Technical queries or open questions needing a response
- Blocking points or dependencies

For each numbered or bulleted point in the email body, ask: does this require any action? If yes, include it.

Return a JSON array where each element has exactly these fields:
- "discipline": team who sent or owns this (one of: "HSED", "ICSS", "Electrical", "HVAC", "Telecom", "Instrumentation", "Other")
- "scope": area of work this relates to (one of: "SPI", "C&E", "FGS Layouts", "Document Review", "Interface", "General", "Other")
- "action": one clear sentence — what needs to happen
- "blocking_point": true only if this explicitly blocks progress, else false
- "deadline": date as YYYY-MM-DD if mentioned, else ""
- "category": exactly one of ["Comment response", "IFR submittal", "Technical query", "Information request", "Meeting action"]
- "priority": exactly one of ["Low", "Medium", "High", "Critical"]

EXAMPLE — for an email saying "Please read the FGS Spec. Jason LeBlanc is the PIC for FGS design.":
[
  {{"discipline": "HSED", "scope": "Document Review", "action": "Read and familiarise with FGS Specification", "blocking_point": false, "deadline": "", "category": "Information request", "priority": "Medium"}},
  {{"discipline": "HSED", "scope": "Interface", "action": "Note: Jason LeBlanc is PIC for FGS design — establish contact", "blocking_point": false, "deadline": "", "category": "Information request", "priority": "Low"}}
]

Return ONLY a valid JSON array. No explanation, no markdown fences, no other text.
If the email has no actionable content at all, return [].

EMAIL SUBJECT: {subject}
FROM: {sender}

BODY:
{body_text[:3000]}

JSON:"""

    try:
        # stream=True required — gemma4 returns empty response with stream=False
        with requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": _current_model["name"],
                "prompt": prompt,
                "stream": True,
                "think": False,
                "options": {"temperature": 0.1, "num_predict": 1024, "top_p": 0.9},
            },
            stream=True,
            timeout=120,
        ) as res:
            res.raise_for_status()
            text = ""
            for raw in res.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                obj = json.loads(raw)
                text += obj.get("response", "")
                if obj.get("done"):
                    break
        text = text.strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            items = json.loads(match.group())
            if isinstance(items, list):
                return items
    except Exception as e:
        print(f"[email_tracker] Extraction failed: {e}")
    return []


def _chunk_text(text, max_words=200, overlap=40):
    words = text.split()
    chunks, start = [], 0
    while start < len(words):
        end = min(start + max_words, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start += max_words - overlap
    return chunks


def _embed(text):
    try:
        res = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": text[:2000]},
            timeout=30,
        )
        return res.json().get("embedding", [])
    except Exception as e:
        print(f"[email_tracker] Embedding failed: {e}")
        return []


def _index_email_body(email_id, body_text, sender, sent_date, discipline):
    name, _ = parseaddr(sender)
    conn = get_db()
    conn.execute("DELETE FROM email_chunks WHERE email_id=?", (email_id,))
    for i, chunk in enumerate(_chunk_text(clean_body(body_text))):
        if len(chunk.strip()) < 30:
            continue
        emb = _embed(chunk)
        if emb:
            conn.execute(
                "INSERT INTO email_chunks "
                "(email_id, chunk_idx, text, sender, sent_date, discipline, embedding) "
                "VALUES (?,?,?,?,?,?,?)",
                (email_id, i, chunk, name or sender, sent_date, discipline,
                 json.dumps(emb)),
            )
    conn.commit()
    conn.close()
    _log(f"[email memory] Indexed email {email_id} ({len(_chunk_text(clean_body(body_text)))} chunk(s))")


def retrieve_email_chunks(question, top_k=3):
    """Cosine similarity search over indexed email chunks. Returns formatted chunk dicts."""
    q_emb = _embed(question)
    if not q_emb:
        return []
    conn = get_db()
    rows = conn.execute(
        "SELECT ec.*, e.subject FROM email_chunks ec "
        "JOIN emails e ON ec.email_id = e.id WHERE ec.embedding IS NOT NULL"
    ).fetchall()
    conn.close()
    if not rows:
        return []

    q_vec = np.array(q_emb, dtype=np.float32)
    q_norm = np.linalg.norm(q_vec)
    if q_norm == 0:
        return []

    scored = []
    for row in rows:
        try:
            emb = np.array(json.loads(row["embedding"]), dtype=np.float32)
            n = np.linalg.norm(emb)
            score = float(np.dot(q_vec, emb) / (q_norm * n)) if n > 0 else 0
            scored.append((score, row))
        except Exception:
            pass

    scored.sort(key=lambda x: -x[0])
    result = []
    seen = set()
    for _, row in scored:
        key = (row["email_id"], row["chunk_idx"])
        if key in seen:
            continue
        seen.add(key)
        date = (row["sent_date"] or "")[:10]
        sender_name = row["sender"] or ""
        result.append({
            "text": row["text"],
            "section": f"{sender_name} — {date}",
            "page": 0,
            "has_table": False,
            "source": "email",
            "doc_number": "Email",
            "title": sender_name,
            "revision": date,
            "is_email": True,
            "email_id": row["email_id"],
            "sent_date": row["sent_date"] or "",
            "sender": sender_name,
        })
        if len(result) >= top_k:
            break
    return result


def extract_contact(body_text, sender):
    """Extract position, operating center and discipline from email signature via Ollama."""
    name, email_addr = parseaddr(sender)
    signature = body_text[-800:] if len(body_text) > 800 else body_text
    prompt = f"""Extract the sender's contact details from this project email.

Return a JSON object with exactly these fields:
- "name": sender's full name
- "email": sender's email address
- "position": job title (e.g. "ICSS Lead", "FGS Engineer", "Project Manager"), else ""
- "operating_center": one of ["POC","HOC","BoOC","Owner","Vendor","Other"] (POC=Paris, HOC=Houston, BoOC=Bogota)
- "discipline": one of ["HSED","ICSS","Electrical","HVAC","Telecom","Instrumentation","Other"]

FROM: {sender}
EMAIL BODY (end — focus on signature):
{signature}

Return ONLY a valid JSON object. No markdown, no explanation.
JSON:"""
    try:
        with requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": _current_model["name"], "prompt": prompt, "stream": True,
                  "think": False, "options": {"temperature": 0.1, "num_predict": 256}},
            stream=True, timeout=60,
        ) as res:
            res.raise_for_status()
            text = ""
            for raw in res.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                obj = json.loads(raw)
                text += obj.get("response", "")
                if obj.get("done"):
                    break
        match = re.search(r'\{.*\}', text.strip(), re.DOTALL)
        if match:
            data = json.loads(match.group())
            return {
                "name": data.get("name", name),
                "email": data.get("email", email_addr),
                "position": data.get("position", ""),
                "operating_center": data.get("operating_center", "Other"),
                "discipline": data.get("discipline", "Other"),
            }
    except Exception as e:
        _log(f"[email memory] Contact extraction failed: {e}")
    return {"name": name, "email": email_addr, "position": "", "operating_center": "Other", "discipline": "Other"}


def upsert_contact(conn, contact, source="auto"):
    """Insert contact or update only blank fields if already exists (preserves manual edits)."""
    now = datetime.datetime.now().isoformat()
    existing = conn.execute(
        "SELECT id FROM contacts WHERE email=?", (contact["email"],)
    ).fetchone()
    if existing:
        conn.execute("""
            UPDATE contacts SET
                name             = CASE WHEN name=''             THEN ? ELSE name END,
                position         = CASE WHEN position=''         THEN ? ELSE position END,
                operating_center = CASE WHEN operating_center='' THEN ? ELSE operating_center END,
                discipline       = CASE WHEN discipline=''       THEN ? ELSE discipline END,
                updated_at       = ?
            WHERE email=?
        """, (contact["name"], contact["position"], contact["operating_center"],
               contact["discipline"], now, contact["email"]))
    else:
        conn.execute(
            "INSERT INTO contacts (name, email, position, operating_center, discipline, source, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (contact["name"], contact["email"], contact["position"],
             contact["operating_center"], contact["discipline"], source, now, now),
        )


@bp.route("/api/email/upload", methods=["POST"])
def upload_email():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    fname_lower = f.filename.lower()
    if not (fname_lower.endswith(".eml") or fname_lower.endswith(".msg")):
        return jsonify({"error": "Only .eml or .msg files are accepted"}), 400

    suffix = ".eml" if fname_lower.endswith(".eml") else ".msg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name
        f.save(tmp_path)

    try:
        if suffix == ".eml":
            sender, subject, sent_date, body_text = parse_eml(tmp_path)
        else:
            try:
                sender, subject, sent_date, body_text = parse_msg(tmp_path)
            except ImportError:
                return jsonify({"error": "extract-msg not installed — run: pip install extract-msg"}), 500
    except Exception as e:
        return jsonify({"error": f"Failed to parse file: {e}"}), 400
    finally:
        os.remove(tmp_path)

    conn = get_db()
    # Duplicate check — same sender + subject + date = same email
    existing = conn.execute(
        "SELECT id, imported_at FROM emails WHERE sender=? AND subject=? AND sent_date=?",
        (sender, subject, sent_date)
    ).fetchone()
    if existing:
        # Still upsert contact (idempotent) but skip action extraction
        contact = extract_contact(body_text, sender)
        upsert_contact(conn, contact, source="auto")
        conn.commit()
        conn.close()
        return jsonify({
            "duplicate": True,
            "email_id": existing["id"],
            "imported_at": existing["imported_at"],
            "sender": sender,
            "subject": subject,
        })

    cur = conn.execute(
        "INSERT INTO emails (filename, sender, subject, sent_date, body_text, imported_at) "
        "VALUES (?,?,?,?,?,?)",
        (f.filename, sender, subject, sent_date, body_text,
         datetime.datetime.now().isoformat()),
    )
    email_id = cur.lastrowid
    conn.commit()
    conn.close()

    items = extract_items(body_text, subject, sender)

    # Auto-extract and upsert contact from sender signature
    contact = extract_contact(body_text, sender)
    conn = get_db()
    upsert_contact(conn, contact, source="auto")
    conn.commit()
    conn.close()

    return jsonify({
        "email_id": email_id,
        "sender": sender,
        "subject": subject,
        "sent_date": sent_date,
        "items": items,
    })


@bp.route("/api/email/approve", methods=["POST"])
def approve_items():
    data = request.json or {}
    email_id = data.get("email_id")
    items = data.get("items", [])
    if not items:
        return jsonify({"ok": True, "saved": 0})

    now = datetime.datetime.now().isoformat()
    conn = get_db()
    for item in items:
        conn.execute(
            "INSERT INTO action_items "
            "(email_id, discipline, scope, action, blocking_point, "
            " deadline, category, priority, status, notes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (email_id,
             item.get("discipline", ""),
             item.get("scope", ""),
             item.get("action", ""),
             1 if item.get("blocking_point") else 0,
             item.get("deadline", ""),
             item.get("category", ""),
             item.get("priority", "Medium"),
             "Open",
             item.get("notes", ""),
             now),
        )
    conn.commit()
    conn.close()

    # Index email body for project memory (background — non-blocking)
    discipline = items[0].get("discipline", "") if items else ""
    email_row = get_db().execute(
        "SELECT body_text, sender, sent_date FROM emails WHERE id=?", (email_id,)
    ).fetchone()
    if email_row:
        t = threading.Thread(
            target=_index_email_body,
            args=(email_id, email_row["body_text"], email_row["sender"],
                  email_row["sent_date"], discipline),
            daemon=True,
        )
        t.start()

    return jsonify({"ok": True, "saved": len(items)})


@bp.route("/api/email/register", methods=["GET"])
def get_register():
    status_f = request.args.get("status", "")
    discipline_f = request.args.get("discipline", "")

    query = (
        "SELECT a.*, e.sender, e.subject, e.sent_date, e.filename AS email_filename "
        "FROM action_items a LEFT JOIN emails e ON a.email_id = e.id WHERE 1=1"
    )
    params = []
    if status_f:
        query += " AND a.status = ?"
        params.append(status_f)
    if discipline_f:
        query += " AND a.discipline = ?"
        params.append(discipline_f)
    query += " ORDER BY a.created_at DESC"

    conn = get_db()
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.close()
    for row in rows:
        try:
            row["notes"] = json.loads(row["notes"]) if row["notes"] else []
        except Exception:
            row["notes"] = []
    return jsonify({"items": rows})


@bp.route("/api/email/items/<int:item_id>", methods=["PUT"])
def update_item(item_id):
    data = request.json or {}
    conn = get_db()
    conn.execute("""
        UPDATE action_items SET
            action=?, discipline=?, scope=?, priority=?,
            deadline=?, blocking_point=?, status=?
        WHERE id=?
    """, (data.get("action", ""), data.get("discipline", ""), data.get("scope", ""),
          data.get("priority", "Medium"), data.get("deadline", ""),
          1 if data.get("blocking_point") else 0, data.get("status", "Open"),
          item_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@bp.route("/api/email/items/<int:item_id>/notes", methods=["POST"])
def add_note(item_id):
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "Note text required"}), 400
    conn = get_db()
    row = conn.execute("SELECT notes FROM action_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    try:
        notes = json.loads(row["notes"]) if row["notes"] else []
    except Exception:
        notes = []
    notes.append({"ts": datetime.datetime.now().isoformat(), "text": text})
    conn.execute("UPDATE action_items SET notes=? WHERE id=?", (json.dumps(notes), item_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "notes": notes})


@bp.route("/api/email/items/<int:item_id>/status", methods=["PUT"])
def update_status(item_id):
    data = request.json or {}
    status = data.get("status", "")
    if status not in STATUSES:
        return jsonify({"error": "Invalid status"}), 400
    conn = get_db()
    conn.execute("UPDATE action_items SET status=? WHERE id=?", (status, item_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@bp.route("/api/email/items/<int:item_id>", methods=["DELETE"])
def delete_item(item_id):
    conn = get_db()
    conn.execute("DELETE FROM action_items WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@bp.route("/api/email/<int:email_id>", methods=["GET"])
def get_email(email_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, sender, subject, sent_date, body_text, imported_at FROM emails WHERE id=?",
        (email_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row))


@bp.route("/api/email/<int:email_id>/reextract", methods=["POST"])
def reextract(email_id):
    conn = get_db()
    row = conn.execute(
        "SELECT sender, subject, sent_date, body_text FROM emails WHERE id=?", (email_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Email not found"}), 404
    items = extract_items(row["body_text"], row["subject"], row["sender"])
    return jsonify({
        "email_id": email_id,
        "sender": row["sender"],
        "subject": row["subject"],
        "sent_date": row["sent_date"],
        "items": items,
    })


@bp.route("/api/email/reindex", methods=["POST"])
def reindex_all():
    """Re-index all emails in the DB. Runs in background; returns immediately."""
    conn = get_db()
    emails = conn.execute("SELECT id, body_text, sender, sent_date FROM emails").fetchall()
    conn.close()

    def _run():
        for row in emails:
            # Use discipline from the first approved action item for this email
            c = get_db().execute(
                "SELECT discipline FROM action_items WHERE email_id=? LIMIT 1", (row["id"],)
            ).fetchone()
            discipline = c["discipline"] if c else ""
            _index_email_body(row["id"], row["body_text"], row["sender"],
                              row["sent_date"], discipline)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "emails": len(emails)})


# --- Contacts ---

@bp.route("/api/email/contacts", methods=["GET"])
def list_contacts():
    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM contacts ORDER BY name COLLATE NOCASE"
    ).fetchall()]
    conn.close()
    return jsonify({"contacts": rows})


@bp.route("/api/email/contacts", methods=["POST"])
def create_contact():
    data = request.json or {}
    now = datetime.datetime.now().isoformat()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO contacts (name, email, position, operating_center, discipline, notes, source, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (data.get("name", ""), data.get("email", ""), data.get("position", ""),
         data.get("operating_center", ""), data.get("discipline", ""),
         data.get("notes", ""), "manual", now, now),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": cur.lastrowid})


@bp.route("/api/email/contacts/<int:contact_id>", methods=["PUT"])
def update_contact(contact_id):
    data = request.json or {}
    now = datetime.datetime.now().isoformat()
    conn = get_db()
    conn.execute("""
        UPDATE contacts SET
            name=?, email=?, position=?, operating_center=?,
            discipline=?, notes=?, updated_at=?
        WHERE id=?
    """, (data.get("name", ""), data.get("email", ""), data.get("position", ""),
          data.get("operating_center", ""), data.get("discipline", ""),
          data.get("notes", ""), now, contact_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@bp.route("/api/email/contacts/<int:contact_id>", methods=["DELETE"])
def delete_contact_route(contact_id):
    conn = get_db()
    conn.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# --- Attachments ---

@bp.route("/api/email/items/<int:item_id>/attachments", methods=["GET"])
def list_attachments(item_id):
    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM attachments WHERE item_id=? ORDER BY uploaded_at DESC", (item_id,)
    ).fetchall()]
    conn.close()
    return jsonify({"attachments": rows})


@bp.route("/api/email/items/<int:item_id>/attachments", methods=["POST"])
def upload_attachment(item_id):
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    original_name = f.filename
    safe_name = re.sub(r'[^\w.\-]', '_', original_name)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{safe_name}"
    item_dir = os.path.join(ATTACHMENTS_DIR, str(item_id))
    os.makedirs(item_dir, exist_ok=True)
    f.save(os.path.join(item_dir, filename))
    now = datetime.datetime.now().isoformat()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO attachments (item_id, filename, original_name, uploaded_at) VALUES (?,?,?,?)",
        (item_id, filename, original_name, now),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": cur.lastrowid,
                    "filename": filename, "original_name": original_name, "uploaded_at": now})


@bp.route("/api/attachments/<int:attachment_id>")
def download_attachment(attachment_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    item_dir = os.path.abspath(os.path.join(ATTACHMENTS_DIR, str(row["item_id"])))
    return send_from_directory(item_dir, row["filename"],
                               as_attachment=True, download_name=row["original_name"])


@bp.route("/api/attachments/<int:attachment_id>", methods=["DELETE"])
def delete_attachment_route(attachment_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    if row:
        path = os.path.join(ATTACHMENTS_DIR, str(row["item_id"]), row["filename"])
        if os.path.exists(path):
            os.remove(path)
        conn.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
        conn.commit()
    conn.close()
    return jsonify({"ok": True})


# --- Meeting Transcription ---

@bp.route("/api/meetings/transcribe", methods=["POST"])
def transcribe_meeting():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    original_name = f.filename or "recording"
    suffix = os.path.splitext(original_name)[1].lower() or ".mp4"
    allowed = {".mp4", ".m4a", ".mp3", ".wav", ".webm", ".ogg"}
    if suffix not in allowed:
        return jsonify({"error": f"Unsupported format: {suffix}. Use mp4, m4a, mp3, wav."}), 400

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=MEETINGS_DIR) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name

        _log(f"[meetings] Transcribing {original_name} ({os.path.getsize(tmp_path)//1024} KB)...")
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return jsonify({"error": "faster-whisper is not installed on this server"}), 500

        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, info = model.transcribe(tmp_path, beam_size=5, language="en")
        parts = [seg.text.strip() for seg in segments]
        transcript = " ".join(parts)
        _log(f"[meetings] Done: {len(transcript)} chars, language={info.language}")
    except Exception as e:
        _log(f"[meetings] Transcription error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    # Parse Teams filename: "Teams meeting-20260429_133404UTC-Meeting Recording.mp4"
    parsed_title = ""
    parsed_date = ""
    m = re.match(r'Teams\s+meeting-(\d{8})_\d{6}\w*-(.+)\.\w+', original_name, re.IGNORECASE)
    if m:
        d = m.group(1)
        parsed_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        parsed_title = m.group(2).strip()

    return jsonify({"ok": True, "transcript": transcript, "filename": original_name,
                    "language": info.language, "parsed_title": parsed_title,
                    "parsed_date": parsed_date})


@bp.route("/api/meetings/extract", methods=["POST"])
def extract_meeting_items():
    data = request.json or {}
    transcript = data.get("transcript", "").strip()
    title = data.get("title", "Meeting").strip() or "Meeting"
    if not transcript:
        return jsonify({"error": "No transcript provided"}), 400

    items = extract_items(transcript, subject=title, sender="Meeting")
    for item in items:
        item["category"] = "Meeting action"

    return jsonify({"ok": True, "items": items, "title": title})


@bp.route("/api/meetings/approve", methods=["POST"])
def approve_meeting_items():
    data = request.json or {}
    title = data.get("title", "Meeting")
    recorded_date = data.get("recorded_date", "")
    transcript = data.get("transcript", "")
    items = data.get("items", [])
    if not items:
        return jsonify({"ok": True, "saved": 0})

    now = datetime.datetime.now().isoformat()
    conn = get_db()

    # Save meeting record
    cur = conn.execute(
        "INSERT INTO meetings (title, recorded_date, transcript, created_at) VALUES (?,?,?,?)",
        (title, recorded_date, transcript, now),
    )
    meeting_id = cur.lastrowid

    for item in items:
        conn.execute(
            "INSERT INTO action_items "
            "(email_id, meeting_id, discipline, scope, action, blocking_point, "
            " deadline, category, priority, status, notes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (None,
             meeting_id,
             item.get("discipline", ""),
             item.get("scope", ""),
             item.get("action", ""),
             1 if item.get("blocking_point") else 0,
             item.get("deadline", ""),
             "Meeting action",
             item.get("priority", "Medium"),
             "Open",
             "",
             now),
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "saved": len(items), "meeting_id": meeting_id})
