import os
import traceback
import json
from typing import List, Dict
from sentence_transformers import CrossEncoder
from elasticsearch import Elasticsearch, exceptions as es_exceptions
from preprocessing.sentence_indexing_rag import ElasticsearchSetup

# ───────────────────────────────────────────────────
# Configurações e conexões
# ───────────────────────────────────────────────────
ES_HOST = os.getenv("ELASTICSEARCH_HOST", "http://localhost:9200")
INDEX_NAME = os.getenv("ES_INDEX", "sentencas_rag")

es = Elasticsearch(
    ES_HOST,
    headers={"Accept": "application/vnd.elasticsearch+json; compatible-with=8"}
)

# ───────────────────────────────────────────────────
# Instância lazy do CrossEncoder
# ───────────────────────────────────────────────────
_cross_encoder: CrossEncoder | None = None

def get_cross_encoder() -> CrossEncoder:
    global _cross_encoder
    if _cross_encoder is None:
        rerank_model = os.getenv(
            "RERANK_MODEL",
            "cross-encoder/ms-marco-MiniLM-L-6-v2"
        )
        _cross_encoder = CrossEncoder(rerank_model)
    return _cross_encoder

# ───────────────────────────────────────────────────
# Função principal de recuperação
# ───────────────────────────────────────────────────

def recuperar_documentos_similares(
    query: str,
    top_k: int = 10,
    rerank_top_k: int = 5
) -> List[Dict]:
    """
    Executa KNN no Elasticsearch (campo 'embedding' com 3072 dimensões)
    e depois re-rank via CrossEncoder. Retorna lista de dicionários:
      cada dicionário tem: id, relatorio, fundamentacao, dispositivo, score_es, score_rerank.
    """

    try:
        # 1) Cria embedding de 3072 dims usando OpenAI (via ElasticsearchSetup já configurado)
        es_setup = ElasticsearchSetup()
        es_client = es_setup.es
        query_vec = es_setup.create_openai_embedding(query)

        # 2) Monta o body da query KNN para o ES
        knn_body = {
            "size": top_k,
            "knn": {
                "field":        "embedding",
                "query_vector": query_vec,
                "k":            top_k
                # "num_candidates": top_k * 2  # opcional
            }
        }

        # 3) Executa pesquisa KNN no índice
        try:
            response = es_client.search(index=INDEX_NAME, body=knn_body)
            hits_list = response.get("hits", {}).get("hits", [])
            if not hits_list:
                return []
            hits = hits_list

        except es_exceptions.TransportError:
            # captura erros de transporte/comunicação com o ES
            traceback.print_exc()
            return []
        except Exception:
            # qualquer outra exceção na consulta
            traceback.print_exc()
            return []

        # 4) Monta lista de candidatos, extraindo "relatorio" / "fundamentacao" / "dispositivo"
        candidatos: List[Dict] = []
        for h in hits:
            src = h.get("_source", {})

            # Aqui suponho que seu índice tem campos "relatorio", "fundamentacao" e "dispositivo".
            # Caso o nome seja diferente, ajuste para o campo correto.
            candidatos.append({
                "id":            h.get("_id", ""),
                "relatorio":     src.get("relatorio", ""),
                "fundamentacao": src.get("fundamentacao", ""),
                "dispositivo":   src.get("dispositivo", ""),
                "score_es":      float(h.get("_score", 0.0)),
            })

        if not candidatos:
            return []

        # 5) Re-rank via CrossEncoder (usa apenas query + relatorio)
        reranker = get_cross_encoder()
        pares = [(query, c["relatorio"]) for c in candidatos]
        try:
            rerank_scores = reranker.predict(pares)
        except Exception:
            traceback.print_exc()
            return []

        # 6) Anexa pontuação de rerank e ordena
        for c, score in zip(candidatos, rerank_scores):
            c["score_rerank"] = float(score)
        candidatos.sort(key=lambda x: x["score_rerank"], reverse=True)

        # Retorna os top rerankados
        return candidatos[:rerank_top_k]

    except Exception:
        traceback.print_exc()
        return []

# ───────────────────────────────────────────────────
# Execução local para teste (opcional)
# ───────────────────────────────────────────────────
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
