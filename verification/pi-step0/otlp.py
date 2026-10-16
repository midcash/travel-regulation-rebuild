"""Local OTLP receiver: proves Python exports Node events without exporting payload text."""
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from travel_agent.telemetry import make_otel


class Capture:
    def __init__(self):
        self.spans = []
        spans = self.spans
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = self.rfile.read(int(self.headers['Content-Length']))
                if self.headers.get('Content-Encoding') == 'gzip':
                    data = gzip.decompress(data)
                request = ExportTraceServiceRequest()
                request.ParseFromString(data)
                for resource in request.resource_spans:
                    for scope in resource.scope_spans:
                        for span in scope.spans:
                            spans.append({'name': span.name, 'trace_id': span.trace_id.hex(),
                                          'operation':next((a.value.string_value for a in span.attributes if a.key=='operation'),None),
                                          'attributes': [a.key for a in span.attributes]})
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tracer, self.provider = make_otel(f'http://127.0.0.1:{self.server.server_port}/v1/traces')

    def close(self, expect_tools=False):
        try:
            self.provider.force_flush(timeout_millis=10000)
            self.provider.shutdown()
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        chats = [s for s in self.spans if s['name'] == 'chat']
        assert chats and all('tokens.totalTokens' in s['attributes'] for s in chats)
        assert not any(k in {'prompt', 'content', 'thinking', 'api_key'} for s in self.spans for k in s['attributes'])
        tools = sum(s['name']=='execute_tool' for s in self.spans)
        controls = sum(s['name']=='control' for s in self.spans)
        compactions = sum(s.get('operation')=='compact' for s in self.spans)
        if expect_tools:
            assert tools >= 1 and controls >= 2 and compactions >= 1
        return {'spans': len(self.spans), 'model_spans': len(chats), 'tool_spans':tools,
                'control_spans':controls,'compaction_model_spans':compactions,
                'tokens_exported': True, 'payload_text_exported': False}
