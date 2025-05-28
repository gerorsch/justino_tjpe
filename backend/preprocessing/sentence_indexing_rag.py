import pandas as pd
import re
import os
import time
from sentence_transformers import SentenceTransformer
from elasticsearch import Elasticsearch

# Pega da variável de ambiente (definida no docker-compose) ou cai em 'http://localhost:9200' se estiver rodando local
ES_HOST = os.getenv("ELASTICSEARCH_HOST", "http://elasticsearch:9200")
print("→ Conectando no Elasticsearch em", ES_HOST)
# Conecta ao ES correto
es = Elasticsearch(hosts=[ES_HOST])

# 2) Aguarda ES ficar pronto
for _ in range(10):
    if es.ping():
        print("✅ ES ping OK")
        break
    print("⏳ esperando ES...")
    time.sleep(2)
else:
    print("❌ ES não respondeu")
    exit(1)

# ────────────────────────────────────────────────
# Função para separar a sentença em três partes
# ────────────────────────────────────────────────
def separar_partes_sentenca(texto: str):
    texto = texto.strip()
    padrao_fundamentacao = r"\b(passo a decidir|decido|passo à decisão)\b"
    padrao_dispositivo = r"\b(julgo|ante o exposto|extingo|declaro)\b"

    texto_lower = texto.lower()
    idx_fund = re.search(padrao_fundamentacao, texto_lower)
    idx_disp = re.search(padrao_dispositivo, texto_lower)

    i_fund = idx_fund.start() if idx_fund else None
    i_disp = idx_disp.start() if idx_disp else None

    relatorio = texto[:i_fund].strip() if i_fund else texto
    fundamentacao = texto[i_fund:i_disp].strip() if i_fund and i_disp else ""
    dispositivo = texto[i_disp:].strip() if i_disp else ""

    return pd.Series({
        "relatorio": relatorio,
        "fundamentacao": fundamentacao,
        "dispositivo": dispositivo
    })

# ────────────────────────────────────────────────
# Carrega os dados e separa as partes
# ────────────────────────────────────────────────
df = pd.read_csv("data/sentencas.csv")
df.fillna("", inplace=True)    # <— adicionado aqui
partes = df["julgado"].apply(separar_partes_sentenca)
df = pd.concat([df, partes], axis=1)

# ────────────────────────────────────────────────
# Inicia modelo de embeddings
# ────────────────────────────────────────────────
model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")

# ────────────────────────────────────────────────
# Conecta ao Elasticsearch
# ────────────────────────────────────────────────
# es = Elasticsearch("http://localhost:9200")
index_name = "sentencas_rag"

# Criação do índice com logs e tratamento de erro
print("→ Verificando existência do índice...")
try:
    if not es.indices.exists(index=index_name):
        print(f"→ Criando índice '{index_name}' no Elasticsearch...")
        es.indices.create(
            index=index_name,
            body={
                "mappings": {
                    "properties": {
                        "relatorio": {"type": "text"},
                        "fundamentacao": {"type": "text"},
                        "dispositivo": {"type": "text"},
                        "embedding": {
                            "type": "dense_vector",
                            "dims": 384,
                            "index": True,
                            "similarity": "cosine"
                        },
                        "classe": {"type": "keyword"},
                        "assunto": {"type": "keyword"},
                        "magistrado": {"type": "keyword"},
                        "processo": {"type": "keyword"}
                    }
                }
            }
        )
        print(f"✅ Índice '{index_name}' criado com sucesso.")
except Exception as e:
    print("❌ Erro ao criar índice:", e)

# ────────────────────────────────────────────────
# Indexa os dados com embedding do relatório
# ────────────────────────────────────────────────
for i, row in df.iterrows():
    texto = row["relatorio"]
    if not isinstance(texto, str) or not texto.strip():
        continue  # pula vazios

    emb = model.encode(texto).tolist()
    doc = {
        "relatorio": texto,
        "fundamentacao": row.get("fundamentacao", "") or "",
        "dispositivo": row.get("dispositivo", "") or "",
        "embedding": emb,
        "classe": row.get("classe", ""),
        "assunto": row.get("assunto", ""),
        "magistrado": row.get("magistrado", ""),
        "processo": str(row.get("processo", ""))
    }

    es.index(index=index_name, id=f"{i}", document=doc)

print("Indexação concluída.")

