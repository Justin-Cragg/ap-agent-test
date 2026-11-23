# One image for every cloud. Build: docker build -t ap-agent .
FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install ".[all]"

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
COPY --from=build /install /usr/local
WORKDIR /app
COPY config ./config
USER app
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
# Service:   migrate (advisory-locked, safe with many replicas) then serve
# Scheduled: ap-agent poll | payment-run | purge          (agent identity)
#            ap-agent execute-payments                    (payment-executor identity)
CMD ["sh", "-c", "ap-agent migrate && exec ap-agent serve --port ${PORT}"]
