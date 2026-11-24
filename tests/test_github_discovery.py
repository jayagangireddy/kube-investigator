import os
import unittest
from unittest.mock import patch
from tools import github

SHA = 'a' * 40
TREE = {'tree': [
    {'path':'src/payment', 'type':'tree'},
    {'path':'src/payment/app.py', 'type':'blob'},
    {'path':'kubernetes/paymentservice/deployment.yaml', 'type':'blob'},
    {'path':'kubernetes/overlays/paymentservice/kustomization.yaml', 'type':'blob'},
    {'path':'helm/astronomy/values.yaml', 'type':'blob'},
    {'path':'helm/astronomy/templates/deployment.yaml', 'type':'blob'},
    {'path':'docs/notes.md', 'type':'blob'},
]}

class DiscoveryTests(unittest.TestCase):
    def run_search(self, service='paymentservice', ref=None, query=None, tree=TREE):
        calls=[]
        def get(endpoint, params=None, **kwargs):
            calls.append((endpoint, params))
            if endpoint == '': return {'default_branch':'main'}, False
            if endpoint.startswith('commits/'): return {'sha': SHA}, False
            if endpoint.startswith('git/trees/'): return tree, False
            raise AssertionError(endpoint)
        def read(path, ref):
            return {'text': 'kind: Deployment\nimage: example/payment:v1\n', 'permalink': f'https://github.com/jayagangireddy/astronamy/blob/{ref}/{path}', 'truncated': False}
        with patch.dict(os.environ, {'GITHUB_REPO_OWNER':'jayagangireddy', 'GITHUB_REPO_NAME':'astronamy'}), patch.object(github, '_github_get', side_effect=get), patch.object(github, 'read_repo_file', side_effect=read):
            return github.find_repo_files(service, query=query, ref=ref), calls
    def test_deployment_helm_and_kustomize_candidates(self):
        result, _ = self.run_search(query='deployment')
        paths={item['path']:item['purpose'] for item in result['files']}
        self.assertIn('kubernetes/paymentservice/deployment.yaml', paths)
        self.assertIn('helm/astronomy/values.yaml', paths)
        self.assertIn('helm/astronomy/templates/deployment.yaml', paths)
        self.assertIn('kubernetes/overlays/paymentservice/kustomization.yaml', paths)
        self.assertIn('unverified', paths['helm/astronomy/values.yaml'])
        self.assertTrue(all(e['commitSha'] == SHA and SHA in e['url'] for e in result['evidence']))
        self.assertEqual(result['imageConfigEvidence'][0]['text'], 'image: example/payment:v1')
        self.assertEqual(result['imageConfigEvidence'][0]['line'], 2)
    def test_explicit_non_default_branch(self):
        result, calls = self.run_search(ref='release/fix')
        self.assertIn(('commits/release%2Ffix', None), calls)
        self.assertNotIn(('', None), calls)
        self.assertEqual(result['ref'], 'release/fix')
    def test_deployment_filename_query_finds_deploy_yaml(self):
        result, _ = self.run_search(query='deployment.yaml')
        self.assertIn('kubernetes/paymentservice/deployment.yaml', [item['path'] for item in result['files']])
        tree = {'tree': [{'path': 'kubernetes/cart/deploy.yaml', 'type': 'blob'}]}
        result, _ = self.run_search(service='cartservice', query='deployment.yaml', tree=tree)
        self.assertIn('kubernetes/cart/deploy.yaml', [item['path'] for item in result['files']])
    def test_service_name_and_multiword_query_find_manifest(self):
        tree = {'tree': [{'path': 'kubernetes/cart/deploy.yaml', 'type': 'blob'}]}
        for query in ('cartservice', 'cartservice deployment', 'cartservice deployment yaml'):
            result, _ = self.run_search(service='cartservice', query=query, tree=tree)
            self.assertIn('kubernetes/cart/deploy.yaml', [item['path'] for item in result['files']])
    def test_no_path_matches_and_incomplete_tree(self):
        empty={'tree':[]}
        result, _=self.run_search(service='notknown', tree=empty)
        self.assertEqual(result['searchStatus'], 'no_path_matches')
        self.assertTrue(result['incomplete'])
        result, _=self.run_search(tree={'tree':[], 'truncated':True})
        self.assertEqual(result['searchStatus'], 'incomplete')
    def test_github_failure(self):
        with patch.object(github, '_resolve_commit', side_effect=github.GitHubReadError('github_unavailable','offline')):
            self.assertEqual(github.find_repo_files('paymentservice')['error']['code'],'github_unavailable')
    def test_branch_unavailable(self):
        with patch.dict(os.environ, {'GITHUB_REPO_OWNER':'owner', 'GITHUB_REPO_NAME':'repo'}), patch.object(github, '_github_get', side_effect=github.GitHubReadError('github_resource_not_found_or_inaccessible','missing')):
            self.assertEqual(github.find_repo_files('paymentservice', ref='missing')['error']['code'], 'github_ref_not_found_or_inaccessible')
    def test_source_file_discovery(self):
        result,_=self.run_search()
        self.assertIn('src/payment/app.py', [f['path'] for f in result['files']])

if __name__ == '__main__': unittest.main()
