"""
sre-demo-service — Cloud Run service that generates realistic SRE signals.

Endpoints:
  GET  /           → homepage, always 200
  GET  /health     → readiness check, always 200, logs INFO
  GET  /error      → forced 500 with full traceback in logs
  GET  /slow       → 200 after 6-9s (simulates DB back-pressure)
  GET  /crash      → unhandled exception → Cloud Run logs ERROR+stacktrace
  GET  /db-timeout → simulates connection pool exhaustion (5% chance of 500)
  POST /webhook    → event ingestion with ~20% random failure rate
  GET  /chaos      → randomly picks one of the failure modes (used by scheduler)

Observability:
  - Structured JSON logs → Cloud Logging (severity, logging.googleapis.com/trace)
  - OpenTelemetry spans  → Cloud Trace via CloudTraceSpanExporter
  - Log↔Trace correlation: StructuredLogHandler reads active OTel span context
    and injects logging.googleapis.com/trace + spanId into every log entry.
"""

import json
import logging
import os
import random
import time
import traceback
from datetime import datetime, timezone

from flask import Flask, jsonify, request


# ---------------------------------------------------------------------------
# OpenTelemetry setup — must happen before Flask is instrumented
# ---------------------------------------------------------------------------
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.propagators.cloud_trace_propagator import CloudTraceFormatPropagator
from opentelemetry.propagate import set_global_textmap


SERVICE_NAME    = os.getenv("K_SERVICE",  "sre-demo-service")
SERVICE_VERSION = os.getenv("K_REVISION", "local")
PROJECT_ID      = os.getenv("GOOGLE_CLOUD_PROJECT", "unknown-project")


class _LoggingSpanProcessor(SpanProcessor):
    """Wraps CloudTraceSpanExporter and logs each export result so failures
    are visible in Cloud Logging instead of being silently swallowed."""

    def __init__(self, exporter: CloudTraceSpanExporter) -> None:
        self._exporter = exporter
        self._log = logging.getLogger("sre-demo.trace")

    def on_start(self, span, parent_context=None):
        pass

    def on_end(self, span: ReadableSpan) -> None:
        try:
            result = self._exporter.export((span,))
            if result == SpanExportResult.SUCCESS:
                self._log.debug("TRACE_EXPORT | ok | span=%s | trace=%s", span.name,
                                format(span.context.trace_id, "032x") if span.context else "?")
            else:
                self._log.error("TRACE_EXPORT | FAILED | result=%s | span=%s", result, span.name)
        except Exception as exc:
            self._log.error("TRACE_EXPORT | EXCEPTION | span=%s | %s", span.name, exc)

    def shutdown(self) -> None:
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def _setup_tracing() -> None:
    """Export spans to Cloud Trace via the GCP exporter.

    SimpleSpanProcessor is used instead of BatchSpanProcessor because Cloud Run
    instances can be recycled before BatchSpanProcessor flushes its buffer,
    causing silent span loss. SimpleSpanProcessor exports each span synchronously
    the moment it ends, ensuring no spans are lost on instance shutdown.

    ALWAYS_ON sampler: the X-Cloud-Trace-Context header injected by the GCP load
    balancer often carries o=0 (sampling disabled). Without ALWAYS_ON, OTel would
    honour that flag and not export the span to Cloud Trace, while still writing
    the trace ID to Cloud Logging — producing orphan log entries with no matching
    trace in Cloud Trace. ALWAYS_ON forces every span to be exported regardless
    of the incoming sampling flag.

    CloudTraceFormatPropagator: reads X-Cloud-Trace-Context and maps the trace ID
    directly into the OTel span context, ensuring log entries and Cloud Trace spans
    share the same 32-char hex trace ID.
    """
    try:
        # Force all spans to be sampled and exported — overrides o=0 from GCP LB header
        set_global_textmap(CloudTraceFormatPropagator())
        exporter = CloudTraceSpanExporter(project_id=PROJECT_ID)
        provider = TracerProvider(sampler=ALWAYS_ON)
        provider.add_span_processor(_LoggingSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        logging.getLogger("sre-demo").info(
            "OTel Cloud Trace exporter initialised (ALWAYS_ON sampler) | project=%s", PROJECT_ID
        )
    except Exception as exc:
        logging.getLogger("sre-demo").warning(
            "Cloud Trace init failed (traces disabled): %s", exc
        )


_setup_tracing()


# ---------------------------------------------------------------------------
# Structured JSON logging with log↔trace correlation
# ---------------------------------------------------------------------------

def _get_trace_context() -> tuple[str, str, bool]:
    """Read trace context — OTel span first, HTTP header as fallback.

    Returns (trace_resource, hex_span_id, is_sampled).
    trace_resource is in Cloud Logging format: projects/{project_id}/traces/{trace_id}

    Priority:
      1. Active OTel span — includes child spans (e.g. payment-processor)
      2. X-Cloud-Trace-Context header — injected by GCP load balancer on every request,
         ensures every log entry is trace-correlated even when OTel context is absent.
    """
    # 1. Primary: active OTel span
    span = trace.get_current_span()
    ctx  = span.get_span_context() if span else None
    if ctx and ctx.is_valid:
        trace_id = format(ctx.trace_id, "032x")
        span_id  = format(ctx.span_id,  "016x")
        sampled  = bool(ctx.trace_flags & 0x01)
        return f"projects/{PROJECT_ID}/traces/{trace_id}", span_id, sampled

    # 2. Fallback: X-Cloud-Trace-Context header (format: TRACE_ID/SPAN_ID;o=FLAG)
    #    SPAN_ID is decimal in the header — convert to 16-char hex for Cloud Logging.
    try:
        from flask import request as _req
        header = _req.headers.get("X-Cloud-Trace-Context", "")
        if header:
            trace_part, _, rest = header.partition("/")
            span_part, _, flag_part = rest.partition(";")
            if len(trace_part) == 32:
                sampled = "o=1" in flag_part
                span_hex = ""
                if span_part:
                    try:
                        span_hex = format(int(span_part), "016x")
                    except ValueError:
                        span_hex = span_part
                return f"projects/{PROJECT_ID}/traces/{trace_part}", span_hex, sampled
    except RuntimeError:
        pass  # No Flask request context (background threads, startup logs)

    return "", "", False


class StructuredLogHandler(logging.StreamHandler):
    SEVERITY_MAP = {
        logging.DEBUG:    "DEBUG",
        logging.INFO:     "INFO",
        logging.WARNING:  "WARNING",
        logging.ERROR:    "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def emit(self, record: logging.LogRecord) -> None:
        severity = self.SEVERITY_MAP.get(record.levelno, "DEFAULT")
        trace_res, span_id, sampled = _get_trace_context()

        payload: dict = {
            "severity": severity,
            "message":  record.getMessage(),
            "time":     datetime.now(timezone.utc).isoformat(),
            "logger":   record.name,
            "service":  SERVICE_NAME,
            "version":  SERVICE_VERSION,
        }

        # Cloud Logging special fields — link log entry to Cloud Trace span
        if trace_res:
            payload["logging.googleapis.com/trace"]        = trace_res
            payload["logging.googleapis.com/spanId"]       = span_id
            payload["logging.googleapis.com/traceSampled"] = sampled

        for key in ("httpRequest", "labels", "error"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)

        print(json.dumps(payload), flush=True)


logging.basicConfig(level=logging.DEBUG, handlers=[StructuredLogHandler()])
log = logging.getLogger("sre-demo")

app   = Flask(__name__)
tracer = trace.get_tracer(SERVICE_NAME)

# Auto-instrument Flask — creates a span per request, propagates X-Cloud-Trace-Context
FlaskInstrumentor().instrument_app(app)


# ---------------------------------------------------------------------------
# Request logging middleware
# ---------------------------------------------------------------------------

@app.before_request
def _start_timer() -> None:
    request._start = time.time()   # type: ignore[attr-defined]


@app.after_request
def _log_request(response):
    duration_ms = round((time.time() - request._start) * 1000)  # type: ignore[attr-defined]
    log.info(
        "%s %s → %d (%dms)",
        request.method, request.path, response.status_code, duration_ms,
        extra={
            "httpRequest": {
                "requestMethod": request.method,
                "requestUrl":    request.url,
                "status":        response.status_code,
                "latency":       f"{duration_ms / 1000:.3f}s",
                "userAgent":     request.headers.get("User-Agent", ""),
                "remoteIp":      request.remote_addr,
            },
            "labels": {"endpoint": request.path, "latency_ms": str(duration_ms)},
        },
    )
    return response


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return jsonify({
        "service":   SERVICE_NAME,
        "version":   SERVICE_VERSION,
        "status":    "running",
        "endpoints": ["/health", "/error", "/slow", "/crash", "/db-timeout", "/webhook", "/chaos"],
    })


@app.get("/health")
def health():
    log.info("Health check OK", extra={"labels": {"check": "liveness"}})
    return jsonify({"status": "ok", "service": SERVICE_NAME,
                    "ts": datetime.now(timezone.utc).isoformat()})


@app.get("/error")
def forced_error():
    """Always returns 500 — simulates a payment processor failure."""
    with tracer.start_as_current_span("payment-processor.process") as span:
        span.set_attribute("component", "payment-processor")
        try:
            _simulate_db_query(fail=True)
        except Exception as exc:
            tb = traceback.format_exc()
            span.record_exception(exc)
            span.set_status(trace.StatusCode.ERROR, str(exc))
            log.error(
                "Unhandled exception in payment processing: %s", exc,
                extra={
                    "error":  {"type": type(exc).__name__, "message": str(exc), "stack": tb},
                    "labels": {"component": "payment-processor", "error_code": "DB_QUERY_FAILED"},
                },
            )
            return jsonify({
                "error":   "Internal server error",
                "code":    "PAYMENT_PROCESSOR_UNAVAILABLE",
                "message": "Failed to reach payment database — connection pool exhausted",
            }), 500


@app.get("/slow")
def slow_response():
    """Simulates a slow DB query — always breaches the 5 s SLO."""
    delay = random.uniform(6.0, 9.5)
    with tracer.start_as_current_span("order-service.db-query") as span:
        span.set_attribute("db.replica", "db-replica-3")
        log.warning(
            "Slow query detected — waiting %.1fs for replica to respond", delay,
            extra={"labels": {"component": "order-service", "latency_ms": str(int(delay * 1000))}},
        )
        time.sleep(delay)
        span.set_attribute("latency_ms", int(delay * 1000))
        log.warning(
            "Slow query completed after %.1fs — SLO breach (threshold=5s)", delay,
            extra={"labels": {"slo_breach": "true", "slo_name": "order-service-latency"}},
        )
    return jsonify({"status": "ok", "latency_s": round(delay, 2),
                    "slo_breach": True, "slo_threshold": 5.0})


@app.get("/crash")
def crash():
    """Raises an unhandled exception — simulates a worker OOM/crash."""
    log.error("Worker entering unstable state — OOM imminent",
               extra={"labels": {"component": "worker", "memory_mb": "1820"}})
    raise RuntimeError(
        "Segmentation fault in native extension libpayments.so (offset 0x4a2f1): "
        "null pointer dereference in PaymentGateway::processCharge()"
    )


@app.get("/db-timeout")
def db_timeout():
    """5 % hard fail, 30 % slow, 65 % OK — mimics a flaky database."""
    with tracer.start_as_current_span("db-pool.acquire") as span:
        roll = random.random()
        if roll < 0.05:
            span.set_status(trace.StatusCode.ERROR, "POOL_EXHAUSTED")
            log.error(
                "Database connection pool exhausted — all 50 connections in use",
                extra={"labels": {"component": "db-pool", "error_code": "POOL_EXHAUSTED"}},
            )
            return jsonify({"error": "Database connection pool exhausted"}), 500
        if roll < 0.35:
            delay = random.uniform(4.0, 8.0)
            span.set_attribute("latency_ms", int(delay * 1000))
            log.warning("Slow DB connection acquired after %.1fs", delay,
                        extra={"labels": {"component": "db-pool",
                                          "latency_ms": str(int(delay * 1000))}})
            time.sleep(delay)
        else:
            log.info("DB query OK", extra={"labels": {"component": "db-pool"}})
    return jsonify({"status": "ok"})


@app.route("/webhook", methods=["POST"])
def webhook():
    """Event ingestion — 20 % failure rate."""
    payload    = request.get_json(silent=True) or {}
    event_type = payload.get("event_type", "unknown")
    with tracer.start_as_current_span("webhook-processor.ingest") as span:
        span.set_attribute("event.type", event_type)
        if random.random() < 0.20:
            span.set_status(trace.StatusCode.ERROR, "SCHEMA_VALIDATION_FAILED")
            log.error(
                "Webhook processing failed for event '%s' — schema validation error",
                event_type,
                extra={"labels": {"event_type": event_type, "component": "webhook-processor",
                                   "error_code": "SCHEMA_VALIDATION_FAILED"}},
            )
            return jsonify({"error": "Schema validation failed", "event_type": event_type}), 500
        log.info("Webhook processed: %s", event_type,
                 extra={"labels": {"event_type": event_type}})
    return jsonify({"status": "accepted", "event_type": event_type})


@app.get("/chaos")
def chaos():
    """Randomly invokes one failure mode — used by Cloud Scheduler.

    Calls view functions directly (no test_request_context) so the active
    OTel span from the real /chaos request is preserved. This ensures the
    trace ID in Cloud Logging matches the trace ID exported to Cloud Trace.
    """
    mode = random.choices(
        ["error", "slow", "db-timeout", "health"],
        weights=[25, 20, 20, 35], k=1,
    )[0]
    log.info("Chaos mode selected: %s", mode)
    if mode == "error":      return forced_error()
    if mode == "slow":       return slow_response()
    if mode == "db-timeout": return db_timeout()
    return health()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _simulate_db_query(fail: bool = False):
    if fail:
        raise ConnectionError(
            "psycopg2.OperationalError: could not connect to server: Connection refused\n"
            "\tIs the server running on host 'payments-db.internal' (10.0.1.42) and accepting\n"
            "\tTCP/IP connections on port 5432?"
        )
    return [{"id": i, "amount": round(random.uniform(1, 999), 2)} for i in range(10)]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    log.info("Starting %s on port %d", SERVICE_NAME, port)
    app.run(host="0.0.0.0", port=port, debug=False)

