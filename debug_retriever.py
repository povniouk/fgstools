import json, sys, os
sys.path.insert(0, os.path.expanduser("~/spec-qa"))
os.environ["OLLAMA_URL"] = "http://192.168.8.200:11434"
os.environ["SPECS_DIR"] = os.path.expanduser("~/spec-qa/specs")

from retriever import _SYNONYMS, _STOPWORDS, _acronym_variants, _ACRONYM_RE

question = "what are the alarms associated to H2 gas detection?"
raw_terms = [w.strip("?.,;:()").lower() for w in question.split()
             if len(w) >= 2 and w.strip("?.,;:()").lower() not in _STOPWORDS]
key_terms = set(raw_terms)
for t in raw_terms:
    if t in _SYNONYMS:
        key_terms.update(_SYNONYMS[t].split())

print("raw_terms:", raw_terms)
print("key_terms:", sorted(key_terms))

spec = os.path.expanduser(
    "~/spec-qa/specs/CWLNG-TEN-000-FPT-SPC-00001_00  FIRE AND SAFTEY SPECIFICATION BUILDINGS.pdf.chunks.json"
)
with open(spec) as f:
    chunks = json.load(f)

print("\nTable chunks:")
for i, c in enumerate(chunks):
    if not c.get("has_table"):
        continue
    text_lower = c["text"].lower()
    matches = [t for t in key_terms
               if any(v in text_lower for v in (_acronym_variants(t) if _ACRONYM_RE.match(t) else {t}))]
    print(f"  [{i}] {c['section']}: matches={matches}")
