FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/digest
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 digest \
    && useradd --uid 10001 --gid digest --no-create-home digest \
    && install -d -o digest -g digest /data
COPY app/ ./app/
USER digest
ENV DATABASE_PATH=/data/twitter_digest.db
CMD ["python", "-m", "app.main"]
