import re
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from rank_bm25 import BM25Okapi

# Domain synonym map — bridges user vocabulary to spec vocabulary.
_SYNONYMS = {
    "threshold":   "setpoint set point alarm level limit",
    "thresholds":  "setpoints set points alarm levels limits",
    "setpoint":    "threshold alarm level set point",
    "setpoints":   "thresholds alarm levels set points",
    "limit":       "threshold setpoint alarm level",
    "limits":      "thresholds setpoints alarm levels",
    "level":       "threshold setpoint alarm limit",
    "levels":      "thresholds setpoints alarm limits",
    "h2s":         "hydrogen sulfide",
    "h2":          "hydrogen catalytic bead",
    "hydrogen":    "h2 catalytic bead",
    "co":          "carbon monoxide",
    "carbon":      "co monoxide",
    "o2":          "oxygen deficiency",
    "oxygen":      "o2 deficiency",
    "nh3":         "ammonia",
    "ammonia":     "nh3",
    "lel":         "lower explosive limit flammable",
    "flammable":   "lel explosive gas",
    "detector":    "sensor transmitter detection",
    "detectors":   "sensors transmitters detection",
    "sensor":      "detector transmitter",
    "sensors":     "detectors transmitters",
    "alarm":       "alert warning setpoint threshold",
    "warning":     "alarm alert high",
    "voting":      "logic 1oo2 2oo3 1oo3",
    "shutdown":    "trip sil interlock",
    "trip":        "shutdown interlock",
    "interlock":   "shutdown trip sil",
}

_ACRONYM_RE = re.compile(r'^[a-z0-9]{2,5}$')


def _acronym_variants(term):
    """Generate common OCR/formatting variants of a short alphanumeric term."""
    variants = {term}
    # Swap digits and letters that look alike: 0↔O, 1↔I, 2↔Z, 5↔S
    swaps = {"0": "o", "o": "0", "1": "i", "i": "1", "2": "z", "z": "2", "5": "s", "s": "5"}
    for ch, alt in swaps.items():
        if ch in term:
            variants.add(term.replace(ch, alt))
    return variants


def expand_query(question):
    """Append synonym terms to bridge vocabulary gaps."""
    words = question.lower().split()
    extra = []
    for word in words:
        clean = word.strip("?.,;:()")
        if clean in _SYNONYMS:
            extra.extend(_SYNONYMS[clean].split())
    if extra:
        return question + " " + " ".join(extra)
    return question


def _tokenize(text):
    return re.findall(r"[a-z0-9]+", text.lower())


class SpecIndex:
    def __init__(self):
        self.chunks = []
        self.cache_key = None
        self.bm25 = None
        self.tfidf_vectorizer = None
        self.tfidf_matrix = None

    def build(self, chunks, cache_key):
        self.chunks = chunks
        self.cache_key = cache_key
        texts = [c["text"] for c in chunks]

        # BM25
        tokenized = [_tokenize(t) for t in texts]
        self.bm25 = BM25Okapi(tokenized)

        # TF-IDF (kept for hybrid merge)
        self.tfidf_vectorizer = TfidfVectorizer(
            strip_accents="unicode", lowercase=True, ngram_range=(1, 2), min_df=1
        )
        self.tfidf_matrix = self.tfidf_vectorizer.fit_transform(texts)

    def query(self, question, top_k=8):
        if not self.chunks or self.bm25 is None:
            return []

        expanded = expand_query(question)

        # BM25 scores
        bm25_scores = np.array(self.bm25.get_scores(_tokenize(expanded)))

        # TF-IDF scores
        q_vec = self.tfidf_vectorizer.transform([expanded])
        tfidf_scores = cosine_similarity(q_vec, self.tfidf_matrix).flatten()

        # Reciprocal Rank Fusion — merge both ranked lists
        def rrf_ranks(scores, k=60):
            order = np.argsort(scores)[::-1]
            ranks = np.empty_like(order)
            ranks[order] = np.arange(len(order))
            return 1.0 / (k + ranks)

        combined = rrf_ranks(bm25_scores) + rrf_ranks(tfidf_scores)

        # Boost table chunks — they are atomic and high-value
        for i, chunk in enumerate(self.chunks):
            if chunk.get("has_table"):
                combined[i] *= 1.4

        top_indices = set(int(i) for i in np.argsort(combined)[::-1][:top_k])

        # Fuzzy keyword fallback — catches acronym variants (H2S / HS2 / h25)
        key_terms = [w.strip("?.,;:()").lower() for w in question.split() if len(w) > 1]
        for i, chunk in enumerate(self.chunks):
            if i in top_indices:
                continue
            text_lower = chunk["text"].lower()
            matched = True
            for term in key_terms:
                variants = _acronym_variants(term) if _ACRONYM_RE.match(term) else {term}
                if not any(v in text_lower for v in variants):
                    matched = False
                    break
            if matched:
                top_indices.add(i)

        # Return in combined-score order
        ordered = sorted(top_indices, key=lambda i: combined[i], reverse=True)
        return [self.chunks[i] for i in ordered]


spec_index = SpecIndex()


def find_relevant_chunks(question, chunks, cache_key, top_k=8):
    if spec_index.cache_key != cache_key:
        spec_index.build(chunks, cache_key)
    return spec_index.query(question, top_k=top_k)
