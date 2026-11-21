import unittest
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import HTTPException
from agent_backend import app as backend

class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, question, answer, available=None, failure=None):
        captured = {}
        class MCP:
            def __init__(self, **kwargs): captured['mcp'] = kwargs
            async def __aenter__(self):
                if failure: raise failure
                self.functions = [SimpleNamespace(name=n) for n in (available if available is not None else backend.ALLOWED_TOOLS)]
                return self
            async def __aexit__(self, *args): pass
        class Agent:
            def __init__(self, **kwargs): captured['agent'] = kwargs
            async def run(self, message):
                captured['message'] = message
                return SimpleNamespace(text=answer)
        backend.app.state.foundry_client = object()
        with patch.object(backend, 'MCPStreamableHTTPTool', MCP), patch.object(backend, 'Agent', Agent):
            result = await backend.chat(backend.ChatRequest(message=question))
        return result, captured
    async def test_registered_tool_allowlist(self):
        result, call = await self.invoke('Inspect paymentservice', 'Pending')
        self.assertEqual(set(call['mcp']['allowed_tools']), {'inspect_namespace', 'find_repo_files', 'read_repo_file'})
        self.assertEqual(result.reply, 'Pending')
        self.assertEqual(call['agent']['instructions'], backend.INSTRUCTIONS)
        self.assertTrue(call['agent']['middleware'])
    async def test_kubernetes_only_and_partial_availability(self):
        result, call = await self.invoke('Is paymentservice running?', 'Pod pending, evidence ev-1', ['inspect_namespace'])
        self.assertIn('ev-1', result.reply)
        self.assertIn('For Kubernetes-only questions, avoid unnecessary GitHub calls', call['agent']['instructions'])
    async def test_kubernetes_and_github_evidence(self):
        result, call = await self.invoke('Why is cartservice failing?', 'Evidence ev-1; commit abc123; https://github.com/example/repo/blob/abc123/cart.py')
        self.assertIn('ev-1', result.reply)
        self.assertIn('abc123', result.reply)
        self.assertIn('read_repo_file', call['agent']['instructions'])
    async def test_namespace_resolution_failure(self):
        result, call = await self.invoke('Why is unknown failing?', 'I could not verify the namespace; please provide it.')
        self.assertIn('namespace', result.reply)
        self.assertIn('Never infer a namespace', call['agent']['instructions'])
    async def test_mcp_error(self):
        with self.assertRaises(HTTPException) as caught:
            await self.invoke('Inspect cart', '', failure=RuntimeError('sensitive server detail'))
        self.assertEqual(caught.exception.status_code, 502)
        self.assertNotIn('sensitive', caught.exception.detail)
    async def test_safe_tool_logging(self):
        middleware = backend.SafeToolLogging()
        ctx = SimpleNamespace(function=SimpleNamespace(name='read_repo_file'), result='{"error":{"message":"secret"}}')
        async def run(): pass
        with self.assertLogs(backend.logger, level='INFO') as captured:
            await middleware.process(ctx, run)
        self.assertIn('status=error', captured.output[-1])
        self.assertNotIn('secret', str(captured.output))

    def test_response_instructions_support_concise_and_explicit_detail_requests(self):
        instructions = backend.INSTRUCTIONS
        self.assertIn('normally respond in under 100 words', instructions)
        self.assertIn('Issue:', instructions)
        self.assertIn('Recommended fix:', instructions)
        self.assertIn('When the user explicitly requests logs, evidence, source code, commands, or detailed analysis', instructions)
        self.assertIn('For a simple factual question, answer directly', instructions)
        self.assertIn('clearly state when evidence is insufficient', instructions)

if __name__ == '__main__': unittest.main()
