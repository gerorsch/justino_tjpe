from database.elasticsearch import es
from database.mongo import db

async def retrieve_documents(query: str, index: str = "rag_index", top_k: int = 3):
    es_response = await es.search(index=index, query={"match": {"text": query}}, size=top_k)
    results = []
    for hit in es_response["hits"]["hits"]:
        doc_id = hit["_id"]
        doc = await db.documentos.find_one({"_id": doc_id})
        if doc:
            results.append(doc)
    return results
