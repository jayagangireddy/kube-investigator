import json
import os
import unittest
from unittest.mock import patch
from tools import github, kube_namespace as kube

POD = {'metadata': {'name': 'cart-1', 'namespace': 'cartservice'}, 'spec': {'containers': [{'name': 'cart', 'image': 'cart:v1', 'resources': {'limits': {'memory': '128Mi'}}}]}, 'status': {'phase': 'Running', 'containerStatuses': [{'name': 'cart', 'image': 'cart:v1', 'ready': False, 'restartCount': 18, 'state': {'waiting': {'reason': 'CrashLoopBackOff'}}, 'lastState': {'terminated': {'reason': 'OOMKilled', 'exitCode': 137}}}]}}
def event(name, timestamp, count):
    return {'metadata': {'name': name, 'namespace': 'cartservice'}, 'type': 'Warning', 'reason': 'BackOff', 'message': 'Back-off restarting container', 'count': count, 'lastTimestamp': timestamp, 'involvedObject': {'kind': 'Pod', 'name': 'cart-1'}}
DATA = {'pods': [POD], 'events': [event('e1', '2026-11-20T10:00:00Z', 3), event('e2', '2026-11-20T11:00:00Z', 5)], 'endpoints': [{'metadata': {'name': 'cart', 'namespace': 'cartservice'}, 'subsets': [{'notReadyAddresses': [{'ip': '10.0.0.1', 'targetRef': {'name': 'cart-1'}}]}]}], 'deployments': [{'metadata': {'name': 'cart', 'namespace': 'cartservice'}, 'spec': {'replicas': 1}, 'status': {'readyReplicas': 0}}]}

class InvestigationTests(unittest.TestCase):
    def inspect(self):
        with patch.dict(os.environ, {'ASTRONOMY_NAMESPACES': 'cartservice'}), patch.object(kube, '_api_list', side_effect=lambda ns, prefix, resource: DATA.get(resource, [])):
            return kube.inspect_namespace('cartservice')
    def test_diagnostic_sources(self):
        result = self.inspect()
        entries = {e['id']: e for e in result['evidence']}
        self.assertFalse(set(entries) & {e['id'] for e in self.inspect()['evidence']})
        fields = {field: value for entry in entries.values() for field, value in entry['fields'].items()}
        for field, value in [('status.containerStatuses[0].state.waiting.reason', 'CrashLoopBackOff'), ('status.containerStatuses[0].lastState.terminated.reason', 'OOMKilled'), ('status.containerStatuses[0].lastState.terminated.exitCode', 137), ('status.containerStatuses[0].restartCount', 18), ('status.containerStatuses[0].ready', False), ('spec.containers[0].resources.limits.memory', '128Mi'), ('status.readyReplicas', 0), ('subsets[0].notReadyAddresses[0].ip', '10.0.0.1')]:
            self.assertEqual(fields[field], value)
        self.assertNotIn('status.availableReplicas', fields)
        event_finding = next(f for f in result['findings'] if f['kind'] == 'Event')
        self.assertEqual(len(event_finding['sources']), 2)
        self.assertEqual(entries[event_finding['latestOccurrence']]['fields']['lastTimestamp'], '2026-11-20T11:00:00Z')
        for finding in result['findings']:
            for ref in finding['evidence']:
                self.assertIn(ref, entries)
            for source in finding.get('sources', []):
                for ref in source['evidence']:
                    self.assertIn(ref, entries)
    def test_errors(self):
        with patch.dict(os.environ, {'ASTRONOMY_NAMESPACES': 'cartservice'}), patch.object(kube, '_api_list', side_effect=kube.KubernetesReadError('permission_denied', 'denied')):
            result = kube.inspect_namespace('cartservice')
            self.assertEqual(result['errors']['pods']['code'], 'permission_denied')
    def test_discovery(self):
        tree = {'tree': [{'path': 'src/cart', 'type': 'tree'}, {'path': 'src/cart/Dockerfile', 'type': 'blob'}, {'path': 'src/cart/src/services/CartService.cs', 'type': 'blob'}, {'path': 'src/cart/values.yaml', 'type': 'blob'}, {'path': 'src/cart/readme.md', 'type': 'blob'}]}
        with patch.object(github, '_resolve_commit', return_value=('owner', 'repo', 'a' * 40)), patch.object(github, '_github_get', return_value=(tree, False)):
            result = github.find_repo_files('cartservice')
        paths = {f['path'] for f in result['files']}
        self.assertTrue({'src/cart/Dockerfile', 'src/cart/src/services/CartService.cs', 'src/cart/values.yaml'} <= paths)
        self.assertNotIn('src/cart/readme.md', paths)
        self.assertTrue(all(e['url'] and e['commitSha'] == 'a' * 40 for e in result['evidence']))

if __name__ == '__main__': unittest.main()
