import base64
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from tools import github

SHA = '3aa625f7a115efb20c7929796093acff10646562'
PATH = 'src/cart/src/Dockerfile'
CONTENT = b'FROM example\nRUN echo hello\n'

class Response:
    headers = {}
    def __init__(self, data): self.data = data
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, n): return self.data[:n]

class ReadFileTests(unittest.TestCase):
    def run_file(self, content_response, raw_response=CONTENT):
        calls = []
        def open_request(request, **kwargs):
            calls.append(request)
            if '/commits/' in request.full_url:
                return Response(json.dumps({'sha': SHA}).encode())
            if request.get_header('Accept') == 'application/vnd.github.raw+json':
                if isinstance(raw_response, Exception): raise raw_response
                return Response(raw_response)
            if isinstance(content_response, Exception): raise content_response
            return Response(json.dumps(content_response).encode())
        with patch.dict(os.environ, {'GITHUB_REPO_OWNER': 'jayagangireddy', 'GITHUB_REPO_NAME': 'astronamy', 'GITHUB_TOKEN': 'test-token'}), patch.object(github, 'urlopen', side_effect=open_request):
            return github.read_repo_file(PATH, ref=SHA), calls
    def payload(self, content, encoding='base64'):
        return {'type': 'file', 'path': PATH, 'size': len(CONTENT), 'encoding': encoding, 'content': content}
    def test_valid_inline_and_lines(self):
        result, calls = self.run_file(self.payload(base64.b64encode(CONTENT).decode()))
        self.assertEqual(result['text'], CONTENT.decode())
        self.assertEqual(result['returnedLines'], {'start': 1, 'end': 2})
        self.assertEqual(result['resolvedCommitSha'], SHA)
        self.assertEqual(result['evidence'][0]['url'], result['permalink'])
        self.assertEqual(len(calls), 2)
    def test_multiline_base64(self):
        encoded = base64.b64encode(CONTENT).decode()
        result, _ = self.run_file(self.payload(encoded[:8] + '\r\n' + encoded[8:] + '\n'))
        self.assertEqual(result['text'], CONTENT.decode())
    def test_missing_inline_fallback_uses_same_commit_and_auth(self):
        payload = self.payload(None, 'none')
        result, calls = self.run_file(payload)
        self.assertEqual(result['text'], CONTENT.decode())
        self.assertIn('ref=' + SHA, calls[-1].full_url)
        self.assertEqual(calls[-1].get_header('Authorization'), 'Bearer test-token')
        self.assertEqual(calls[-1].get_header('Accept'), 'application/vnd.github.raw+json')
    def test_malformed_base64_does_not_fallback(self):
        result, calls = self.run_file(self.payload('not!base64'))
        self.assertEqual(result['error']['code'], 'github_invalid_content')
        self.assertEqual(len(calls), 2)
    def test_unsupported_encoding(self):
        result, calls = self.run_file(self.payload('abc', 'gzip'))
        self.assertEqual(result['error']['code'], 'unsupported_file_encoding')
        self.assertEqual(len(calls), 2)
    def test_http_error_distinct_from_decoding(self):
        error = HTTPError('https://api.github.com/', 500, 'failure', {}, io.BytesIO(b'failure'))
        result, _ = self.run_file(error)
        self.assertEqual(result['error']['code'], 'github_api_error')
        self.assertIn('HTTP 500', result['error']['message'])
    def test_raw_fallback_http_error(self):
        error = HTTPError('https://api.github.com/', 403, 'denied', {}, io.BytesIO(b'denied'))
        result, _ = self.run_file(self.payload(None, 'none'), error)
        self.assertEqual(result['error']['code'], 'github_access_denied')
    def test_raw_fallback_size_limit(self):
        result, _ = self.run_file(self.payload(None, 'none'), b'x' * (github.MAX_REPO_FILE_BYTES + 1))
        self.assertEqual(result['error']['code'], 'repo_file_too_large')
    def test_empty_file(self):
        result, calls = self.run_file({'type':'file', 'path':PATH, 'size':0, 'encoding':'base64', 'content':''})
        self.assertEqual(result['text'], '')
        self.assertEqual(len(calls), 2)
    def test_invalid_path(self):
        result = github.read_repo_file('../Dockerfile', ref=SHA)
        self.assertEqual(result['error']['code'], 'invalid_path')

if __name__ == '__main__': unittest.main()
