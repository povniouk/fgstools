import pdfplumber
import re

# Patterns that match recurring header/footer boilerplate in CWLNG spec PDFs
_BOILERPLATE = re.compile(
    r"(CUI/CEII\s*[-—]|"
    r"CONTAINS CRITICAL ENERGY|"
    r"DO NOT RELEASE|"
    r"Project\s+N[°o]\s+Unit\s+Doc|"
    r"078051C\s+000|"
    r"Client\s+Doc\.\s+No\.|"
    r"Client\s+Commonwealth|"
    r"Project\s+Commonwealth|"
    r"Location\s+Cameron|"
    r"FIRE AND SAF[ET]+Y SPECIFICATION|"
    r"Confidential\s*[–-]\s*Do Not Disclose|"
    r"Copyright\s+Technip|"
    r"Technip Energies USA|"
    r"LAPELS\s+Firm\s+Reg|"
    r"All Rights Reserved|"
    r"\d{6}[A-Z]\s+\d{3}\s+[A-Z]+\s+\d{4})"
, re.IGNORECASE)

_SECTION_RE = re.compile(r"^(\d+(\.\d+)*)\s+[A-Z]")


def strip_boilerplate(text):
    lines = text.split("\n")
    return "\n".join(ln for ln in lines if not _BOILERPLATE.search(ln))


def table_to_bullets(rows):
    """Convert a table to bullet points — more reliably parsed by small LLMs than pipe tables."""
    if not rows:
        return ""
    cleaned = [[str(c).strip() if c is not None else "" for c in row] for row in rows]
    lines = []
    for row in cleaned:
        cells = [c for c in row if c]
        if cells:
            lines.append("• " + " — ".join(cells))
    return "\n".join(lines)


def extract_page_segments(page):
    """Return ordered list of {type, text} dicts for the page, tables kept atomic."""
    table_objects = page.find_tables()

    if not table_objects:
        text = strip_boilerplate(page.extract_text() or "")
        return [{"type": "text", "text": text}] if text.strip() else []

    table_objects = sorted(table_objects, key=lambda t: t.bbox[1])
    segments = []
    prev_bottom = 0

    for t in table_objects:
        x0, top, x1, bottom = t.bbox

        if top > prev_bottom + 2:
            region = page.crop((0, prev_bottom, page.width, top))
            txt = strip_boilerplate(region.extract_text() or "")
            if txt.strip():
                segments.append({"type": "text", "text": txt.strip()})

        rows = t.extract()
        if rows:
            bullets = table_to_bullets(rows)
            if bullets:
                segments.append({"type": "table", "text": bullets})

        prev_bottom = bottom

    if prev_bottom < page.height - 2:
        region = page.crop((0, prev_bottom, page.width, page.height))
        txt = strip_boilerplate(region.extract_text() or "")
        if txt.strip():
            segments.append({"type": "text", "text": txt.strip()})

    return segments


def load_pdf_chunks(path, chunk_size=700, overlap=150):
    """
    Tables are emitted as atomic chunks with the preceding prose lines as
    context prefix (so the table knows what it describes). Prose is
    word-chunked with overlap between tables, never splitting a table.
    """
    chunks = []
    current_section = "Unknown"
    prose_words = []        # (word, page_num, section)
    last_prose_lines = []   # context injected as prefix into next table chunk

    def flush_prose():
        if not prose_words:
            return
        for i in range(0, len(prose_words), chunk_size - overlap):
            window = prose_words[i: i + chunk_size]
            if not window:
                break
            text = " ".join(w for w, _, _ in window)
            if len(text.strip()) < 50:
                continue
            chunks.append({
                "text": text,
                "section": window[0][2],
                "page": window[0][1],
                "has_table": False,
            })
        prose_words.clear()

    with pdfplumber.open(path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            for seg in extract_page_segments(page):

                if seg["type"] == "text":
                    text = seg["text"]
                    lines = [ln.strip() for ln in text.split("\n")]
                    for line in lines:
                        if _SECTION_RE.match(line):
                            current_section = line[:80]
                        for w in line.split():
                            prose_words.append((w, page_num, current_section))
                    # Keep last 4 non-empty lines as context for the next table
                    last_prose_lines = [ln for ln in lines if ln][-4:]

                elif seg["type"] == "table":
                    flush_prose()
                    # Prefix the table with its immediately preceding prose context
                    # so the chunk is self-contained: heading + table data together
                    prefix = " ".join(last_prose_lines)
                    table_text = (prefix + "\n\n" + seg["text"]) if prefix else seg["text"]
                    if len(table_text.strip()) >= 20:
                        chunks.append({
                            "text": table_text,
                            "section": current_section,
                            "page": page_num,
                            "has_table": True,
                        })

    flush_prose()
    return chunks
