import asyncpg
import os

async def get_postgres_conn():
    return await asyncpg.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "rag_user"),
        password=os.getenv("POSTGRES_PASSWORD", "rag_password"),
        database=os.getenv("POSTGRES_DB", "rag_database"),
    )
