import os
import re
import json
import datetime
import sqlite3
import tempfile
import requests
from flask import Blueprint, request, jsonify

bp = Blueprint("email_tracker", __name__)

DB_PATH = os.environ.get("DB_PATH", "cwlng.db")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Shared with app.py current_model dict after registration — set by app.py
_current_model = {"name": os.environ.get("OLLAMA_MODEL", "gemma4:latest")}

DISCIPLINES = ["HSED HOC", "HSED BoOC", "Civil", "Piping", "Electrical",
               "Structural", "Vendor", "Other"]
CATEGORIES = ["Comment response", "IFR submittal", "Technical query",
              "Information request", "Meeting action", "Blocking point"]
PRIORITIES = ["Low", "Medium", "High", "Critical"]
STATUSES = ["Open", "In Progress", "Closed"]


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
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id      INTEGER,
            discipline    TEXT    DEFAULT '',
            document_ref  TEXT    DEFAULT '',
            action        TEXT    DEFAULT '',
            blocking_point INTEGER DEFAULT 0,
            deadline      TEXT    DEFAULT '',
            category      TEXT    DEFAULT '',
            priority      TEXT    DEFAULT 'Medium',
            status        TEXT    DEFAULT 'Open',
            notes         TEXT    DEFAULT '',
            created_at    TEXT,
            FOREIGN KEY (email_id) REFERENCES emails(id)
        );
    """)
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


def extract_items(body_text, subject, sender):
    prompt = f"""You are an assistant helping a Fire & Gas control systems engineer on a greenfield LNG EPC project.

Extract ALL action items, deliverables, technical queries, or blocking points from the project email below.
Return a JSON array. Each element must have exactly these fields:
- "discipline": team who sent or owns this (one of: "HSED HOC", "HSED BoOC", "Civil", "Piping", "Electrical", "Structural", "Vendor", "Other")
- "document_ref": document number or name if mentioned, else ""
- "action": clear one-sentence description of what needs to happen
- "blocking_point": true if this explicitly blocks progress, false otherwise
- "deadline": date in YYYY-MM-DD if mentioned, else ""
- "category": exactly one of ["Comment response", "IFR submittal", "Technical query", "Information request", "Meeting action", "Blocking point"]
- "priority": exactly one of ["Low", "Medium", "High", "Critical"]

If there are no action items, return [].
Return ONLY a valid JSON array. No explanation, no markdown fences, no other text.

EMAIL SUBJECT: {subject}
FROM: {sender}

BODY:
{body_text[:3000]}

JSON:"""

    try:
        res = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": _current_model["name"],
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 1024, "top_p": 0.9},
            },
            timeout=120,
        )
        res.raise_for_status()
        text = res.json().get("response", "").strip()
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if match:
            items = json.loads(match.group())
            if isinstance(items, list):
                return items
    except Exception as e:
        print(f"[email_tracker] Extraction failed: {e}")
    return []


@bp.route("/api/email/upload", methods=["POST"])
def upload_email():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".msg"):
        return jsonify({"error": "Only .msg files are accepted"}), 400

    try:
        import extract_msg as emsg
    except ImportError:
        return jsonify({"error": "extract-msg not installed — run: pip install extract-msg"}), 500

    with tempfile.NamedTemporaryFile(suffix=".msg", delete=False) as tmp:
        tmp_path = tmp.name
        f.save(tmp_path)

    try:
        msg = emsg.openMsg(tmp_path)
        sender = str(msg.sender or "")
        subject = str(msg.subject or "")
        sent_date = str(msg.date) if msg.date else ""
        html_body = msg.htmlBody
        if isinstance(html_body, bytes):
            html_body = html_body.decode("utf-8", errors="replace")
        body_text = strip_html(html_body) if html_body else str(msg.body or "")
    except Exception as e:
        return jsonify({"error": f"Failed to parse .msg: {e}"}), 400
    finally:
        os.remove(tmp_path)

    conn = get_db()
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
            "(email_id, discipline, document_ref, action, blocking_point, "
            " deadline, category, priority, status, notes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (email_id,
             item.get("discipline", ""),
             item.get("document_ref", ""),
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
    return jsonify({"items": rows})


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
