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
# Service:   ap-agent serve          (approval UI + API + webhooks)
# Scheduled: ap-agent poll           (Cloud Run Job / ECS scheduled task / Container Apps Job)
#            ap-agent payment-run
CMD ["sh", "-c", "exec ap-agent serve --port ${PORT}"]
