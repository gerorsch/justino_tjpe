# backend/services/elasticsearch.py
import os
from elasticsearch import Elasticsearch  # use o sync client para indexação / buscas
# ou, se você realmente quiser o async client:
# from elasticsearch import AsyncElasticsearch

# Ele vai buscar essa variável do docker-compose:
ELASTICSEARCH_HOST = os.getenv("ELASTICSEARCH_HOST", "http://elasticsearch:9200")

# Para o client síncrono:
es = Elasticsearch(hosts=[ELASTICSEARCH_HOST])

# Se for usar o AsyncElasticsearch, faça:
# es = AsyncElasticsearch(hosts=[ELASTICSEARCH_HOST])
