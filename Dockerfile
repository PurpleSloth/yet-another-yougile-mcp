FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY yougile.openapi.json ./yougile.openapi.json

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["yougile-mcp"]

