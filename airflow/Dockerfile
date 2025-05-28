FROM apache/airflow:2.7.2-python3.10


USER airflow
RUN pip install --no-cache-dir \
    pandas \
    orjsonl \
    psycopg2-binary \
    elasticsearch \
    motor \
    httpx

COPY airflow.cfg /opt/airflow/airflow.cfg
COPY dags/ /opt/airflow/dags/
COPY plugins/ /opt/airflow/plugins/


