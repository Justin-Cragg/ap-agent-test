"""Structured logging, Prometheus metrics, optional OpenTelemetry tracing, and
LLM cost accounting."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from ap_agent.config import Settings

REGISTRY = CollectorRegistry()
EMAILS = Counter("ap_emails_processed_total", "Emails processed", ["entity", "status"], registry=REGISTRY)
DECISIONS = Counter("ap_invoice_decisions_total", "Policy decisions on submitted invoices", ["entity", "decision"], registry=REGISTRY)
POSTINGS = Counter("ap_erp_postings_total", "ERP postings", ["entity", "result"], registry=REGISTRY)
PAYMENTS = Counter("ap_payments_recorded_total", "Payments recorded in the ERP", ["entity", "result"], registry=REGISTRY)
JOBS = Counter("ap_jobs_total", "Queue jobs finished", ["kind", "status"], registry=REGISTRY)
DEAD_JOBS = Gauge("ap_jobs_dead", "Jobs in the dead-letter state", registry=REGISTRY)
TOKENS = Counter("ap_llm_tokens_total", "LLM tokens", ["type"], registry=REGISTRY)
COST = Counter("ap_llm_cost_usd_total", "Estimated LLM spend (USD)", ["task"], registry=REGISTRY)
AGENT_TURNS = Histogram("ap_agent_turns", "Agent turns per task", ["task"], buckets=(1, 2, 4, 6, 8, 12, 16, 24, 32), registry=REGISTRY)
APPROVAL_LATENCY = Histogram("ap_approval_latency_hours", "Hours from approval request to decision", ["kind"], buckets=(1, 4, 8, 24, 48, 72, 168), registry=REGISTRY)


def llm_cost(settings: Settings, usage: dict[str, int]) -> float:
    m = 1_000_000
    return round(
        usage.get("input_tokens", 0) / m * settings.llm_price_input
        + usage.get("output_tokens", 0) / m * settings.llm_price_output
        + usage.get("cache_read_input_tokens", 0) / m * settings.llm_price_cache_read
        + usage.get("cache_creation_input_tokens", 0) / m * settings.llm_price_cache_write,
        6,
    )


def record_usage(settings: Settings, task: str, usage: dict[str, int], turns: int) -> float:
    for k, v in usage.items():
        TOKENS.labels(k).inc(v)
    cost = llm_cost(settings, usage)
    COST.labels(task).inc(cost)
    AGENT_TURNS.labels(task).observe(turns)
    return cost


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "severity": record.levelname,  # Cloud Logging / CloudWatch / Azure Monitor all parse this
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("entity", "job_id", "message_id", "invoice_id", "request_id"):
            if hasattr(record, key):
                out[key] = getattr(record, key)
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.log_format == "json" else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level)


def setup_tracing(settings: Settings, app=None) -> None:
    """OpenTelemetry via OTEL_EXPORTER_OTLP_* env vars (install the `otel` extra)."""
    if not settings.otel_enabled:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "ap-agent", "deployment.environment": settings.environment}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
