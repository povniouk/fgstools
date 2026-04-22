import pdfplumber
import re


def load_pdf_chunks(path, chunk_size=700, overlap=150):
    """Chunk across page boundaries so sections aren't fragmented mid-thought."""
    words = []           # flat list of (word, page, section_at_word)
    current_section = "Unknown"

    with pdfplumber.open(path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            if not text:
                continue
            for line in text.split("\n"):
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
