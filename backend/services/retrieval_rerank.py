import os
import json
from typing import List, Dict
from sentence_transformers import SentenceTransformer, CrossEncoder
from elasticsearch import Elasticsearch, exceptions as es_exceptions

# ──────────────────────────────
# Configurações e conexões
# ──────────────────────────────
ES_HOST = os.getenv("ELASTICSEARCH_HOST", "http://localhost:9200")
INDEX_NAME = os.getenv("ES_INDEX", "sentencas_rag")

es = Elasticsearch(
    ES_HOST,
    headers={"Accept": "application/vnd.elasticsearch+json; compatible-with=8"}
)

# ──────────────────────────────
# Instânciações lazy dos modelos
# ──────────────────────────────
_embed_model: SentenceTransformer | None = None
_cross_encoder: CrossEncoder | None = None

def get_embed_model() -> SentenceTransformer:
    global _embed_model
    if _embed_model is None:
        model_name = os.getenv(
            "EMBEDDING_MODEL",
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        )
        _embed_model = SentenceTransformer(model_name)
    return _embed_model

def get_cross_encoder() -> CrossEncoder:
    global _cross_encoder
    if _cross_encoder is None:
        rerank_model = os.getenv(
            "RERANK_MODEL",
            "cross-encoder/ms-marco-MiniLM-L-6-v2"
        )
        _cross_encoder = CrossEncoder(rerank_model)
    return _cross_encoder

# ──────────────────────────────
# Função principal de recuperação
# ──────────────────────────────
def recuperar_documentos_similares(
    query: str,
    top_k: int = 10,
    rerank_top_k: int = 5
) -> List[Dict]:
    """
    Realiza busca vetorial no Elasticsearch com re-ranking por cross-encoder.

    Retorna uma lista de dicionários com os campos:
    - id
    - relatorio
    - fundamentacao
    - dispositivo
    - score
    - rerank_score
    """
    try:
        # 1) Gera embedding
        embedder = get_embed_model()
        query_vec = embedder.encode(query).tolist()

        # 2) Busca bruta via KNN no Elasticsearch
        response = es.search(
            index=INDEX_NAME,
            body={
                "size": top_k,
                "knn": {
                    "field": "embedding",
                    "query_vector": query_vec,
                    "k": top_k,
                    "num_candidates": top_k * 2
                }
            }
        )

        # 3) Monta lista de docs
        docs: List[Dict] = []
        for hit in response.get("hits", {}).get("hits", []):
            src = hit["_source"]
            docs.append({
                "id": hit["_id"],
                "relatorio": src.get("relatorio", ""),
                "fundamentacao": src.get("fundamentacao", ""),
                "dispositivo": src.get("dispositivo", ""),
                "score": float(hit.get("_score", 0.0))
            })

        if not docs:
            return []

        # 4) Re-ranking com CrossEncoder
        reranker = get_cross_encoder()
        pairs = [(query, doc["relatorio"]) for doc in docs]
        rerank_scores = reranker.predict(pairs)

        for doc, score in zip(docs, rerank_scores):
            doc["rerank_score"] = float(score)

        # 5) Ordena e retorna apenas os top rerankados
        docs.sort(key=lambda x: x["rerank_score"], reverse=True)
        return docs[:rerank_top_k]

    except es_exceptions.ElasticsearchException as e:
        print(f"Erro na conexão com Elasticsearch: {e}")
        return []


# ──────────────────────────────
# Execução local para teste
# ──────────────────────────────
if __name__ == "__main__":
    path = os.getenv("RELATORIO_PATH", "relatorio.txt")
    if not os.path.isfile(path):
        print(f"Arquivo '{path}' não encontrado.")
        exit(1)

    with open(path, "r", encoding="utf-8") as f:
        query = f.read().strip()

    print(f"→ Query carregada de '{path}' ({len(query)} caracteres)\n")
    resultados = recuperar_documentos_similares(query)

    print("=== Resultados Top-Rerank ===")
    print(json.dumps(resultados, ensure_ascii=False, indent=2))
