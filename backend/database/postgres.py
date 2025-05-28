import asyncpg
import os

async def get_postgres_conn():
    return await asyncpg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=5432,
        user=os.getenv("POSTGRES_USER", "airflow"),
        password=os.getenv("POSTGRES_PASSWORD", "airflow"),
        database=os.getenv("POSTGRES_DB", "airflow"),
    )
