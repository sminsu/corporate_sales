FROM deploy-base-image:slim

ARG APP_RELEASE=unknown

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_HOST=0.0.0.0
ENV APP_PORT=8080
ENV APP_RELEASE=${APP_RELEASE}
ENV PROMPT_GUARDRAIL_ENABLED=1

USER root

COPY combined.pem /etc/ssl/certs/combined.pem
COPY requirements.docker.txt ./
COPY wheels ./wheels
RUN pip install --no-cache-dir --no-index --find-links=/app/wheels -r requirements.docker.txt

COPY config ./config
COPY webapp_compatible_api ./webapp_compatible_api
COPY semantic_layer.yaml web_service.py ./
COPY text2sql_agent ./text2sql_agent
COPY web ./web
COPY outputs/managed_company_scope_20260714 ./outputs/managed_company_scope_20260714

EXPOSE 8080

CMD ["uvicorn", "web_service:app", "--host", "0.0.0.0", "--port", "8080"]
