# backend/services/elastic_client.py

import os
from elasticsearch import Elasticsearch

class ElasticClient:
    def __init__(self):
        cloud_id = os.getenv("ELASTIC_CLOUD_ID")
        api_key  = os.getenv("ELASTICSEARCH_API_KEY")
        host     = os.getenv("ELASTICSEARCH_HOST")

        if cloud_id and api_key:
            # Conexão via Elastic Cloud
            self.es = Elasticsearch(cloud_id=cloud_id, api_key=api_key)
        elif host and api_key:
            # Fallback para host + API Key
            self.es = Elasticsearch(host, api_key=api_key, use_ssl=True, verify_certs=True)
        else:
            raise RuntimeError("🛑 Variáveis ELASTIC_CLOUD_ID ou ELASTICSEARCH_HOST/API_KEY não configuradas")

        # nome do índice
        self.index_name = os.getenv("ELASTICSEARCH_INDEX", "sentencas_rag")

    def create_index(self, mappings: dict, settings: dict = None):
        # Deleta índice se já existir (opcional)
        if self.es.indices.exists(index=self.index_name):
            self.es.indices.delete(index=self.index_name)
        body = {}
        if settings:
            body["settings"] = settings
        body["mappings"] = mappings
        return self.es.indices.create(index=self.index_name, body=body)

    def put_mapping(self, mappings: dict):
        return self.es.indices.put_mapping(index=self.index_name, body=mappings)
