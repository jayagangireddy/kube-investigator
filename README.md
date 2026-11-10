# Kube Investigator MCP

The read-only inspect_namespace(namespace) tool is served over MCP Streamable HTTP at /mcp. It inventories these namespaced resource types: Pods, Services, Endpoints, ConfigMaps (key names only), PersistentVolumeClaims, Events, Deployments, ReplicaSets, StatefulSets, DaemonSets, Jobs, CronJobs, Ingresses, and NetworkPolicies. Secrets and ClusterRoleBindings are excluded.

## Configuration

- The initial server allowlist is ASTRONOMY_NAMESPACES=paymentservice in kubernetes/server/deployment.yaml. Add comma-separated namespace names there as needed.
- The MCP ServiceAccount namespace is set by the namespace field in kubernetes/server/kustomization.yaml (initially kube-investigator). Ensure that namespace exists before applying.
- For every namespace in ASTRONOMY_NAMESPACES, add a RoleBinding manifest in that namespace referencing the ClusterRole in kubernetes/role.yaml. Its subject namespace must match the ServiceAccount namespace.
- Replace YOUR_REGISTRY in kubernetes/kustomization.yaml with the registry login server. The image tag is 0.2.0.
- Kubernetes API requests have a 4-second timeout, configurable with KUBERNETES_API_TIMEOUT_SECONDS.

Build and push the image from this repository root, then apply the manifests:

docker build -t YOUR_REGISTRY/kube-investigator-mcp:0.2.0 .
docker push YOUR_REGISTRY/kube-investigator-mcp:0.2.0
kubectl apply -k kubernetes

The server is internal only. For local access after deployment, port-forward the Service on port 8000 and use http://localhost:8000/mcp.
