# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_HOST=0.0.0.0
ENV APP_PORT=8080

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git libpq-dev openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN mkdir -p -m 0700 /root/.ssh \
    && ssh-keyscan github.com >> /root/.ssh/known_hosts
RUN --mount=type=ssh,required=true pip install --no-cache-dir -r requirements.txt

COPY config ./config
COPY schema_v6_gptoss.yaml webservice_v1.py ./
COPY text2sql_agent ./text2sql_agent
COPY webapp_compatible_api ./webapp_compatible_api
COPY web ./web

EXPOSE 8080

CMD ["uvicorn", "webservice_v1:app", "--host", "0.0.0.0", "--port", "8080"]
