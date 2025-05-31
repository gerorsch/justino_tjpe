import os
import time
import glob
import json
import re
import pandas as pd
from datetime import datetime
from elasticsearch import Elasticsearch
from openai import OpenAI
from typing import List, Dict, Optional
from dotenv import load_dotenv

load_dotenv()

class ElasticsearchSetup:
    def __init__(self):
        """
        Inicializa:
          • Cliente Elasticsearch (priorizando Elastic Cloud)
          • Cliente OpenAI
        Variáveis esperadas em produção
          ELASTIC_CLOUD_ID        id do deployment (ex.: "mydeploy:ZGZmLm…")
          ELASTICSEARCH_API_KEY   api-key gerada no Cloud
          ELASTICSEARCH_INDEX     nome do índice (default: sentencas_rag)
          -- opcionalmente --
          ELASTICSEARCH_HOST      http(s)://host:port   (em dev/local)
        """
        cloud_id = os.getenv("ELASTIC_CLOUD_ID")
        api_key  = os.getenv("ELASTICSEARCH_API_KEY")
        host     = os.getenv("ELASTICSEARCH_HOST")      # só use em dev/local
        self.index_name = os.getenv("ELASTICSEARCH_INDEX", "sentencas_rag")

        if cloud_id and api_key:
            print(f"🔌 ElasticsearchSetup → usando Elastic Cloud ({cloud_id.split(':',1)[0]})")
            self.es = Elasticsearch(
                cloud_id=cloud_id,
                api_key=api_key,
                headers={"Accept": "application/vnd.elasticsearch+json; compatible-with=8"}
            )
        elif host:
            print(f"🔌 ElasticsearchSetup → usando host explícito {host}")
            self.es = Elasticsearch(
                hosts=[host],
                headers={"Accept": "application/vnd.elasticsearch+json; compatible-with=8"},
                verify_certs=host.startswith("https")
            )
        else:
            raise RuntimeError(
                "🛑 Defina ELASTIC_CLOUD_ID + ELASTICSEARCH_API_KEY (produção) "
                "ou ELASTICSEARCH_HOST (desenvolvimento)"
            )

        # ─── OpenAI ──────────────────────────────────────────────────────────
        openai_key = os.getenv("OPENAI_API_KEY")
        if not openai_key:
            raise ValueError("❌ OPENAI_API_KEY não encontrada nas variáveis de ambiente")
        self.openai_client = OpenAI(api_key=openai_key)

        print("✅ Cliente OpenAI configurado")

    def wait_for_elasticsearch(self, max_retries: int = 30):
        """Aguarda Elasticsearch ficar disponível"""
        for i in range(max_retries):
            try:
                if self.es.ping():
                    print("✅ Elasticsearch conectado com sucesso")
                    return True
            except Exception as e:
                print(f"⏳ Aguardando Elasticsearch... ({i+1}/{max_retries}) - {e}")
                time.sleep(2)
        raise Exception("❌ Elasticsearch não ficou disponível")

    def create_openai_embedding(self, text: str) -> List[float]:
        """Cria embedding usando OpenAI text-embedding-3-large"""
        try:
            # Limitar texto para evitar erro de token limit
            text = text[:8000] if len(text) > 8000 else text

            response = self.openai_client.embeddings.create(
                model="text-embedding-3-large",
                input=text,
                encoding_format="float"
            )
            embedding = response.data[0].embedding
            print(f"✅ Embedding criado - {len(embedding)} dimensões")
            return embedding

        except Exception as e:
            print(f"❌ Erro ao criar embedding: {e}")
            # Fallback: retorna embedding zero
            return [0.0] * 3072

    def separar_partes_sentenca(self, texto: str) -> Dict[str, str]:
        """Separa sentença em relatório, fundamentação e dispositivo"""
        if not isinstance(texto, str):
            return {"relatorio": "", "fundamentacao": "", "dispositivo": ""}

        texto = texto.strip()
        padrao_fund = r"\b(passo a decidir|decido|passo à decisão)\b"
        padrao_disp = r"\b(julgo|resolvo o mérito|condeno|extingo|declaro)\b"

        low = texto.lower()
        m_fund = re.search(padrao_fund, low)
        m_disp = re.search(padrao_disp, low)

        i_fund = m_fund.start() if m_fund else None
        i_disp = m_disp.start() if m_disp else None

        rel = texto[:i_fund].strip() if i_fund else texto
        fund = texto[i_fund:i_disp].strip() if i_fund and i_disp else ""
        disp = texto[i_disp:].strip() if i_disp else ""

        return {"relatorio": rel, "fundamentacao": fund, "dispositivo": disp}

    def create_index(self):
        """Cria índice Elasticsearch com mapeamento otimizado"""
        if self.es.indices.exists(index=self.index_name):
            print(f"✅ Índice '{self.index_name}' já existe")
            return

        print(f"→ Criando índice '{self.index_name}'...")
        mapping = {
            "settings": {
                "number_of_shards":   1,
                "number_of_replicas": 0,
                "index.max_result_window": 50000
            },
            "mappings": {
                "properties": {
                    "relatorio":        {"type": "text",         "analyzer": "portuguese"},
                    "fundamentacao":    {"type": "text",         "analyzer": "portuguese"},
                    "dispositivo":      {"type": "text",         "analyzer": "portuguese"},
                    "julgado_completo": {"type": "text",         "analyzer": "portuguese"},
                    "embedding": {
                        "type":       "dense_vector",
                        "dims":       3072,
                        "index":      True,
                        "similarity": "cosine"
                    },
                    "classe":    {"type": "keyword"},
                    "assunto":   {"type": "keyword"},
                    "magistrado":{"type": "keyword"},
                    "processo":  {"type": "keyword"},
                    "created_at":{"type": "date"},
                    "source":    {"type": "keyword"}
                }
            }
        }
        self.es.indices.create(index=self.index_name, body=mapping)
        print(f"✅ Índice '{self.index_name}' criado com sucesso")

    def load_sentences_from_csv(self, csv_path: str = "data/sentencas.csv") -> Optional[pd.DataFrame]:
        """Carrega sentenças do CSV e processa"""
        if not os.path.exists(csv_path):
            print(f"⚠️ Arquivo {csv_path} não encontrado")
            return None

        print(f"→ Carregando dados de {csv_path}")
        # Força parser único para evitar mixed types warning
        df = pd.read_csv(csv_path, dtype=str, low_memory=False)
        df.fillna("", inplace=True)
        print(f"✅ {len(df)} sentenças carregadas")
        return df

    def index_sentence(self, row: pd.Series, doc_id: str) -> bool:
        """Indexa uma sentença individual"""
        try:
            # Se já existir, pula imediatamente
            if self.es.exists(index=self.index_name, id=doc_id):
                print(f"⭐ Documento {doc_id} já indexado — pulando")
                return False
        
            julgado = row.get("julgado", "")
            partes = self.separar_partes_sentenca(julgado)
            texto_embed = partes["relatorio"] or julgado[:1000]
            if not texto_embed.strip():
                print(f"⚠️ Pulando documento {doc_id} - texto vazio")
                return False

            print(f"→ Criando embedding para documento {doc_id}...")
            emb = self.create_openai_embedding(texto_embed)

            # ** Agora com timestamp ISO em vez de "now" **
            now_iso = datetime.utcnow().isoformat() + "Z"

            doc = {
                "relatorio":        partes["relatorio"],
                "fundamentacao":    partes["fundamentacao"],
                "dispositivo":      partes["dispositivo"],
                "julgado_completo": julgado,
                "embedding":        emb,
                "classe":           row.get("classe", ""),
                "assunto":          row.get("assunto", ""),
                "magistrado":       row.get("magistrado", ""),
                "processo":         str(row.get("processo", "")),
                "created_at":       now_iso,
                "source":           "csv_import"
            }

            self.es.index(index=self.index_name, id=doc_id, document=doc)
            print(f"✅ Documento {doc_id} indexado")
            return True

        except Exception as e:
            print(f"❌ Erro ao indexar documento {doc_id}: {e}")
            return False

    def get_document_count(self) -> int:
        """Retorna número de documentos no índice"""
        try:
            return self.es.count(index=self.index_name)["count"]
        except:
            return 0

    def search_similar(self, query_text: str, size: int = 5) -> List[Dict]:
        """Busca sentenças similares usando embedding"""
        try:
            query_emb = self.create_openai_embedding(query_text)
            body = {
                "query": {
                    "script_score": {
                        "query":  {"match_all": {}},
                        "script": {
                            "source": "cosineSimilarity(params.query_vector,'embedding')+1.0",
                            "params": {"query_vector": query_emb}
                        }
                    }
                },
                "size": size,
                "_source": ["relatorio","fundamentacao","dispositivo","classe","assunto","processo"]
            }
            resp = self.es.search(index=self.index_name, body=body)
            return [
                {"score": hit["_score"], "content": hit["_source"]}
                for hit in resp["hits"]["hits"]
            ]
        except Exception as e:
            print(f"❌ Erro na busca: {e}")
            return []

    def setup(self):
        """Setup completo do Elasticsearch: índice + dados + teste"""
        print("🚀 Iniciando setup do Elasticsearch...")
        self.wait_for_elasticsearch()
        self.create_index()

        count = self.get_document_count()
        print(f"📊 Documentos atuais no índice: {count}")

        if count == 0:
            print("📚 Índice vazio - carregando dados iniciais...")
            df = self.load_sentences_from_csv()
            if df is not None and len(df) > 0:
                print(f"→ Processando {len(df)} sentenças...")
                success = 0
                for i, row in df.iterrows():
                    if self.index_sentence(row, f"sentence_{i}"):
                        success += 1
                    time.sleep(0.1)
                    if (i+1) % 10 == 0:
                        print(f"→ {i+1}/{len(df)} documentos processados")
                print(f"✅ Setup completo! {success}/{len(df)} documentos indexados")
            else:
                print("⚠️ Sem dados para indexar")
        else:
            print("✅ Índice já populado - setup completo")

        # Teste rápido
        print("🔍 Testando busca...")
        res = self.search_similar("ação de cobrança", size=2)
        if res:
            print(f"✅ Teste OK - {len(res)} resultados")
        else:
            print("⚠️ Teste de busca não retornou nada")

def setup_elasticsearch():
    """Função de entrada para o FastAPI startup"""
    try:
        setup = ElasticsearchSetup()
        setup.setup()
        return setup
    except Exception as e:
        print(f"❌ Erro no setup do Elasticsearch: {e}")
        raise

if __name__ == "__main__":
    setup_elasticsearch()

