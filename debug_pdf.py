import sys
import pdfplumber

path = sys.argv[1]
page_num = int(sys.argv[2]) - 1  # convert to 0-indexed

with pdfplumber.open(path) as pdf:
    total = len(pdf.pages)
    print(f"Total pages: {total}")
    if page_num >= total:
        print(f"Page {page_num+1} out of range.")
        sys.exit(1)

    page = pdf.pages[page_num]
    print(f"\n{'='*60}")
    print(f"PAGE {page_num+1} — size: {page.width:.0f} x {page.height:.0f} pts")
    print(f"{'='*60}")

    # Raw text
    text = page.extract_text() or ""
    print(f"\n--- extract_text() [{len(text)} chars] ---")
    print(text[:2000] if text else "(empty)")

    # Words
    words = page.extract_words()
    print(f"\n--- extract_words() [{len(words)} words found] ---")

    # Tables
    tables = page.extract_tables()
    print(f"\n--- extract_tables() [{len(tables)} table(s) found] ---")
    for i, table in enumerate(tables):
        print(f"\n  Table {i+1} ({len(table)} rows):")
        for row in table:
            cleaned = [str(c).strip() if c else "" for c in row]
            print("  | " + " | ".join(cleaned) + " |")

    # Images / bitmaps on the page
    images = page.images
    print(f"\n--- Images on page: {len(images)} ---")
    for img in images:
        print(f"  bbox={img.get('x0',0):.0f},{img.get('y0',0):.0f} → {img.get('x1',0):.0f},{img.get('y1',0):.0f}  name={img.get('name','?')}")
