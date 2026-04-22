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
    r"\d{6}[A-Z]\s+\d{3}\s+[A-Z]+\s+\d{4})"  # project code lines
, re.IGNORECASE)


def strip_boilerplate(text):
    """Remove recurring header/footer lines from spec page text."""
    lines = text.split("\n")
    cleaned = [ln for ln in lines if not _BOILERPLATE.search(ln)]
    return "\n".join(cleaned)


def table_to_markdown(rows):
    if not rows:
        return ""
    cleaned = [[str(c).strip() if c is not None else "" for c in row] for row in rows]
    col_count = max(len(row) for row in cleaned)
    padded = [row + [""] * (col_count - len(row)) for row in cleaned]
    header = padded[0]
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join(["---"] * col_count) + " |")
    for row in padded[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def extract_page_content(page):
    """Build page content top-to-bottom: text sections interleaved with
    Markdown tables at their original inline position."""
    table_objects = page.find_tables()

    if not table_objects:
        return strip_boilerplate(page.extract_text() or "")

    # Sort tables top to bottom by their y position
    table_objects = sorted(table_objects, key=lambda t: t.bbox[1])

    parts = []
    prev_bottom = 0

    for t in table_objects:
        x0, top, x1, bottom = t.bbox

        # Text above this table
        if top > prev_bottom + 2:
            region = page.crop((0, prev_bottom, page.width, top))
            txt = region.extract_text()
            if txt and txt.strip():
                parts.append(txt.strip())

        # Table as Markdown
        rows = t.extract()
        if rows:
            md = table_to_markdown(rows)
            if md:
                parts.append(md)

        prev_bottom = bottom

    # Text below last table
    if prev_bottom < page.height - 2:
        region = page.crop((0, prev_bottom, page.width, page.height))
        txt = region.extract_text()
        if txt and txt.strip():
            parts.append(txt.strip())

    return strip_boilerplate("\n\n".join(parts))


def load_pdf_chunks(path, chunk_size=700, overlap=150):
    """Chunk across page boundaries; tables rendered as Markdown inline."""
    words = []
    current_section = "Unknown"

    with pdfplumber.open(path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            content = extract_page_content(page)
            if not content:
                continue
            for line in content.split("\n"):
                stripped = line.strip()
                if re.match(r"^(\d+(\.\d+)*)\s+[A-Z]", stripped):
                    current_section = stripped[:80]
                for w in line.split():
                    words.append((w, page_num, current_section))

    chunks = []
    for i in range(0, len(words), chunk_size - overlap):
        window = words[i: i + chunk_size]
        if not window:
            break
        chunk_text = " ".join(w for w, _, _ in window)
        if len(chunk_text.strip()) < 50:
            continue
        chunks.append({
            "text": chunk_text,
            "section": window[0][2],
            "page": window[0][1],
        })

    return chunks
