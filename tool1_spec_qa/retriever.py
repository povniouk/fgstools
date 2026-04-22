from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

# Domain synonym map — bridges user vocabulary to spec vocabulary.
# Keys are lowercase single words; values are space-separated synonyms to append.
_SYNONYMS = {
    # Alarm terminology
    "threshold":   "setpoint set point alarm level limit",
    "thresholds":  "setpoints set points alarm levels limits",
    "setpoint":    "threshold alarm level set point",
    "setpoints":   "thresholds alarm levels set points",
    "limit":       "threshold setpoint alarm level",
    "limits":      "thresholds setpoints alarm levels",
    "level":       "threshold setpoint alarm limit",
    "levels":      "thresholds setpoints alarm limits",
    # Gas names
    "h2s":         "hydrogen sulfide",
    "co":          "carbon monoxide",
    "o2":          "oxygen deficiency",
    "nh3":         "ammonia",
    "lel":         "lower explosive limit flammable",
    "flammable":   "lel explosive gas",
    # Detection vocabulary
    "detector":    "sensor transmitter detection",
    "detectors":   "sensors transmitters detection",
    "sensor":      "detector transmitter",
    "sensors":     "detectors transmitters",
    "alarm":       "alert warning setpoint threshold",
    "warning":     "alarm alert high",
    "voting":      "logic 1oo2 2oo3 1oo3",
    # Action vocabulary
    "shutdown":    "trip sil interlock",
    "trip":        "shutdown interlock",
    "interlock":   "shutdown trip sil",
}


def expand_query(question):
    """Append synonym terms to bridge vocabulary gaps between user language and spec language."""
    words = question.lower().split()
    extra = []
    for word in words:
        clean = word.strip("?.,;:()")
        if clean in _SYNONYMS:
            extra.extend(_SYNONYMS[clean].split())
    if extra:
        return question + " " + " ".join(extra)
    return question


class SpecIndex:
    def __init__(self):
        self.chunks = []
        self.vectorizer = None
        self.matrix = None
        self.cache_key = None

    def build(self, chunks, cache_key):
        self.chunks = chunks
        self.cache_key = cache_key
        texts = [c["text"] for c in chunks]
        self.vectorizer = TfidfVectorizer(
            strip_accents="unicode",
            lowercase=True,
            ngram_range=(1, 2),
            min_df=1,
        )
        self.matrix = self.vectorizer.fit_transform(texts)

    def query(self, question, top_k=4):
        if self.vectorizer is None or not self.chunks:
            return []

        expanded = expand_query(question)
        q_vec = self.vectorizer.transform([expanded])
        scores = cosine_similarity(q_vec, self.matrix).flatten()
        tfidf_hits = set(int(i) for i in np.argsort(scores)[::-1][:top_k] if scores[i] > 0)

        # Keyword fallback: force-include any chunk containing all key terms
        key_terms = [w.strip("?.,;:()").lower() for w in question.split() if len(w) > 2]
        keyword_hits = set()
        if key_terms:
            for i, chunk in enumerate(self.chunks):
                if i not in tfidf_hits:
                    text_lower = chunk["text"].lower()
                    if all(term in text_lower for term in key_terms):
                        keyword_hits.add(i)

        # TF-IDF results first, keyword fallbacks appended
        ordered = list(tfidf_hits) + list(keyword_hits)
        seen = set()
        results = []
        for i in ordered:
            if i not in seen:
                seen.add(i)
                results.append(self.chunks[i])
        return results


spec_index = SpecIndex()


def find_relevant_chunks(question, chunks, cache_key, top_k=4):
    if spec_index.cache_key != cache_key:
        spec_index.build(chunks, cache_key)
    return spec_index.query(question, top_k=top_k)
