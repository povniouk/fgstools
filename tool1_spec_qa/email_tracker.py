import os
import re
import json
import datetime
import sqlite3
import tempfile
import requests
import email as email_lib
from email import policy as email_policy
from email.utils import parseaddr
from flask import Blueprint, request, jsonify

bp = Blueprint("email_tracker", __name__)

DB_PATH = os.environ.get("DB_PATH", "cwlng.db")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Shared with app.py current_model dict after registration — set by app.py
_current_model = {"name": os.environ.get("OLLAMA_MODEL", "gemma4:latest")}

DISCIPLINES = ["HSED", "ICSS", "Electrical", "HVAC", "Telecom", "Instrumentation", "Other"]
SCOPES     = ["SPI", "C&E", "FGS Layouts", "Document Review", "Interface", "General", "Other"]
CATEGORIES = ["Comment response", "IFR submittal", "Technical query",
              "Information request", "Meeting action"]
PRIORITIES = ["Low", "Medium", "High", "Critical"]
STATUSES   = ["Open", "In Progress", "Closed"]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
    # Migrations for existing databases
    for col in [
        "ALTER TABLE action_items ADD COLUMN scope TEXT DEFAULT ''",
    ]:
        try:
            conn.execute(col)
        except Exception:
            pass
    conn.commit()
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
        print(f"[email_tracker] Contact extraction failed: {e}")
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
