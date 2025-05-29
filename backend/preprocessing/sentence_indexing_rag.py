import pandas as pd
import re
import os
import time
import json
from typing import List, Dict, Optional
from elasticsearch import Elasticsearch
import openai
from openai import OpenAI

class ElasticsearchSetup:
    def __init__(self):
        # Configuração Elasticsearch
        self.es_host = os.getenv("ELASTICSEARCH_HOST", "http://elasticsearch:9200")
        self.es = Elasticsearch(hosts=[self.es_host])
        self.index_name = "sentencas_rag"
        
        # Configuração OpenAI
        openai_key = os.getenv("OPENAI_API_KEY")
        if not openai_key:
            raise ValueError("❌ OPENAI_API_KEY não encontrada nas variáveis de ambiente")
        
        self.openai_client = OpenAI(api_key=openai_key)
        
        print(f"→ Conectando ao Elasticsearch em {self.es_host}")
        print("→ Cliente OpenAI configurado")
    
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
            return [0.0] * 3072  # text-embedding-3-large tem 3072 dimensões
    
    def separar_partes_sentenca(self, texto: str) -> Dict[str, str]:
        """Separa sentença em relatório, fundamentação e dispositivo"""
        if not isinstance(texto, str):
            return {"relatorio": "", "fundamentacao": "", "dispositivo": ""}
        
        texto = texto.strip()
        padrao_fundamentacao = r"\b(passo a decidir|decido|passo à decisão)\b"
        padrao_dispositivo = r"\b(julgo|resolvo o mérito|condeno|condenar|extingo|declaro)\b"

        texto_lower = texto.lower()
        idx_fund = re.search(padrao_fundamentacao, texto_lower)
        idx_disp = re.search(padrao_dispositivo, texto_lower)

        i_fund = idx_fund.start() if idx_fund else None
        i_disp = idx_disp.start() if idx_disp else None

        relatorio = texto[:i_fund].strip() if i_fund else texto
        fundamentacao = texto[i_fund:i_disp].strip() if i_fund and i_disp else ""
        dispositivo = texto[i_disp:].strip() if i_disp else ""

        return {
            "relatorio": relatorio,
            "fundamentacao": fundamentacao,
            "dispositivo": dispositivo
        }
    
    def create_index(self):
        """Cria índice Elasticsearch com mapeamento otimizado"""
        try:
            if self.es.indices.exists(index=self.index_name):
                print(f"✅ Índice '{self.index_name}' já existe")
                return
            
            print(f"→ Criando índice '{self.index_name}'...")
            
            mapping = {
                "mappings": {
                    "properties": {
                        "relatorio": {"type": "text", "analyzer": "portuguese"},
                        "fundamentacao": {"type": "text", "analyzer": "portuguese"},
                        "dispositivo": {"type": "text", "analyzer": "portuguese"},
                        "julgado_completo": {"type": "text", "analyzer": "portuguese"},
                        "embedding": {
                            "type": "dense_vector",
                            "dims": 3072,  # text-embedding-3-large
                            "index": True,
                            "similarity": "cosine"
                        },
                        "classe": {"type": "keyword"},
                        "assunto": {"type": "keyword"},
                        "magistrado": {"type": "keyword"},
                        "processo": {"type": "keyword"},
                        "created_at": {"type": "date"},
                        "source": {"type": "keyword"}
                    }
                },
                "settings": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0,  # Railway não precisa réplicas
                    "index.max_result_window": 50000
                }
            }
            
            self.es.indices.create(index=self.index_name, body=mapping)
            print(f"✅ Índice '{self.index_name}' criado com sucesso")
            
        except Exception as e:
            print(f"❌ Erro ao criar índice: {e}")
            raise
    
    def load_sentences_from_csv(self, csv_path: str = "/app/data/sentencas.csv"):
        """Carrega sentenças do CSV e processa"""
        try:
            if not os.path.exists(csv_path):
                print(f"⚠️ Arquivo {csv_path} não encontrado")
                return
            
            print(f"→ Carregando dados de {csv_path}")
            df = pd.read_csv(csv_path)
            df.fillna("", inplace=True)
            
            print(f"✅ {len(df)} sentenças carregadas")
            return df
            
        except Exception as e:
            print(f"❌ Erro ao carregar CSV: {e}")
            return None
    
    def index_sentence(self, row: pd.Series, doc_id: str):
        """Indexa uma sentença individual"""
        try:
            # Separar partes da sentença
            julgado = row.get("julgado", "")
            partes = self.separar_partes_sentenca(julgado)
            
            # Usar relatório para embedding (parte mais importante para busca)
            texto_embedding = partes["relatorio"] or julgado[:1000]
            
            if not texto_embedding.strip():
                print(f"⚠️ Pulando documento {doc_id} - texto vazio")
                return False
            
            # Criar embedding
            print(f"→ Criando embedding para documento {doc_id}...")
            embedding = self.create_openai_embedding(texto_embedding)
            
            # Preparar documento
            doc = {
                "relatorio": partes["relatorio"],
                "fundamentacao": partes["fundamentacao"],
                "dispositivo": partes["dispositivo"],
                "julgado_completo": julgado,
                "embedding": embedding,
                "classe": row.get("classe", ""),
                "assunto": row.get("assunto", ""),
                "magistrado": row.get("magistrado", ""),
                "processo": str(row.get("processo", "")),
                "created_at": "now",
                "source": "csv_import"
            }
            
            # Indexar no Elasticsearch
            self.es.index(index=self.index_name, id=doc_id, document=doc)
            print(f"✅ Documento {doc_id} indexado")
            return True
            
        except Exception as e:
            print(f"❌ Erro ao indexar documento {doc_id}: {e}")
            return False
    
    def get_document_count(self) -> int:
        """Retorna número de documentos no índice"""
        try:
            count = self.es.count(index=self.index_name)["count"]
            return count
        except:
            return 0
    
    def search_similar(self, query_text: str, size: int = 5) -> List[Dict]:
        """Busca sentenças similares usando embedding"""
        try:
            # Criar embedding da query
            query_embedding = self.create_openai_embedding(query_text)
            
            # Busca por similaridade
            search_body = {
                "query": {
                    "script_score": {
                        "query": {"match_all": {}},
                        "script": {
                            "source": "cosineSimilarity(params.query_vector, 'embedding') + 1.0",
                            "params": {"query_vector": query_embedding}
                        }
                    }
                },
                "size": size,
                "_source": ["relatorio", "fundamentacao", "dispositivo", "classe", "assunto", "processo"]
            }
            
            response = self.es.search(index=self.index_name, body=search_body)
            
            results = []
            for hit in response["hits"]["hits"]:
                results.append({
                    "score": hit["_score"],
                    "content": hit["_source"]
                })
            
            return results
            
        except Exception as e:
            print(f"❌ Erro na busca: {e}")
            return []
    
    def setup(self):
        """Setup completo do Elasticsearch"""
        print("🚀 Iniciando setup do Elasticsearch...")
        
        # 1. Aguardar Elasticsearch
        self.wait_for_elasticsearch()
        
        # 2. Criar índice
        self.create_index()
        
        # 3. Verificar se já tem dados
        doc_count = self.get_document_count()
        print(f"📊 Documentos atuais no índice: {doc_count}")
        
        if doc_count == 0:
            print("📚 Índice vazio - carregando dados iniciais...")
            
            # 4. Carregar CSV
            df = self.load_sentences_from_csv()
            
            if df is not None and len(df) > 0:
                print(f"→ Processando {len(df)} sentenças...")
                
                success_count = 0
                for i, row in df.iterrows():
                    if self.index_sentence(row, f"sentence_{i}"):
                        success_count += 1
                    
                    # Pequena pausa para não sobrecarregar API
                    time.sleep(0.1)
                    
                    # Log a cada 10 documentos
                    if (i + 1) % 10 == 0:
                        print(f"→ Processados {i + 1}/{len(df)} documentos")
                
                print(f"✅ Setup completo! {success_count}/{len(df)} documentos indexados")
            else:
                print("⚠️ Nenhum dado encontrado para carregar")
        else:
            print("✅ Índice já populado - setup completo")
        
        # 5. Teste rápido
        self.test_search()
    
    def test_search(self):
        """Teste rápido de busca"""
        try:
            print("🔍 Testando busca...")
            results = self.search_similar("ação de cobrança", size=2)
            
            if results:
                print(f"✅ Teste de busca OK - encontrados {len(results)} resultados")
                for i, result in enumerate(results[:1]):
                    print(f"   Resultado {i+1}: score={result['score']:.3f}")
            else:
                print("⚠️ Teste de busca não retornou resultados")
                
        except Exception as e:
            print(f"❌ Erro no teste de busca: {e}")

# ────────────────────────────────────────────────
# Função principal para usar no FastAPI startup
# ────────────────────────────────────────────────
def setup_elasticsearch():
    """Função principal para setup automático"""
    try:
        setup = ElasticsearchSetup()
        setup.setup()
        return setup
    except Exception as e:
        print(f"❌ Erro no setup do Elasticsearch: {e}")
        raise

# ────────────────────────────────────────────────
# Para teste local/debug
# ────────────────────────────────────────────────
if __name__ == "__main__":
    setup_elasticsearch()
    
    
