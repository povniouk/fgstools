import os
import re
import json
import datetime
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from flask import Blueprint, request, jsonify, send_file

bp = Blueprint('gaia', __name__)

DB_PATH = os.environ.get("DB_PATH", "cwlng.db")
_log = print

# Indexed columns mirror the Advitium attributes worth filtering on.
# Everything else lands in data_json so we can pull it later without re-importing.
_INDEXED = [
    ('ref',            'REF'),
    ('client_ref',     'CLIENT_REF'),
    ('title',          'TITLE'),
    ('rev',            'REV'),
    ('rev_purp',       'REV_PURP'),
    ('rev_date',       'REV_DATE'),
    ('state',          'STATE'),
    ('disc',           'DISC'),
    ('project_disc',   'PROJECT_DISC'),
    ('oc',             'OC'),
    ('unit',           'UNIT'),
    ('doc_type',       'DOC_TYPE'),
    ('doc_code',       'DOC_CODE'),
    ('trans_out_date', 'TRANS_OUT_DATE'),
    ('last_date',      'last_date'),
]


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


_DIFF_FIELDS = ['rev', 'rev_purp', 'rev_date', 'state', 'trans_out_date', 'title', 'client_ref']


def _run_migration(db, key, fn):
    db.execute("CREATE TABLE IF NOT EXISTS _migrations (key TEXT PRIMARY KEY)")
    db.commit()
    if not db.execute("SELECT 1 FROM _migrations WHERE key=?", (key,)).fetchone():
        fn(db)
        db.execute("INSERT OR IGNORE INTO _migrations (key) VALUES (?)", (key,))
        db.commit()


def _mig_gaia_unique_week(db):
    """Deduplicate gaia_imports by week_label (keep latest), then add UNIQUE constraint."""
    db.execute("PRAGMA foreign_keys = OFF")
    dupes = db.execute("""
        SELECT id FROM gaia_imports WHERE id NOT IN (
            SELECT MAX(id) FROM gaia_imports GROUP BY COALESCE(week_label, '')
        )
    """).fetchall()
    if dupes:
        ids = [r[0] for r in dupes]
        ph = ','.join('?' * len(ids))
        db.execute(f"DELETE FROM gaia_doc_history WHERE import_id IN ({ph})", ids)
        db.execute(f"DELETE FROM gaia_docs        WHERE import_id IN ({ph})", ids)
        db.execute(f"DELETE FROM gaia_imports      WHERE id IN ({ph})", ids)
        _log(f"[GAIA migration] Removed {len(ids)} duplicate import(s)")
    db.execute("""
        CREATE TABLE gaia_imports_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filename    TEXT,
            week_label  TEXT UNIQUE,
            imported_at TEXT,
            total_docs  INTEGER
        )
    """)
    db.execute("INSERT INTO gaia_imports_new SELECT * FROM gaia_imports")
    db.execute("DROP TABLE gaia_imports")
    db.execute("ALTER TABLE gaia_imports_new RENAME TO gaia_imports")
    db.execute("PRAGMA foreign_keys = ON")


def _mig_gaia_unique_docs(db):
    """Deduplicate gaia_docs within each import, then add UNIQUE(import_id, ref)."""
    db.execute("PRAGMA foreign_keys = OFF")
    dupes = db.execute("""
        SELECT id FROM gaia_docs WHERE ref IS NOT NULL AND id NOT IN (
            SELECT MIN(id) FROM gaia_docs
            WHERE ref IS NOT NULL
            GROUP BY import_id, ref
        )
    """).fetchall()
    if dupes:
        ids = [r[0] for r in dupes]
        ph = ','.join('?' * len(ids))
        db.execute(f"DELETE FROM gaia_docs WHERE id IN ({ph})", ids)
        _log(f"[GAIA migration] Removed {len(ids)} duplicate doc(s) within imports")
    db.execute("""
        CREATE TABLE gaia_docs_new (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id      INTEGER REFERENCES gaia_imports(id) ON DELETE CASCADE,
            ref            TEXT,
            client_ref     TEXT,
            title          TEXT,
            rev            TEXT,
            rev_purp       TEXT,
            rev_date       TEXT,
            state          TEXT,
            disc           TEXT,
            project_disc   TEXT,
            oc             TEXT,
            unit           TEXT,
            doc_type       TEXT,
            doc_code       TEXT,
            trans_out_date TEXT,
            last_date      TEXT,
            data_json      TEXT,
            UNIQUE(import_id, ref) ON CONFLICT IGNORE
        )
    """)
    db.execute("INSERT INTO gaia_docs_new SELECT * FROM gaia_docs")
    db.execute("DROP TABLE gaia_docs")
    db.execute("ALTER TABLE gaia_docs_new RENAME TO gaia_docs")
    db.execute("PRAGMA foreign_keys = ON")


def _mig_gaia_files_dedup(db):
    """Deduplicate gaia_doc_files, then add UNIQUE(ref, original_name, source)."""
    db.execute("PRAGMA foreign_keys = OFF")
    dupes = db.execute("""
        SELECT id FROM gaia_doc_files WHERE id NOT IN (
            SELECT MIN(id) FROM gaia_doc_files
            GROUP BY ref, COALESCE(original_name, filename), COALESCE(source, 'upload')
        )
    """).fetchall()
    if dupes:
        ids = [r[0] for r in dupes]
        ph = ','.join('?' * len(ids))
        db.execute(f"DELETE FROM gaia_doc_files WHERE id IN ({ph})", ids)
        _log(f"[GAIA migration] Removed {len(ids)} duplicate file record(s)")
    db.execute("""
        CREATE TABLE gaia_doc_files_new (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ref           TEXT NOT NULL,
            filename      TEXT NOT NULL,
            original_name TEXT,
            file_path     TEXT NOT NULL,
            file_size     INTEGER,
            uploaded_at   TEXT,
            source        TEXT DEFAULT 'upload',
            UNIQUE(ref, original_name, source) ON CONFLICT IGNORE
        )
    """)
    db.execute("INSERT INTO gaia_doc_files_new SELECT * FROM gaia_doc_files")
    db.execute("DROP TABLE gaia_doc_files")
    db.execute("ALTER TABLE gaia_doc_files_new RENAME TO gaia_doc_files")
    db.execute("PRAGMA foreign_keys = ON")


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS gaia_imports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filename    TEXT,
            week_label  TEXT,
            imported_at TEXT,
            total_docs  INTEGER
        );
        CREATE TABLE IF NOT EXISTS gaia_docs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id      INTEGER REFERENCES gaia_imports(id) ON DELETE CASCADE,
            ref            TEXT,
            client_ref     TEXT,
            title          TEXT,
            rev            TEXT,
            rev_purp       TEXT,
            rev_date       TEXT,
            state          TEXT,
            disc           TEXT,
            project_disc   TEXT,
            oc             TEXT,
            unit           TEXT,
            doc_type       TEXT,
            doc_code       TEXT,
            trans_out_date TEXT,
            last_date      TEXT,
            data_json      TEXT
        );
        CREATE TABLE IF NOT EXISTS gaia_doc_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id   INTEGER REFERENCES gaia_imports(id) ON DELETE CASCADE,
            ref         TEXT,
            title       TEXT,
            disc        TEXT,
            change_type TEXT,
            changes     TEXT
        );
        CREATE TABLE IF NOT EXISTS gaia_watchlist (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ref              TEXT,
            client_ref       TEXT,
            title            TEXT,
            priority         TEXT DEFAULT 'Normal',
            notes            TEXT,
            added_via        TEXT,
            added_date       TEXT,
            last_seen_rev    TEXT,
            last_seen_state  TEXT,
            acknowledged_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS gaia_doc_files (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ref           TEXT NOT NULL,
            filename      TEXT NOT NULL,
            original_name TEXT,
            file_path     TEXT NOT NULL,
            file_size     INTEGER,
            uploaded_at   TEXT,
            source        TEXT DEFAULT 'upload'
        );
    """)
    _run_migration(db, 'gaia_unique_week_label',   _mig_gaia_unique_week)
    _run_migration(db, 'gaia_unique_docs_in_import', _mig_gaia_unique_docs)
    _run_migration(db, 'gaia_files_dedup',           _mig_gaia_files_dedup)
    # Indexes last — recreated after any table migrations above
    db.executescript("""
        CREATE INDEX        IF NOT EXISTS idx_gaia_docs_import     ON gaia_docs(import_id);
        CREATE INDEX        IF NOT EXISTS idx_gaia_docs_ref        ON gaia_docs(ref);
        CREATE INDEX        IF NOT EXISTS idx_gaia_docs_client_ref ON gaia_docs(client_ref);
        CREATE INDEX        IF NOT EXISTS idx_gaia_docs_disc       ON gaia_docs(disc);
        CREATE INDEX        IF NOT EXISTS idx_gaia_docs_state      ON gaia_docs(state);
        CREATE INDEX        IF NOT EXISTS idx_gaia_docs_oc         ON gaia_docs(oc);
        CREATE INDEX        IF NOT EXISTS idx_gaia_hist_import     ON gaia_doc_history(import_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_gaia_wl_ref          ON gaia_watchlist(ref) WHERE ref IS NOT NULL;
        CREATE INDEX        IF NOT EXISTS idx_gaia_files_ref       ON gaia_doc_files(ref);
    """)
    db.commit()
    db.close()


def _compute_diff(db, new_id, old_id):
    cols = 'ref, title, disc, ' + ', '.join(_DIFF_FIELDS)
    new_docs = {r['ref']: dict(r) for r in
                db.execute(f"SELECT {cols} FROM gaia_docs WHERE import_id=?", (new_id,))}
    old_docs = {r['ref']: dict(r) for r in
                db.execute(f"SELECT {cols} FROM gaia_docs WHERE import_id=?", (old_id,))}

    rows = []
    for ref, d in new_docs.items():
        if ref not in old_docs:
            rows.append((new_id, ref, d['title'], d['disc'], 'new', None))
    for ref, d in old_docs.items():
        if ref not in new_docs:
            rows.append((new_id, ref, d['title'], d['disc'], 'removed', None))
    for ref in set(new_docs) & set(old_docs):
        nd, od = new_docs[ref], old_docs[ref]
        changes = {f: {'old': od[f], 'new': nd[f]} for f in _DIFF_FIELDS if nd[f] != od[f]}
        if changes:
            rows.append((new_id, ref, nd['title'], nd['disc'], 'changed', json.dumps(changes)))

    if rows:
        db.executemany(
            "INSERT INTO gaia_doc_history (import_id, ref, title, disc, change_type, changes) "
            "VALUES (?,?,?,?,?,?)",
            rows
        )
    return len([r for r in rows if r[4] == 'new']), \
           len([r for r in rows if r[4] == 'removed']), \
           len([r for r in rows if r[4] == 'changed'])


def _extract_week(filename):
    """yyyymmdd date prefix → ISO date label (e.g. '20260506-...' → '2026-05-06')."""
    m = re.search(r'(\d{4})(\d{2})(\d{2})', filename)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            pass
    return datetime.date.today().isoformat()


def _strip_ns(tag):
    """Remove `{ns}` prefix from element tags. Advitium xml omits the default namespace, but be safe."""
    return tag.split('}', 1)[-1] if '}' in tag else tag


def parse_gaia_xml(filepath):
    """Stream-parse Advitium engineering doc xml. Returns list of attribute dicts."""
    docs = []
    for _ev, el in ET.iterparse(filepath, events=('end',)):
        if _strip_ns(el.tag) == 'Element' and el.attrib:
            docs.append(dict(el.attrib))
            el.clear()
    return docs


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.route('/api/gaia/import', methods=['POST'])
def import_gaia():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.xml'):
        return jsonify({'error': 'Only .xml files accepted (Advitium export)'}), 400

    with tempfile.NamedTemporaryFile(suffix='.xml', delete=False) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    week_label = _extract_week(f.filename)

    # Reject duplicate week — same date already imported
    db = get_db()
    existing = db.execute(
        "SELECT id, filename, imported_at FROM gaia_imports WHERE week_label=?", (week_label,)
    ).fetchone()
    db.close()
    if existing:
        return jsonify({
            'error': f'Already imported: {week_label} (file: {existing["filename"]}, '
                     f'imported {existing["imported_at"][:10]}). '
                     f'Delete the existing import first if you want to replace it.',
            'existing_import_id': existing['id'],
        }), 409

    _log(f"[GAIA] Importing {f.filename} ({week_label})...")

    try:
        docs = parse_gaia_xml(tmp_path)
    except ET.ParseError as e:
        return jsonify({'error': f'XML parse error: {e}'}), 400
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if not docs:
        return jsonify({'error': 'No <Element> records found in xml'}), 400

    now = datetime.datetime.utcnow().isoformat()
    db = get_db()

    prev = db.execute(
        "SELECT id FROM gaia_imports ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev_id = prev['id'] if prev else None

    cur = db.execute(
        "INSERT INTO gaia_imports (filename, week_label, imported_at, total_docs) VALUES (?,?,?,?)",
        (f.filename, week_label, now, len(docs))
    )
    import_id = cur.lastrowid

    rows = []
    for d in docs:
        row = {'import_id': import_id, 'data_json': json.dumps(d, ensure_ascii=False)}
        for col, attr in _INDEXED:
            v = d.get(attr)
            row[col] = v if (v is None or v != '') else None
        rows.append(row)

    cols = ['import_id'] + [c for c, _ in _INDEXED] + ['data_json']
    placeholders = ', '.join(':' + c for c in cols)
    db.executemany(
        f"INSERT OR IGNORE INTO gaia_docs ({', '.join(cols)}) VALUES ({placeholders})",
        rows
    )

    n_added = n_removed = n_changed = 0
    if prev_id:
        n_added, n_removed, n_changed = _compute_diff(db, import_id, prev_id)
        _log(f"[GAIA] Diff vs import {prev_id}: +{n_added} new, -{n_removed} removed, ~{n_changed} changed")

    db.commit()

    by_disc = db.execute(
        "SELECT disc, COUNT(*) c FROM gaia_docs WHERE import_id=? GROUP BY disc ORDER BY c DESC",
        (import_id,)
    ).fetchall()
    by_state = db.execute(
        "SELECT state, COUNT(*) c FROM gaia_docs WHERE import_id=? GROUP BY state ORDER BY c DESC",
        (import_id,)
    ).fetchall()
    by_oc = db.execute(
        "SELECT oc, COUNT(*) c FROM gaia_docs WHERE import_id=? GROUP BY oc ORDER BY c DESC",
        (import_id,)
    ).fetchall()
    db.close()

    _log(f"[GAIA] Done: {len(docs)} docs imported (import_id={import_id})")
    return jsonify({
        'ok': True,
        'import_id': import_id,
        'week_label': week_label,
        'total_docs': len(docs),
        'by_disc':  [{'disc':  r['disc']  or '(blank)', 'count': r['c']} for r in by_disc],
        'by_state': [{'state': r['state'] or '(blank)', 'count': r['c']} for r in by_state],
        'by_oc':    [{'oc':    r['oc']    or '(blank)', 'count': r['c']} for r in by_oc],
        'diff': {'added': n_added, 'removed': n_removed, 'changed': n_changed} if prev_id else None,
    })


@bp.route('/api/gaia/imports', methods=['GET'])
def list_imports():
    db = get_db()
    rows = db.execute("SELECT * FROM gaia_imports ORDER BY id DESC").fetchall()
    db.close()
    return jsonify({'imports': [dict(r) for r in rows]})


@bp.route('/api/gaia/imports/<int:import_id>', methods=['DELETE'])
def delete_import(import_id):
    db = get_db()
    row = db.execute("SELECT id FROM gaia_imports WHERE id=?", (import_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({'error': 'Import not found'}), 404
    db.execute("DELETE FROM gaia_imports WHERE id=?", (import_id,))
    db.commit()
    db.close()
    _log(f"[GAIA] Deleted import {import_id}")
    return jsonify({'ok': True})


@bp.route('/api/gaia/imports/<int:import_id>/summary', methods=['GET'])
def import_summary(import_id):
    db = get_db()
    imp = db.execute("SELECT * FROM gaia_imports WHERE id=?", (import_id,)).fetchone()
    if not imp:
        db.close()
        return jsonify({'error': 'Import not found'}), 404
    by_disc = db.execute(
        "SELECT disc, COUNT(*) c FROM gaia_docs WHERE import_id=? GROUP BY disc ORDER BY c DESC",
        (import_id,)
    ).fetchall()
    by_state = db.execute(
        "SELECT state, COUNT(*) c FROM gaia_docs WHERE import_id=? GROUP BY state ORDER BY c DESC",
        (import_id,)
    ).fetchall()
    by_oc = db.execute(
        "SELECT oc, COUNT(*) c FROM gaia_docs WHERE import_id=? GROUP BY oc ORDER BY c DESC",
        (import_id,)
    ).fetchall()
    db.close()
    return jsonify({
        'import': dict(imp),
        'by_disc':  [{'disc':  r['disc']  or '(blank)', 'count': r['c']} for r in by_disc],
        'by_state': [{'state': r['state'] or '(blank)', 'count': r['c']} for r in by_state],
        'by_oc':    [{'oc':    r['oc']    or '(blank)', 'count': r['c']} for r in by_oc],
    })


@bp.route('/api/gaia/imports/<int:import_id>/diff', methods=['GET'])
def import_diff(import_id):
    db = get_db()
    if not db.execute("SELECT id FROM gaia_imports WHERE id=?", (import_id,)).fetchone():
        db.close()
        return jsonify({'error': 'Import not found'}), 404
    rows = db.execute(
        "SELECT ref, title, disc, change_type, changes FROM gaia_doc_history "
        "WHERE import_id=? ORDER BY change_type, ref",
        (import_id,)
    ).fetchall()
    db.close()
    if not rows:
        return jsonify({'diff': None, 'message': 'No diff recorded (first import or no changes)'})
    _key = {'new': 'added', 'removed': 'removed', 'changed': 'changed'}
    result = {'added': [], 'removed': [], 'changed': []}
    for r in rows:
        entry = {'ref': r['ref'], 'title': r['title'], 'disc': r['disc']}
        if r['change_type'] == 'changed':
            entry['changes'] = json.loads(r['changes'])
        result[_key[r['change_type']]].append(entry)
    return jsonify({'diff': result})


@bp.route('/api/gaia/docs', methods=['GET'])
def get_docs():
    """Lightweight register listing — supports filters that M2 will surface in the UI."""
    import_id = request.args.get('import_id')
    disc      = request.args.get('disc', '')
    oc        = request.args.get('oc', '')
    state     = request.args.get('state', '')
    rev_purp  = request.args.get('rev_purp', '')
    q_search  = request.args.get('q', '').strip()
    limit     = min(int(request.args.get('limit', 5000)), 10000)

    db = get_db()
    if not import_id:
        latest = db.execute("SELECT id FROM gaia_imports ORDER BY id DESC LIMIT 1").fetchone()
        if not latest:
            db.close()
            return jsonify({'docs': [], 'import_id': None, 'count': 0})
        import_id = latest['id']

    q = ("SELECT id, ref, client_ref, title, rev, rev_purp, rev_date, state, "
         "disc, oc, unit, doc_type, trans_out_date FROM gaia_docs WHERE import_id = ?")
    params = [import_id]
    if disc:     q += " AND disc = ?";     params.append(disc)
    if oc:       q += " AND oc = ?";       params.append(oc)
    if state:    q += " AND state = ?";    params.append(state)
    if rev_purp: q += " AND rev_purp = ?"; params.append(rev_purp)
    if q_search:
        q += " AND (ref LIKE ? OR client_ref LIKE ? OR title LIKE ?)"
        like = f"%{q_search}%"
        params.extend([like, like, like])
    q += f" ORDER BY ref LIMIT {limit}"

    docs = [dict(r) for r in db.execute(q, params).fetchall()]
    total_in_import = db.execute(
        "SELECT COUNT(*) FROM gaia_docs WHERE import_id=?", (import_id,)
    ).fetchone()[0]
    db.close()
    return jsonify({'docs': docs, 'import_id': int(import_id), 'count': len(docs),
                    'total_in_import': total_in_import})


@bp.route('/api/gaia/docs/<int:doc_id>', methods=['GET'])
def get_doc_detail(doc_id):
    db = get_db()
    row = db.execute("SELECT * FROM gaia_docs WHERE id = ?", (doc_id,)).fetchone()
    db.close()
    if not row:
        return jsonify({'error': 'Doc not found'}), 404
    out = dict(row)
    if out.get('data_json'):
        out['data'] = json.loads(out['data_json'])
        del out['data_json']
    return jsonify(out)


# ── Watchlist ─────────────────────────────────────────────────────────────────

SPECS_DIR      = os.environ.get("SPECS_DIR",      os.path.expanduser("~/spec-qa/specs"))
GAIA_FILES_DIR = os.environ.get("GAIA_FILES_DIR", os.path.expanduser("~/spec-qa/gaia_files"))


def _safe_ref(ref):
    return re.sub(r'[^A-Za-z0-9_\-]', '_', ref)


def _files_for_refs(db, refs):
    """Return {ref: [{id, original_name}]} for a list of refs."""
    if not refs:
        return {}
    ph = ','.join('?' * len(refs))
    rows = db.execute(
        f"SELECT ref, id, original_name FROM gaia_doc_files WHERE ref IN ({ph})", refs
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r['ref'], []).append({'id': r['id'], 'name': r['original_name'] or r['id']})
    return out


def _latest_import_id(db):
    row = db.execute("SELECT id FROM gaia_imports ORDER BY id DESC LIMIT 1").fetchone()
    return row['id'] if row else None


def _current_doc_state(db, import_id, ref, client_ref):
    """Return the gaia_docs row for this ref/client_ref in the given import."""
    if ref:
        row = db.execute(
            "SELECT id, ref, client_ref, title, rev, rev_purp, state, trans_out_date "
            "FROM gaia_docs WHERE import_id=? AND ref=?", (import_id, ref)
        ).fetchone()
        if row:
            return dict(row)
    if client_ref:
        row = db.execute(
            "SELECT id, ref, client_ref, title, rev, rev_purp, state, trans_out_date "
            "FROM gaia_docs WHERE import_id=? AND client_ref=?", (import_id, client_ref)
        ).fetchone()
        if row:
            return dict(row)
    return None


def _wl_to_dict(w, current):
    d = dict(w)
    d['current'] = current
    if current:
        rev_changed   = current['rev']   != w['last_seen_rev']   if w['last_seen_rev']   else True
        state_changed = current['state'] != w['last_seen_state'] if w['last_seen_state'] else True
        d['delta'] = []
        if rev_changed:   d['delta'].append('REV')
        if state_changed: d['delta'].append('STATE')
    else:
        d['delta'] = []
    return d


@bp.route('/api/gaia/watchlist', methods=['GET'])
def get_watchlist():
    db = get_db()
    imp_id = _latest_import_id(db)
    items  = db.execute("SELECT * FROM gaia_watchlist ORDER BY priority DESC, added_date").fetchall()
    wl_refs = [w['ref'] for w in items if w['ref']]
    files_by_ref = _files_for_refs(db, wl_refs)
    result = []
    for w in items:
        current = _current_doc_state(db, imp_id, w['ref'], w['client_ref']) if imp_id else None
        d = _wl_to_dict(w, current)
        d['files'] = files_by_ref.get(w['ref'], [])
        result.append(d)
    db.close()
    return jsonify({'items': result, 'import_id': imp_id})


@bp.route('/api/gaia/watchlist', methods=['POST'])
def add_watchlist():
    body = request.get_json(force=True)
    ref        = (body.get('ref') or '').strip() or None
    client_ref = (body.get('client_ref') or '').strip() or None
    if not ref and not client_ref:
        return jsonify({'error': 'Provide ref or client_ref'}), 400
    title    = (body.get('title') or '').strip() or None
    priority = body.get('priority', 'Normal')
    notes    = (body.get('notes') or '').strip() or None
    added_via = body.get('added_via', 'manual')
    now = datetime.date.today().isoformat()
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO gaia_watchlist (ref, client_ref, title, priority, notes, added_via, added_date) "
            "VALUES (?,?,?,?,?,?,?)",
            (ref, client_ref, title, priority, notes, added_via, now)
        )
        db.commit()
        row_id = cur.lastrowid
    except sqlite3.IntegrityError:
        db.close()
        return jsonify({'error': f'Already on watchlist: {ref}'}), 409
    imp_id  = _latest_import_id(db)
    current = _current_doc_state(db, imp_id, ref, client_ref) if imp_id else None
    item    = dict(db.execute("SELECT * FROM gaia_watchlist WHERE id=?", (row_id,)).fetchone())
    db.close()
    return jsonify({'ok': True, 'item': _wl_to_dict(item, current)})


@bp.route('/api/gaia/watchlist/bulk', methods=['POST'])
def bulk_watchlist():
    body = request.get_json(force=True)
    refs_raw = body.get('refs', '')
    priority = body.get('priority', 'Normal')
    added_via = 'bulk'
    now = datetime.date.today().isoformat()
    refs = [r.strip() for r in re.split(r'[\n,;]+', refs_raw) if r.strip()]
    if not refs:
        return jsonify({'error': 'No refs provided'}), 400
    db = get_db()
    imp_id = _latest_import_id(db)
    added = skipped = 0
    for ref in refs:
        # Determine whether it looks like a CLIENT_REF or internal REF
        is_client = ref.upper().startswith('CWLNG')
        r   = None if is_client else ref
        cr  = ref  if is_client else None
        # Try to fill in title from latest import
        title = None
        if imp_id:
            doc = _current_doc_state(db, imp_id, r, cr)
            if doc:
                title = doc.get('title')
                if not r:   r  = doc.get('ref')
                if not cr:  cr = doc.get('client_ref')
        try:
            db.execute(
                "INSERT INTO gaia_watchlist (ref, client_ref, title, priority, added_via, added_date) "
                "VALUES (?,?,?,?,?,?)", (r, cr, title, priority, added_via, now)
            )
            added += 1
        except sqlite3.IntegrityError:
            skipped += 1
    db.commit()
    db.close()
    return jsonify({'ok': True, 'added': added, 'skipped': skipped})


@bp.route('/api/gaia/watchlist/<int:item_id>', methods=['PATCH'])
def update_watchlist(item_id):
    body = request.get_json(force=True)
    allowed = {'priority', 'notes', 'title'}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return jsonify({'error': 'Nothing to update'}), 400
    db = get_db()
    if not db.execute("SELECT id FROM gaia_watchlist WHERE id=?", (item_id,)).fetchone():
        db.close(); return jsonify({'error': 'Not found'}), 404
    set_clause = ', '.join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE gaia_watchlist SET {set_clause} WHERE id=?",
               list(updates.values()) + [item_id])
    db.commit(); db.close()
    return jsonify({'ok': True})


@bp.route('/api/gaia/watchlist/<int:item_id>', methods=['DELETE'])
def delete_watchlist(item_id):
    db = get_db()
    if not db.execute("SELECT id FROM gaia_watchlist WHERE id=?", (item_id,)).fetchone():
        db.close(); return jsonify({'error': 'Not found'}), 404
    db.execute("DELETE FROM gaia_watchlist WHERE id=?", (item_id,))
    db.commit(); db.close()
    return jsonify({'ok': True})


@bp.route('/api/gaia/watchlist/<int:item_id>/acknowledge', methods=['POST'])
def acknowledge_watchlist(item_id):
    db = get_db()
    w = db.execute("SELECT * FROM gaia_watchlist WHERE id=?", (item_id,)).fetchone()
    if not w:
        db.close(); return jsonify({'error': 'Not found'}), 404
    imp_id  = _latest_import_id(db)
    current = _current_doc_state(db, imp_id, w['ref'], w['client_ref']) if imp_id else None
    now = datetime.datetime.utcnow().isoformat()
    db.execute(
        "UPDATE gaia_watchlist SET last_seen_rev=?, last_seen_state=?, acknowledged_at=? WHERE id=?",
        (current['rev'] if current else None,
         current['state'] if current else None,
         now, item_id)
    )
    db.commit(); db.close()
    return jsonify({'ok': True})


@bp.route('/api/gaia/watchlist/seed', methods=['POST'])
def seed_watchlist():
    """Try to match spec PDFs in SPECS_DIR against the latest import's CLIENT_REFs."""
    if not os.path.isdir(SPECS_DIR):
        return jsonify({'error': f'Specs dir not found: {SPECS_DIR}'}), 404
    db = get_db()
    imp_id = _latest_import_id(db)
    if not imp_id:
        db.close()
        return jsonify({'error': 'No imports yet — import a GAIA XML first'}), 400

    # Load all client_refs and internal refs from latest import for fast lookup
    all_docs = {r['client_ref']: dict(r) for r in
                db.execute("SELECT ref, client_ref, title FROM gaia_docs WHERE import_id=? AND client_ref IS NOT NULL", (imp_id,))}
    all_ref_docs = {r['ref']: dict(r) for r in
                    db.execute("SELECT ref, client_ref, title FROM gaia_docs WHERE import_id=? AND ref IS NOT NULL", (imp_id,))}

    now = datetime.date.today().isoformat()
    added = skipped = unmatched = 0
    unmatched_names = []

    for fname in os.listdir(SPECS_DIR):
        if not fname.lower().endswith('.pdf'):
            continue
        # Try CLIENT_REF pattern (CWLNG-...) first, then internal REF pattern (078051C-...)
        cr_key = r_key = None
        m = re.search(r'(CWLNG[-_]\w+[-_]\w+[-_]\w+[-_]\w+[-_]\d+)', fname, re.IGNORECASE)
        if m:
            cr_key = m.group(1).replace('_', '-').upper()
            doc = all_docs.get(cr_key)
        else:
            # Try internal REF: e.g. 078051C-000-CN-1930-0002
            m2 = re.search(r'(\d{6}[A-Z]-\d{3}-[A-Z]+-\d{4}-\d{4})', fname, re.IGNORECASE)
            if m2:
                r_key = m2.group(1).upper()
                doc = all_ref_docs.get(r_key)
            else:
                unmatched += 1; unmatched_names.append(fname); continue
        if not doc:
            unmatched += 1; unmatched_names.append(fname); continue
        try:
            db.execute(
                "INSERT INTO gaia_watchlist (ref, client_ref, title, priority, added_via, added_date) "
                "VALUES (?,?,?,?,?,?)",
                (doc['ref'], doc['client_ref'], doc['title'], 'Normal', 'seed', now)
            )
            added += 1
        except sqlite3.IntegrityError:
            skipped += 1

    db.commit(); db.close()
    _log(f"[GAIA] Seed: +{added} added, {skipped} already watched, {unmatched} unmatched")
    return jsonify({'ok': True, 'added': added, 'skipped': skipped,
                    'unmatched': unmatched, 'unmatched_names': unmatched_names})


# ── Acknowledge all ───────────────────────────────────────────────────────────

@bp.route('/api/gaia/watchlist/acknowledge-all', methods=['POST'])
def acknowledge_all_watchlist():
    db = get_db()
    imp_id = _latest_import_id(db)
    items  = db.execute("SELECT * FROM gaia_watchlist").fetchall()
    now    = datetime.datetime.utcnow().isoformat()
    updated = 0
    for w in items:
        current = _current_doc_state(db, imp_id, w['ref'], w['client_ref']) if imp_id else None
        d = _wl_to_dict(w, current)
        if d['delta']:
            db.execute(
                "UPDATE gaia_watchlist SET last_seen_rev=?, last_seen_state=?, acknowledged_at=? WHERE id=?",
                (current['rev'] if current else None,
                 current['state'] if current else None,
                 now, w['id'])
            )
            updated += 1
    db.commit(); db.close()
    return jsonify({'ok': True, 'updated': updated})


# ── Change history for a REF ──────────────────────────────────────────────────

@bp.route('/api/gaia/history/<path:ref>', methods=['GET'])
def doc_history(ref):
    db = get_db()
    rows = db.execute(
        "SELECT h.change_type, h.changes, i.week_label, i.imported_at "
        "FROM gaia_doc_history h JOIN gaia_imports i ON h.import_id = i.id "
        "WHERE h.ref = ? ORDER BY i.id DESC",
        (ref,)
    ).fetchall()
    db.close()
    result = []
    for r in rows:
        entry = {'change_type': r['change_type'], 'week_label': r['week_label'],
                 'imported_at': r['imported_at']}
        if r['changes']:
            entry['changes'] = json.loads(r['changes'])
        result.append(entry)
    return jsonify({'ref': ref, 'history': result})


# ── Document file attachment ──────────────────────────────────────────────────

@bp.route('/api/gaia/refs/<path:ref>/files', methods=['GET'])
def list_doc_files(ref):
    db = get_db()
    rows = db.execute(
        "SELECT id, original_name, file_size, uploaded_at, source FROM gaia_doc_files WHERE ref=? ORDER BY uploaded_at DESC",
        (ref,)
    ).fetchall()
    db.close()
    return jsonify({'files': [dict(r) for r in rows]})


@bp.route('/api/gaia/refs/<path:ref>/files', methods=['POST'])
def upload_doc_file(ref):
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['file']
    dest_dir = os.path.join(GAIA_FILES_DIR, _safe_ref(ref))
    os.makedirs(dest_dir, exist_ok=True)
    # Avoid name collisions
    base, ext = os.path.splitext(f.filename)
    filename = f.filename
    dest = os.path.join(dest_dir, filename)
    if os.path.exists(dest):
        filename = f"{base}_{int(datetime.datetime.utcnow().timestamp())}{ext}"
        dest = os.path.join(dest_dir, filename)
    f.save(dest)
    size = os.path.getsize(dest)
    now  = datetime.datetime.utcnow().isoformat()
    db   = get_db()
    cur  = db.execute(
        "INSERT INTO gaia_doc_files (ref, filename, original_name, file_path, file_size, uploaded_at, source) "
        "VALUES (?,?,?,?,?,?,?)",
        (ref, filename, f.filename, dest, size, now, 'upload')
    )
    db.commit()
    fid = cur.lastrowid
    db.close()
    _log(f"[GAIA] File uploaded: {f.filename} → {ref} (id={fid})")
    return jsonify({'ok': True, 'id': fid, 'name': f.filename, 'size': size})


@bp.route('/api/gaia/files/<int:file_id>', methods=['GET'])
def serve_doc_file(file_id):
    db = get_db()
    row = db.execute("SELECT * FROM gaia_doc_files WHERE id=?", (file_id,)).fetchone()
    db.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    path = row['file_path']
    if not os.path.isfile(path):
        return jsonify({'error': 'File missing from disk'}), 404
    return send_file(path, as_attachment=False, download_name=row['original_name'] or row['filename'])


@bp.route('/api/gaia/files/<int:file_id>', methods=['DELETE'])
def delete_doc_file(file_id):
    db = get_db()
    row = db.execute("SELECT * FROM gaia_doc_files WHERE id=?", (file_id,)).fetchone()
    if not row:
        db.close(); return jsonify({'error': 'Not found'}), 404
    try:
        os.remove(row['file_path'])
    except OSError:
        pass
    db.execute("DELETE FROM gaia_doc_files WHERE id=?", (file_id,))
    db.commit(); db.close()
    return jsonify({'ok': True})


@bp.route('/api/gaia/files/link-specs', methods=['POST'])
def link_specs_to_docs():
    """Auto-link PDFs in SPECS_DIR to GAIA entries via REF/CLIENT_REF pattern matching."""
    if not os.path.isdir(SPECS_DIR):
        return jsonify({'error': f'Specs dir not found: {SPECS_DIR}'}), 404
    db = get_db()
    imp_id = _latest_import_id(db)
    if not imp_id:
        db.close(); return jsonify({'error': 'No imports yet'}), 400
    all_docs     = {r['client_ref']: dict(r) for r in db.execute(
        "SELECT ref, client_ref FROM gaia_docs WHERE import_id=? AND client_ref IS NOT NULL", (imp_id,))}
    all_ref_docs = {r['ref']: dict(r) for r in db.execute(
        "SELECT ref, client_ref FROM gaia_docs WHERE import_id=? AND ref IS NOT NULL", (imp_id,))}
    now = datetime.datetime.utcnow().isoformat()
    linked = skipped = unmatched = 0
    for fname in os.listdir(SPECS_DIR):
        if not fname.lower().endswith('.pdf'):
            continue
        fpath = os.path.join(SPECS_DIR, fname)
        doc = None
        m = re.search(r'(CWLNG[-_]\w+[-_]\w+[-_]\w+[-_]\w+[-_]\d+)', fname, re.IGNORECASE)
        if m:
            doc = all_docs.get(m.group(1).replace('_', '-').upper())
        else:
            m2 = re.search(r'(\d{6}[A-Z]-\d{3}-[A-Z]+-\d{4}-\d{4})', fname, re.IGNORECASE)
            if m2:
                doc = all_ref_docs.get(m2.group(1).upper())
        if not doc:
            unmatched += 1; continue
        ref = doc['ref']
        existing = db.execute(
            "SELECT id FROM gaia_doc_files WHERE ref=? AND original_name=?", (ref, fname)
        ).fetchone()
        if existing:
            skipped += 1; continue
        fsize = os.path.getsize(fpath)
        db.execute(
            "INSERT INTO gaia_doc_files (ref, filename, original_name, file_path, file_size, uploaded_at, source) "
            "VALUES (?,?,?,?,?,?,?)",
            (ref, fname, fname, fpath, fsize, now, 'spec')
        )
        linked += 1
    db.commit(); db.close()
    _log(f"[GAIA] Link specs: {linked} linked, {skipped} already linked, {unmatched} unmatched")
    return jsonify({'ok': True, 'linked': linked, 'skipped': skipped, 'unmatched': unmatched})
