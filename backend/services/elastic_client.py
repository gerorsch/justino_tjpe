import os
from elasticsearch import Elasticsearch, helpers
from tqdm import tqdm   # pip install tqdm
from dotenv import load_dotenv

load_dotenv()

class ElasticClient:
    def __init__(self):
        cloud_id  = os.getenv("ELASTIC_CLOUD_ID")
        api_key   = os.getenv("ELASTICSEARCH_API_KEY")
        host      = os.getenv("ELASTICSEARCH_HOST")
        self.index_name = os.getenv("ELASTICSEARCH_INDEX", "sentencas_rag")

        if cloud_id and api_key:
            self.es = Elasticsearch(cloud_id=cloud_id, api_key=api_key)
        elif host and api_key:
            self.es = Elasticsearch(host, api_key=api_key, use_ssl=True, verify_certs=True)
        else:
            raise RuntimeError("🛑 Configure ELASTIC_CLOUD_ID ou ELASTICSEARCH_HOST + ELASTICSEARCH_API_KEY")

    def create_index(self, mappings: dict, settings: dict = None, delete_if_exists: bool = False):
        if delete_if_exists and self.es.indices.exists(index=self.index_name):
            self.es.indices.delete(index=self.index_name, ignore=[400, 404])
        body = {"mappings": mappings}
        if settings:
            body["settings"] = settings
        self.es.indices.create(index=self.index_name, body=body, ignore=400)

    # NOVO ────────────────────────────────────────────────────────────────────
    def migrate_from_local(self, local_url: str, chunk: int = 2_000):
        src = Elasticsearch(local_url)
        dst = self.es
        idx = self.index_name

        # 1. Criar o índice destino com o mesmo mapping
        mapping = src.indices.get(index=idx)[idx]["mappings"]
        self.create_index(mapping, delete_if_exists=False)

        # 2. Scroll + bulk copy
        scroll = helpers.scan(
                    src, index=idx, query={"query": {"match_all": {}}},
                    preserve_order=False
                )

        total_docs = src.count(index=idx)["count"]
        bar = tqdm(total=total_docs, unit="docs")

        def gen_actions():
            for doc in scroll:
                bar.update(1)
                yield {
                    "_op_type": "index",
                    "_index": idx,
                    "_id": doc["_id"],
                    **doc["_source"]
                }

        helpers.bulk(
            dst,
            gen_actions(),           # generator atualiza barra
            chunk_size=chunk,
            request_timeout=120,
            raise_on_error=True,
            stats_only=True,
            refresh=True,
        )
        bar.close()
        print(f"✅ Migração concluída: {total_docs} documentos copiados para {idx}")

# ───────── EXECUÇÃO ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    # • LOCAL_ES_URL: URL do cluster que já tem o índice populado (ex. http://localhost:9200)
    # • As demais variáveis (CLOUD_ID, API_KEY, etc.) já usadas pelo ElasticClient
    LOCAL_ES_URL = os.getenv("LOCAL_ES_URL", "http://localhost:9200")

    client = ElasticClient()
    client.migrate_from_local(local_url=LOCAL_ES_URL)

