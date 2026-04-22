from flask import Flask, request, jsonify
from sentence_transformers import CrossEncoder
import os

app = Flask(__name__)
MODEL = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-base")

print(f"Loading reranker model: {MODEL}")
model = CrossEncoder(MODEL, max_length=512)
print("Reranker ready.")


@app.route("/rerank", methods=["POST"])
def rerank():
    data = request.json
    query = data.get("query", "")
    passages = data.get("passages", [])
    if not query or not passages:
        return jsonify({"error": "query and passages required"}), 400
    pairs = [(query, p[:512]) for p in passages]
    scores = model.predict(pairs).tolist()
    return jsonify({"scores": scores})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "model": MODEL})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=11435, debug=False)
