import os
import sys
import types
import unittest
from unittest.mock import patch
from agent_backend import observability as telemetry
from opentelemetry import trace

class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.original = telemetry._provider
        telemetry._provider = None
    def tearDown(self):
        telemetry._provider = self.original
    def fake_modules(self, providers):
        sdk=types.ModuleType('opentelemetry.sdk'); sdk.__path__=[]
        resources=types.ModuleType('opentelemetry.sdk.resources')
        tracing=types.ModuleType('opentelemetry.sdk.trace'); tracing.__path__=[]
        export=types.ModuleType('opentelemetry.sdk.trace.export')
        class Resource:
            @staticmethod
            def create(attributes): return attributes
        class Provider:
            def __init__(self, resource): self.resource=resource; self.processors=[]; self.flushes=0; providers.append(self)
            def add_span_processor(self, processor): self.processors.append(processor)
            def force_flush(self, timeout_millis): self.flushes+=1
            def shutdown(self): pass
        resources.Resource=Resource; tracing.TracerProvider=Provider
        export.ConsoleSpanExporter=type('ConsoleSpanExporter', (), {})
        export.SimpleSpanProcessor=lambda exporter: exporter
        return {'opentelemetry.sdk':sdk,'opentelemetry.sdk.resources':resources,'opentelemetry.sdk.trace':tracing,'opentelemetry.sdk.trace.export':export}
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(trace, 'get_tracer_provider', side_effect=AssertionError('must not configure')):
            telemetry.initialize_telemetry()
        self.assertIsNone(telemetry._provider)
    def test_opt_in_once_and_flush(self):
        providers=[]; current=[trace.ProxyTracerProvider()]
        with patch.dict(os.environ, {'KUBE_OTEL_CONSOLE_EXPORTER':'true'}), patch.dict(sys.modules,self.fake_modules(providers)), patch.object(trace,'get_tracer_provider',side_effect=lambda:current[0]), patch.object(trace,'set_tracer_provider',side_effect=lambda p:current.__setitem__(0,p)):
            telemetry.initialize_telemetry(); telemetry.initialize_telemetry(); telemetry.flush_telemetry()
        self.assertEqual(len(providers),1)
        self.assertEqual(providers[0].resource['service.name'],'kube-investigator-agent')
        self.assertEqual(len(providers[0].processors),1)
        self.assertEqual(providers[0].flushes,1)
    def test_existing_provider_not_replaced(self):
        providers=[]
        with patch.dict(os.environ, {'KUBE_OTEL_CONSOLE_EXPORTER':'true'}), patch.dict(sys.modules,self.fake_modules(providers)), patch.object(trace,'get_tracer_provider',return_value=object()), patch.object(trace,'set_tracer_provider') as setter:
            telemetry.initialize_telemetry()
        self.assertEqual(providers,[]); setter.assert_not_called()
    def test_initialization_failure_is_safe(self):
        with patch.dict(os.environ, {'KUBE_OTEL_CONSOLE_EXPORTER':'true'}), patch.object(trace,'get_tracer_provider',side_effect=RuntimeError('private payload')):
            with self.assertLogs(telemetry.LOGGER,level='WARNING') as logs:
                telemetry.initialize_telemetry()
        self.assertNotIn('private payload', str(logs.output))

if __name__=='__main__': unittest.main()
