"""Optional OpenTelemetry trace setup shared independently of the agent framework."""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

LOGGER = logging.getLogger("kube_investigator_agent.telemetry")
_lock = threading.Lock()
_provider: Any = None


def initialize_telemetry() -> None:
    """Install one console trace provider only when explicitly requested.

    Existing providers are left untouched. No exporter or provider is installed
    by default; future OTLP setup can use the same provider ownership boundary.
    """
    global _provider
    if os.getenv("KUBE_OTEL_CONSOLE_EXPORTER", "false").strip().lower() not in {"1", "true", "yes"}:
        return
    with _lock:
        if _provider is not None:
            return
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
            if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
                LOGGER.warning("telemetry_existing_provider; console exporter not installed")
                return
            provider = TracerProvider(resource=Resource.create({"service.name": "kube-investigator-agent"}))
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
            trace.set_tracer_provider(provider)
            if trace.get_tracer_provider() is not provider:
                provider.shutdown()
                LOGGER.warning("telemetry_provider_registration_failed")
                return
            _provider = provider
            LOGGER.info("telemetry_console_enabled")
        except Exception as exc:
            LOGGER.warning("telemetry_initialization_failed error_type=%s", type(exc).__name__)


def flush_telemetry() -> None:
    """Flush only a provider owned by this module; never stop a host provider."""
    if _provider is not None:
        try:
            _provider.force_flush(timeout_millis=2000)
        except Exception as exc:
            LOGGER.warning("telemetry_flush_failed error_type=%s", type(exc).__name__)
