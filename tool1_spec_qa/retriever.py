import os
import re
import glob
import hashlib
import numpy as np
import requests
from rank_bm25 import BM25Okapi
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity as sk_cosine

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
SPECS_DIR = os.environ.get("SPECS_DIR", "specs")
_EMBED_MODEL = "nomic-embed-text"

# App sets this to log_info so embedding progress appears in the browser log panel
_log = print

# Synonym expansion — still useful for BM25 exact-term bridging.
# Embeddings now handle semantic similarity; this covers acronyms/abbreviations.
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
    "temperature": "thermal heat degree fahrenheit celsius",
    "temp":        "temperature thermal heat",
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

# Common English stopwords and question framing words — excluded from keyword fallback
_STOPWORDS = {
    "what", "are", "the", "is", "for", "to", "of", "in", "a", "an", "and",
    "or", "with", "on", "at", "by", "from", "this", "that", "these", "those",
    "associated", "regarding", "concerning", "related", "about", "how", "when",
    "where", "which", "who", "does", "do", "be", "been", "being", "have",
    "has", "had", "can", "could", "will", "would", "should", "shall", "may",
    "might", "its", "their", "any", "all", "each", "per", "as", "if",
    # Question framing words
    "tell", "me", "you", "give", "explain", "describe", "show", "list",
    "know", "find", "get", "provide", "information", "details", "specify",
}


def _acronym_variants(term):
    variants = {term}
    swaps = {"0": "o", "o": "0", "1": "i", "i": "1", "2": "z", "z": "2", "5": "s", "s": "5"}
    for ch, alt in swaps.items():
        if ch in term:
            variants.add(term.replace(ch, alt))
    return variants


def expand_query(question):
    words = question.lower().split()
    extra = []
    for word in words:
        clean = word.strip("?.,;:()")
        if clean in _SYNONYMS:
            extra.extend(_SYNONYMS[clean].split())
    return (question + " " + " ".join(extra)) if extra else question


def _tokenize(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def _cache_hash(cache_key):
    return hashlib.md5(str(cache_key).encode()).hexdigest()[:12]


def _get_embedding(text):
    """Call Ollama embeddings API. Returns float32 numpy vector."""
    resp = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": _EMBED_MODEL, "prompt": text[:2000]},
        timeout=30,
    )
    resp.raise_for_status()
    return np.array(resp.json()["embedding"], dtype=np.float32)


def _build_embedding_matrix(chunks, cache_key):
    """
    Return embedding matrix (n_chunks × dim). Computes once and caches to disk.
    Cache is keyed by hash of cache_key — invalidated automatically when specs change.
    Falls back gracefully if nomic-embed-text is not available.
    """
    h = _cache_hash(cache_key)
    cache_path = os.path.join(SPECS_DIR, f"_embeddings_{h}.npy")

    if os.path.exists(cache_path):
        _log(f"[retriever] Loading cached embeddings ({h})")
        return np.load(cache_path)

    # Remove stale embedding caches
    for old in glob.glob(os.path.join(SPECS_DIR, "_embeddings_*.npy")):
        try:
            os.remove(old)
        except OSError:
            pass

    _log(f"[retriever] Building embedding index ({len(chunks)} chunks) — first time only...")
    embeddings = []
    for i, chunk in enumerate(chunks):
        emb = _get_embedding(chunk["text"])
        embeddings.append(emb)
        if (i + 1) % 10 == 0 or i == len(chunks) - 1:
            _log(f"[retriever] Embedded {i + 1}/{len(chunks)}")

    matrix = np.stack(embeddings)
    np.save(cache_path, matrix)
    _log(f"[retriever] Embedding cache saved ({cache_path})")
    return matrix


class SpecIndex:
    def __init__(self):
        self.chunks = []
        self.cache_key = None
        self.bm25 = None
        self.tfidf_vectorizer = None
        self.tfidf_matrix = None
        self.embed_matrix = None    # shape (n_chunks, embed_dim), None if unavailable
        self.use_embeddings = True

    def build(self, chunks, cache_key):
        self.chunks = chunks
        self.cache_key = cache_key
        texts = [c["text"] for c in chunks]

        # BM25
        self.bm25 = BM25Okapi([_tokenize(t) for t in texts])

        # TF-IDF (fast ngram fallback + complements BM25 for multi-word phrases)
        self.tfidf_vectorizer = TfidfVectorizer(
            strip_accents="unicode", lowercase=True, ngram_range=(1, 2), min_df=1
        )
        self.tfidf_matrix = self.tfidf_vectorizer.fit_transform(texts)

        # Semantic embeddings — graceful degradation if model unavailable
        if self.use_embeddings:
            try:
                self.embed_matrix = _build_embedding_matrix(chunks, cache_key)
            except Exception as e:
                _log(f"[retriever] Embeddings unavailable ({e}) — using BM25+TF-IDF only")
                self.embed_matrix = None
                self.use_embeddings = False

    def query(self, question, top_k=8):
        if not self.chunks or self.bm25 is None:
            return []

        expanded = expand_query(question)

        # Score with each method
        bm25_scores = np.array(self.bm25.get_scores(_tokenize(expanded)))

        q_vec = self.tfidf_vectorizer.transform([expanded])
        tfidf_scores = sk_cosine(q_vec, self.tfidf_matrix).flatten()

        embed_scores = None
        if self.embed_matrix is not None:
            try:
                q_emb = _get_embedding(question)
                q_norm = q_emb / (np.linalg.norm(q_emb) + 1e-8)
                norms = np.linalg.norm(self.embed_matrix, axis=1, keepdims=True)
                normed = self.embed_matrix / (norms + 1e-8)
                embed_scores = normed @ q_norm
            except Exception:
                embed_scores = None

        # Reciprocal Rank Fusion
        def rrf(scores, k=60):
            order = np.argsort(scores)[::-1]
            ranks = np.empty_like(order)
            ranks[order] = np.arange(len(order))
            return 1.0 / (k + ranks)

        combined = rrf(bm25_scores) + rrf(tfidf_scores)
        if embed_scores is not None:
            combined += rrf(embed_scores)

        # Table chunks are atomic and high-value — boost their score
        for i, chunk in enumerate(self.chunks):
            if chunk.get("has_table"):
                combined[i] *= 2.0

        top_indices = set(int(i) for i in np.argsort(combined)[::-1][:top_k])

        # Content key terms — strip stopwords; keep even 2-char terms (H2, CO, O2)
        raw_terms = [
            w.strip("?.,;:()").lower() for w in question.split()
            if len(w) >= 2 and w.strip("?.,;:()").lower() not in _STOPWORDS
        ]
        # Expand through synonyms so "h2" also checks for "hydrogen"
        key_terms = set(raw_terms)
        for t in raw_terms:
            if t in _SYNONYMS:
                key_terms.update(_SYNONYMS[t].split())

        # Keyword fallback — different rules for table vs prose chunks:
        # Table chunks are atomic/authoritative: include if ANY key term matches.
        # Prose chunks: require ALL key terms (stricter, avoids noise).
        def term_in(t, text):
            """Check t OR any of its synonyms against text."""
            variants = _acronym_variants(t) if _ACRONYM_RE.match(t) else {t}
            if any(v in text for v in variants):
                return True
            # Also check synonyms of t
            for syn in _SYNONYMS.get(t, "").split():
                if syn in text:
                    return True
            return False

        table_fallback = []
        prose_fallback = []

        for i, chunk in enumerate(self.chunks):
            if i in top_indices:
                continue
            text_lower = chunk["text"].lower()
            if chunk.get("has_table"):
                match_count = sum(1 for t in raw_terms if term_in(t, text_lower))
                if match_count >= 2:
                    table_fallback.append(i)
            else:
                if raw_terms and all(term_in(t, text_lower) for t in raw_terms):
                    prose_fallback.append(i)

        # Table and prose fallbacks are capped independently so a prose chunk
        # containing the exact answer can't be displaced by table chunks
        table_fallback.sort(key=lambda i: combined[i], reverse=True)
        prose_fallback.sort(key=lambda i: combined[i], reverse=True)
        for i in table_fallback[:2]:
            top_indices.add(i)
        for i in prose_fallback[:2]:
            top_indices.add(i)

        ordered = sorted(top_indices, key=lambda i: combined[i], reverse=True)
        return [self.chunks[i] for i in ordered]


spec_index = SpecIndex()

# --- Re-ranker ---
# CrossEncoder re-scores (question, chunk) pairs after first-pass retrieval.
# Much more accurate than vector similarity because it reads both together.
_reranker = None
_reranker_available = True

def _load_reranker():
    global _reranker, _reranker_available
    if not _reranker_available:
        return None
    if _reranker is not None:
        return _reranker
    try:
        from sentence_transformers import CrossEncoder
        _log("Loading re-ranker (BAAI/bge-reranker-base) — first time downloads ~270MB...")
        _reranker = CrossEncoder("BAAI/bge-reranker-base", max_length=512)
        _log("Re-ranker ready.")
    except Exception as e:
        _log(f"Re-ranker unavailable ({e}) — using retrieval order only.")
        _reranker_available = False
    return _reranker


def _rerank(question, candidates, top_n=5):
    """Re-score candidates with cross-encoder and return top_n."""
    reranker = _load_reranker()
    if reranker is None or not candidates:
        return candidates[:top_n]
    try:
        pairs = [(question, c["text"][:512]) for c in candidates]
        scores = reranker.predict(pairs)
        ranked = sorted(zip(scores, candidates), key=lambda x: x[0], reverse=True)
        return [c for _, c in ranked[:top_n]]
    except Exception as e:
        _log(f"Re-ranking failed ({e}) — using retrieval order.")
        return candidates[:top_n]


def find_relevant_chunks(question, chunks, cache_key, top_k=6):
    if spec_index.cache_key != cache_key:
        spec_index.build(chunks, cache_key)
    # First pass: cast wide net with BM25+TF-IDF+embeddings
    candidates = spec_index.query(question, top_k=top_k)
    # Second pass: re-rank with cross-encoder, return top 5
    return _rerank(question, candidates, top_n=5)
