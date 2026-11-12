"""Read-only Kubernetes namespace inventory MCP tool."""

from __future__ import annotations

import json
import os
import re
import ssl
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API_TIMEOUT_SECONDS = float(os.getenv("KUBERNETES_API_TIMEOUT_SECONDS", "4"))
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
NAMESPACE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SUPPORTED_RESOURCES = (
    ("pods", "/api/v1", "pods", "Pod"),
    ("services", "/api/v1", "services", "Service"),
    ("endpoints", "/api/v1", "endpoints", "Endpoints"),
    ("configmaps", "/api/v1", "configmaps", "ConfigMap"),
    ("persistentvolumeclaims", "/api/v1", "persistentvolumeclaims", "PersistentVolumeClaim"),
    ("events", "/api/v1", "events", "Event"),
    ("deployments", "/apis/apps/v1", "deployments", "Deployment"),
    ("replicasets", "/apis/apps/v1", "replicasets", "ReplicaSet"),
    ("statefulsets", "/apis/apps/v1", "statefulsets", "StatefulSet"),
    ("daemonsets", "/apis/apps/v1", "daemonsets", "DaemonSet"),
    ("jobs", "/apis/batch/v1", "jobs", "Job"),
    ("cronjobs", "/apis/batch/v1", "cronjobs", "CronJob"),
    ("ingresses", "/apis/networking.k8s.io/v1", "ingresses", "Ingress"),
    ("networkpolicies", "/apis/networking.k8s.io/v1", "networkpolicies", "NetworkPolicy"),
)


class KubernetesReadError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _allowed_namespaces() -> set[str]:
    configured = os.getenv("ASTRONOMY_NAMESPACES", "")
    return {
        name.strip()
        for name in configured.split(",")
        if name.strip()
    }


def _api_list(namespace: str, api_prefix: str, resource: str) -> list[dict[str, Any]]:
    host = os.getenv("KUBERNETES_SERVICE_HOST")
    port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    if not host:
        raise KubernetesReadError(
            "kubernetes_unavailable",
            "In-cluster Kubernetes API environment is unavailable.",
        )

    try:
        with open(TOKEN_PATH, encoding="utf-8") as token_file:
            token = token_file.read().strip()
        tls_context = ssl.create_default_context(cafile=CA_PATH)
    except OSError as exc:
        raise KubernetesReadError(
            "kubernetes_authentication_unavailable",
            "Could not read the in-cluster service account credentials.",
        ) from exc

    path = (
        f"{api_prefix}/namespaces/{quote(namespace, safe='')}/"
        f"{quote(resource, safe='')}"
    )
    request = Request(
        f"https://{host}:{port}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(
            request, timeout=API_TIMEOUT_SECONDS, context=tls_context
        ) as response:
            result = json.load(response)
    except HTTPError as exc:
        if exc.code == 403:
            raise KubernetesReadError(
                "permission_denied",
                f"Permission denied listing {resource} in namespace {namespace}.",
            ) from exc
        if exc.code == 404:
            raise KubernetesReadError(
                "resource_not_found",
                f"The Kubernetes API resource {resource} was not found.",
            ) from exc
        raise KubernetesReadError(
            "kubernetes_api_error",
            f"Kubernetes API returned HTTP {exc.code} listing {resource}.",
        ) from exc
    except (TimeoutError, URLError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise KubernetesReadError(
                "kubernetes_api_timeout",
                f"Listing {resource} exceeded {API_TIMEOUT_SECONDS:g} seconds.",
            ) from exc
        raise KubernetesReadError(
            "kubernetes_unavailable",
            f"Could not connect to the Kubernetes API while listing {resource}.",
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise KubernetesReadError(
            "kubernetes_api_error",
            f"Kubernetes API returned an unreadable response for {resource}.",
        ) from exc

    return result.get("items", [])


def _compact(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    result: dict[str, Any] = {
        "kind": kind,
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "createdAt": metadata.get("creationTimestamp"),
        "labels": metadata.get("labels", {}),
    }

    if kind == "Pod":
        result.update(
            phase=status.get("phase"),
            nodeName=spec.get("nodeName"),
            podIP=status.get("podIP"),
            containers=[
                {
                    "name": container.get("name"),
                    "restartCount": container.get("restartCount"),
                    "state": next(iter(container.get("state", {})), None),
                    "reason": next(
                        iter(container.get("state", {}).values()), {}
                    ).get("reason"),
                    "lastTermination": {
                        "reason": (
                            container.get("lastState", {}).get("terminated", {}).get("reason")
                        ),
                        "exitCode": (
                            container.get("lastState", {}).get("terminated", {}).get("exitCode")
                        ),
                        "timestamp": (
                            container.get("lastState", {}).get("terminated", {}).get("finishedAt")
                        ),
                    },
                }
                for container in status.get("containerStatuses", [])
            ],
        )
    elif kind in {"Deployment", "ReplicaSet", "StatefulSet", "DaemonSet"}:
        result.update(
            desiredReplicas=spec.get("replicas"),
            readyReplicas=status.get("readyReplicas"),
            availableReplicas=status.get("availableReplicas"),
            updatedReplicas=status.get("updatedReplicas"),
        )
    elif kind == "Service":
        result.update(
            type=spec.get("type"),
            clusterIP=spec.get("clusterIP"),
            ports=spec.get("ports", []),
        )
    elif kind == "Endpoints":
        result["subsets"] = [
            {
                "addresses": [
                    address.get("ip") for address in subset.get("addresses", [])
                ],
                "notReadyAddresses": [
                    address.get("ip")
                    for address in subset.get("notReadyAddresses", [])
                ],
                "ports": subset.get("ports", []),
            }
            for subset in item.get("subsets", [])
        ]
    elif kind == "ConfigMap":
        data = item.get("data", {})
        binary_data = item.get("binaryData", {})
        result.update(
            dataKeys=sorted(data),
            binaryDataKeys=sorted(binary_data),
            immutable=item.get("immutable"),
        )
    elif kind == "PersistentVolumeClaim":
        result.update(
            phase=status.get("phase"),
            storageClassName=spec.get("storageClassName"),
            accessModes=spec.get("accessModes", []),
            capacity=status.get("capacity"),
        )
    elif kind == "Event":
        result.update(
            eventType=item.get("type"),
            reason=item.get("reason"),
            message=item.get("message"),
            involvedObject=item.get("involvedObject"),
            count=item.get("count"),
            lastTimestamp=item.get("lastTimestamp") or item.get("eventTime"),
        )
    elif kind in {"Job", "CronJob"}:
        result.update(
            active=status.get("active"),
            succeeded=status.get("succeeded"),
            failed=status.get("failed"),
            lastScheduleTime=status.get("lastScheduleTime"),
            lastSuccessfulTime=status.get("lastSuccessfulTime"),
        )
    elif kind == "Ingress":
        result.update(
            ingressClassName=spec.get("ingressClassName"),
            hosts=[
                rule.get("host")
                for rule in spec.get("rules", [])
                if rule.get("host")
            ],
            addresses=status.get("loadBalancer", {}).get("ingress", []),
        )
    elif kind == "NetworkPolicy":
        result.update(
            podSelector=spec.get("podSelector"),
            policyTypes=spec.get("policyTypes", []),
        )

    return result


def _read_resource(
    namespace: str, key: str, api_prefix: str, resource: str, kind: str
) -> tuple[str, list[dict[str, Any]], dict[str, str] | None]:
    try:
        items = _api_list(namespace, api_prefix, resource)
        return key, [_compact(kind, item) for item in items], None
    except KubernetesReadError as exc:
        return key, [], {"code": exc.code, "message": exc.message}


def _inspect_namespace(namespace: str) -> dict[str, Any]:
    if (
        not isinstance(namespace, str)
        or not NAMESPACE_PATTERN.fullmatch(namespace)
    ):
        return {
            "error": {
                "code": "invalid_namespace",
                "message": "namespace must be a valid Kubernetes namespace name.",
            }
        }

    allowed = _allowed_namespaces()
    if "*" not in allowed and namespace not in allowed:
        return {
            "error": {
                "code": "namespace_not_allowed",
                "message": "The requested namespace is not in the server allowlist.",
                "allowedNamespaces": sorted(allowed),
            }
        }

    resources: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, dict[str, str]] = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(_read_resource, namespace, *resource_spec)
            for resource_spec in SUPPORTED_RESOURCES
        ]
        for future in futures:
            key, items, error = future.result()
            resources[key] = items
            if error:
                errors[key] = error

    return {
        "namespace": namespace,
        "resources": resources,
        "errors": errors,
    }



def inspect_namespace(namespace: str) -> dict[str, Any]:
    """Return a compact snapshot of supported resources in an allowlisted namespace."""
    return _inspect_namespace(namespace)



def register(mcp: Any) -> None:
    """Register Kubernetes tools on the shared FastMCP application."""
    mcp.tool()(inspect_namespace)