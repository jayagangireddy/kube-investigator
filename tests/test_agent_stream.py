import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from agent_backend import app as backend

class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, tool_calls=(), fail_connect=False, timeout=False, empty=False, question='Why is cartservice failing?'):
        class MCP:
            def __init__(self, **kwargs): pass
            async def __aenter__(self):
                if fail_connect: raise RuntimeError('secret-token')
                self.functions = [SimpleNamespace(name=n) for n in backend.ALLOWED_TOOLS]
                return self
            async def __aexit__(self, *args): pass
        class Agent:
            def __init__(self, **kwargs): self.middleware = kwargs['middleware'][0]
            def run(self, message, *, stream=False):
                assert stream
                middleware = self.middleware
                class Stream:
                    async def __aiter__(self):
                        for name, value in tool_calls:
                            ctx = SimpleNamespace(function=SimpleNamespace(name=name), arguments={'namespace':'cartservice'}, result=None)
                            async def invoke():
                                if isinstance(value, Exception): raise value
                                ctx.result = value
                            try: await middleware.process(ctx, invoke)
                            except Exception: pass
                        if timeout: await asyncio.sleep(1)
                        if not empty:
                            yield SimpleNamespace(role='assistant', text='Issue: OOMKilled.', merge_mode=None)
                            yield SimpleNamespace(role='tool', text='sensitive tool data', merge_mode=None)
                            yield SimpleNamespace(role='assistant', text='\n\nRecommended fix: Adjust memory.', merge_mode=None)
                    async def get_final_response(self):
                        return SimpleNamespace(text='' if empty else 'Issue: OOMKilled.\n\nRecommended fix: Adjust memory.')
                return Stream()
        backend.app.state.foundry_client = object()
        with patch.object(backend, 'MCPStreamableHTTPTool', MCP), patch.object(backend, 'Agent', Agent), patch.object(backend, 'CHAT_TIMEOUT_SECONDS', 0.01 if timeout else 5):
            response = await backend.chat_stream(backend.ChatRequest(message=question))
            self.assertEqual(response.media_type, 'text/event-stream')
            frames = [chunk async for chunk in response.body_iterator]
        events = []
        for frame in frames:
            lines = frame.strip().splitlines()
            events.append((lines[0][7:], json.loads(lines[1][6:])))
        return events
    async def test_actual_tool_and_final(self):
        events = await self.collect([('inspect_namespace', '{"findings":[]}')])
        self.assertEqual([(kind, data.get('state')) for kind, data in events], [('status','started'),('status','completed'),('delta',None),('delta',None),('final',None)])
        self.assertIn('cartservice', events[0][1]['message'])
        self.assertIn('Issue:', events[-1][1]['reply'])
        self.assertNotIn('find_repo_files', str(events))
        self.assertNotIn('read_repo_file', str(events))
        self.assertNotIn('sensitive tool data', str(events))
        self.assertEqual(''.join(data['text'] for kind, data in events if kind == 'delta'), events[-1][1]['reply'])
    async def test_general_conversation_without_tool_events(self):
        for question in ('hi', 'What can you do?', 'What is Kubernetes?'):
            events = await self.collect(question=question)
            self.assertFalse(any(kind == 'status' for kind, _ in events))
    async def test_other_tools_only_when_invoked(self):
        events = await self.collect([('find_repo_files','{}'),('read_repo_file','{}')])
        self.assertEqual([e[1]['tool'] for e in events if e[0]=='status'], ['find_repo_files','find_repo_files','read_repo_file','read_repo_file'])
    async def test_tool_errors_are_safe(self):
        events = await self.collect([('inspect_namespace', RuntimeError('secret-token'))])
        self.assertEqual(events[1][1]['state'], 'failed')
        self.assertEqual(events[2][0], 'delta')
        self.assertNotIn('secret-token', str(events))
    async def test_structured_tool_error(self):
        events = await self.collect([('inspect_namespace', '{"error":{"message":"secret-token"}}')])
        self.assertEqual(events[1][1]['state'], 'failed')
        self.assertNotIn('secret-token', str(events))
    async def test_connection_error(self):
        events = await self.collect(fail_connect=True)
        self.assertEqual(events[0][0], 'error')
        self.assertNotIn('secret-token', str(events))
    async def test_timeout_and_empty(self):
        self.assertIn('timed out', (await self.collect(timeout=True))[-1][1]['message'])
        self.assertIn('empty', (await self.collect(empty=True))[-1][1]['message'])

if __name__ == '__main__': unittest.main()
