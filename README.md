# Kube Investigator MCP

The read-only `inspect_namespace(namespace)`, `list_repo_commits(path=None, ref=None, limit=10)`, and `get_commit(sha)` tools are served over the existing MCP Streamable HTTP endpoint at `/mcp`. The GitHub tools can only access the repository configured by `GITHUB_REPO_OWNER` and `GITHUB_REPO_NAME`.

Tool implementations are split by integration: `tools/kube_namespace.py` contains Kubernetes inventory, `tools/github.py` contains GitHub commit tools, and `server.py` creates the shared FastMCP app and registers each module.

`inspect_namespace` inventories Pods, Services, Endpoints, ConfigMaps (key names only), PersistentVolumeClaims, Events, Deployments, ReplicaSets, StatefulSets, DaemonSets, Jobs, CronJobs, Ingresses, and NetworkPolicies. Secrets and cluster-scoped resources are excluded.

## Configuration

- ASTRONOMY_NAMESPACES=* allows inspection of any namespace. Set comma-separated namespace names instead to restrict access.
- The MCP ServiceAccount namespace is set by the namespace field in kubernetes/server/kustomization.yaml (initially kube-investigator). Ensure that namespace exists before applying.
- The ClusterRoleBinding grants the server read access to supported resource types across all namespaces. It does not grant access to Secrets or cluster-scoped resources.
- Set `GITHUB_REPO_OWNER` and `GITHUB_REPO_NAME` in `kubernetes/server/deployment.yaml` to the owner and name of the Astronomy Shop source repository. Both are required for the GitHub tools.
- `GITHUB_TOKEN` is optional for public repositories. For a private repository or higher API rate limits, create a Kubernetes Secret named `github-token` in the `kube-investigator` namespace, with key `token`. The Deployment references this Secret without storing the credential in the manifest. Use a fine-grained GitHub token restricted to the selected repository with **Contents: Read** permission. Keep the token in your secret manager or secure local input; do not commit it or put it in source, manifests, logs, or tool responses.
- To create that Secret from a protected local token file, run `kubectl create secret generic github-token --namespace kube-investigator --from-file=token="C:\secure\github-token.txt"` (replace the example path with your protected file). Keep the file outside the repository and remove it according to your credential-handling policy.
- GitHub requests have an 8-second timeout. Commit listings are limited to 30 results. `get_commit` returns at most 50 changed files and 40,000 patch characters total (at most 8,000 per file); its response marks file or patch truncation and explicitly marks unavailable patches.
- Replace YOUR_REGISTRY in kubernetes/kustomization.yaml with the registry login server. The image tag is 0.2.1.
- Kubernetes API requests have a 4-second timeout, configurable with KUBERNETES_API_TIMEOUT_SECONDS.

The GitHub tools return structured errors for missing repository configuration, inaccessible resources, rate limits, and API failures. When `ref` is omitted, GitHub lists commits from the repository's default branch.

## Example: investigate paymentservice

1. Find recent changes to the paymentservice manifest (adjust the path to the actual repository-relative path): `list_repo_commits(path="kubernetes/paymentservice/deployment.yaml", limit=10)`.
2. Fetch a relevant diff with `get_commit(sha="<commit-sha-from-result>")` and review the changed fields.
3. Compare declared values such as replica count and service ports with the current live snapshot from `inspect_namespace(namespace="paymentservice")`, including its Deployment readiness and Service details.

A Git commit shows source history only. It does not prove that the commit was built, deployed, or is currently running in AKS; verify deployment evidence separately, such as the live image/version and rollout state.

Build and push the image from this repository root, then apply the manifests:

docker build -t YOUR_REGISTRY/kube-investigator-mcp:0.2.1 .
docker push YOUR_REGISTRY/kube-investigator-mcp:0.2.1
kubectl apply -k kubernetes

The server is internal only. For local access after deployment, port-forward the Service on port 8000 and use http://localhost:8000/mcp.
