FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_HOST=0.0.0.0
ENV APP_PORT=8080

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY schema_v6_gptoss.yaml webservice_v1.py ./
COPY text2sql_agent ./text2sql_agent
COPY web ./web

EXPOSE 8080

CMD ["uvicorn", "webservice_v1:app", "--host", "0.0.0.0", "--port", "8080"]
