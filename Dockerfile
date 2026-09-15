FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=120 --retries=10 \
    -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
COPY artifacts ./artifacts
COPY scripts ./scripts

EXPOSE 8080
# start.sh waits for the database before migrating, so a container that boots
# faster than the platform's private DNS does not crash-loop. Railway injects
# PORT; 8080 locally.
RUN chmod +x scripts/start.sh
CMD ["sh", "scripts/start.sh"]
