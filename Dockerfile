    FROM python:3.12-slim

WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
COPY templates ./templates
COPY scripts ./scripts
RUN pip install --no-cache-dir .

# All secrets come in as env vars via Container Apps secrets — nothing baked in.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
