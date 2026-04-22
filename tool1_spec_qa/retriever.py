from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np


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
        q_vec = self.vectorizer.transform([question])
        scores = cosine_similarity(q_vec, self.matrix).flatten()
        top_indices = np.argsort(scores)[::-1][:top_k]
        return [self.chunks[i] for i in top_indices if scores[i] > 0]


spec_index = SpecIndex()


def find_relevant_chunks(question, chunks, cache_key, top_k=4):
    if spec_index.cache_key != cache_key:
        spec_index.build(chunks, cache_key)
    return spec_index.query(question, top_k=top_k)
