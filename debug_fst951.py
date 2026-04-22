import json, os, sys
sys.path.insert(0, os.path.expanduser("~/spec-qa"))
os.environ["OLLAMA_URL"] = "http://192.168.8.200:11434"
os.environ["SPECS_DIR"] = os.path.expanduser("~/spec-qa/specs")

from retriever import _SYNONYMS, _STOPWORDS, _acronym_variants, _ACRONYM_RE

question = "what is the temperature associated to the fixed thermal sensor for notifier model FST-951"

raw_terms = [w.strip("?.,;:()").lower() for w in question.split()
             if len(w) >= 2 and w.strip("?.,;:()").lower() not in _STOPWORDS]
print("raw_terms:", raw_terms)

def term_in(t, text):
    variants = _acronym_variants(t) if _ACRONYM_RE.match(t) else {t}
    if any(v in text for v in variants):
        return True
    for syn in _SYNONYMS.get(t, "").split():
        if syn in text:
            return True
    return False

spec = os.path.expanduser(
    "~/spec-qa/specs/CWLNG-TEN-000-FPT-SPC-00001_00  FIRE AND SAFTEY SPECIFICATION BUILDINGS.pdf.chunks.json"
)
with open(spec) as f:
    chunks = json.load(f)

print(f"\nChunk 31 section: {chunks[31]['section']}")
print(f"Chunk 31 text[:300]: {chunks[31]['text'][:300]}")
print("\nterm_in results for chunk 31:")
text_lower = chunks[31]["text"].lower()
for t in raw_terms:
    result = term_in(t, text_lower)
    print(f"  {t!r}: {result}")

print("\nAll prose chunks matching ALL raw_terms:")
for i, c in enumerate(chunks):
    if c.get("has_table"):
        continue
    tl = c["text"].lower()
    if all(term_in(t, tl) for t in raw_terms):
        print(f"  [{i}] {c['section']}: {c['text'][:100]}")
