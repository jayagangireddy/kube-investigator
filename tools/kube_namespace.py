"""Read-only Kubernetes namespace inventory MCP tool."""

from __future__ import annotations

import json
import base64
import logging
import os
import re
import ssl
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API_TIMEOUT_SECONDS = float(os.getenv("KUBERNETES_API_TIMEOUT_SECONDS", "4"))
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
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


def _read_resource(
    namespace: str, key: str, api_prefix: str, resource: str, kind: str
) -> tuple[str, list[dict[str, Any]], dict[str, str] | None]:
    try:
        return key, _api_list(namespace, api_prefix, resource), None
    except KubernetesReadError as exc:
        return key, [], {"code": exc.code, "message": exc.message}


def _inspect_namespace(namespace: str) -> dict[str, Any]:
    if not isinstance(namespace, str) or not NAMESPACE_PATTERN.fullmatch(namespace):
        return {"error": {"code": "invalid_namespace", "message": "namespace must be a valid Kubernetes namespace name."}}
    allowed = _allowed_namespaces()
    if "*" not in allowed and namespace not in allowed:
        return {"error": {"code": "namespace_not_allowed", "message": "The requested namespace is not in the server allowlist.", "allowedNamespaces": sorted(allowed)}}

    resources: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, dict[str, str]] = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(_read_resource, namespace, *spec) for spec in SUPPORTED_RESOURCES]
        for future in futures:
            key, items, error = future.result()
            resources[key] = items
            if error:
                errors[key] = error

    evidence: list[dict[str, Any]] = []
    evidence_by_resource: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}
    findings: list[dict[str, Any]] = []
    investigation_id = base64.urlsafe_b64encode(uuid.uuid4().bytes).decode("ascii").rstrip("=")
    collected = datetime.now(timezone.utc).isoformat()

    def finding(kind: str, item: dict[str, Any]) -> dict[str, Any]:
        return {"kind": kind, "resource": item.get("metadata", {}).get("name"), "evidence": []}

    def add(target: dict[str, Any], item: dict[str, Any], path: str, label: str | None = None) -> None:
        value: Any = item
        for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
            if part.startswith("["):
                index = int(part[1:-1])
                if not isinstance(value, list) or index >= len(value):
                    return
                value = value[index]
            elif isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return
        resource_name = item.get("metadata", {}).get("name")
        resource_namespace = item.get("metadata", {}).get("namespace", namespace)
        key = (target["kind"], resource_namespace, resource_name)
        entry = evidence_by_resource.get(key)
        if entry is None:
            entry = {"id": f"{investigation_id}-{len(evidence) + 1}", "tool": "inspect_namespace",
                     "kind": target["kind"], "namespace": resource_namespace, "resource": resource_name,
                     "observedAt": collected, "fields": {}}
            evidence_by_resource[key] = entry
            evidence.append(entry)
        entry["fields"][path] = value
        if entry["id"] not in target["evidence"]:
            target["evidence"].append(entry["id"])

    for pod in resources["pods"]:
        target = finding("Pod", pod)
        add(target, pod, "status.phase", "phase")
        statuses = pod.get("status", {}).get("containerStatuses", [])
        specs = pod.get("spec", {}).get("containers", [])
        for index, status in enumerate(statuses):
            prefix = f"status.containerStatuses[{index}]"
            for field in ("name", "ready", "restartCount", "image"):
                add(target, pod, f"{prefix}.{field}")
            for state in ("waiting", "running", "terminated"):
                if state in status.get("state", {}):
                    # The state name is a field in the API, not an inferred diagnosis.
                    if state == "running":
                        add(target, pod, f"{prefix}.state.running")
                    else:
                        for field in ("reason", "message", "exitCode", "signal", "finishedAt"):
                            add(target, pod, f"{prefix}.state.{state}.{field}")
            for field in ("reason", "exitCode", "signal", "finishedAt"):
                add(target, pod, f"{prefix}.lastState.terminated.{field}")
        for index, container in enumerate(specs):
            prefix = f"spec.containers[{index}]"
            # Include spec image and limits even when no status has appeared yet.
            corresponding = next((status for status in statuses if status.get("name") == container.get("name")), None)
            if corresponding is None:
                add(target, pod, f"{prefix}.name")
            if corresponding is None or "image" not in corresponding:
                add(target, pod, f"{prefix}.image")
            for limit in ("cpu", "memory", "ephemeral-storage"):
                add(target, pod, f"{prefix}.resources.limits.{limit}")
        for index, condition in enumerate(pod.get("status", {}).get("conditions", [])):
            if condition.get("status") == "False" and condition.get("reason"):
                for field in ("type", "status", "reason", "message"):
                    add(target, pod, f"status.conditions[{index}].{field}")
        findings.append(target)

    for deployment in resources["deployments"]:
        target = finding("Deployment", deployment)
        for path, label in (("spec.replicas", "desiredReplicas"), ("status.readyReplicas", "readyReplicas"),
                            ("status.availableReplicas", "availableReplicas"), ("status.updatedReplicas", "updatedReplicas")):
            add(target, deployment, path, label)
        findings.append(target)

    for key, kind in (("statefulsets", "StatefulSet"), ("daemonsets", "DaemonSet"), ("replicasets", "ReplicaSet")):
        for workload in resources[key]:
            status = workload.get("status", {})
            desired = workload.get("spec", {}).get("replicas")
            if kind == "DaemonSet":
                desired = status.get("desiredNumberScheduled")
                ready = status.get("numberReady")
            else:
                ready = status.get("readyReplicas")
            if desired is None or ready is None or desired != ready:
                target = finding(kind, workload)
                for path in ("spec.replicas", "status.readyReplicas", "status.availableReplicas", "status.updatedReplicas",
                             "status.desiredNumberScheduled", "status.numberReady", "status.numberUnavailable"):
                    add(target, workload, path)
                findings.append(target)

    for endpoint in resources["endpoints"]:
        target = finding("Endpoints", endpoint)
        for i, subset in enumerate(endpoint.get("subsets", [])):
            for j, address in enumerate(subset.get("notReadyAddresses", [])):
                add(target, endpoint, f"subsets[{i}].notReadyAddresses[{j}].ip")
                add(target, endpoint, f"subsets[{i}].notReadyAddresses[{j}].targetRef.name")
        if target["evidence"]:
            findings.append(target)

    # Relevant normal events explain starts, scheduling, image pulls, and back-off.
    relevant_normal = {"BackOff", "Pulled", "Pulling", "Scheduled", "Started", "Killing", "Created"}
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for event in resources["events"]:
        if event.get("type") != "Warning" and not (event.get("type") == "Normal" and event.get("reason") in relevant_normal):
            continue
        obj = event.get("involvedObject", {})
        key = (event.get("type"), event.get("reason"), event.get("message"), obj.get("kind"), obj.get("namespace"), obj.get("name"), obj.get("uid"), obj.get("fieldPath"))
        grouped.setdefault(key, []).append(event)
    for events in grouped.values():
        target = finding("Event", events[0])
        for path in ("type", "reason", "message", "involvedObject.name", "involvedObject.kind", "involvedObject.fieldPath"):
            add(target, events[0], path)
        target["sources"] = []
        for event in events:
            # Preserve each source's count and timestamp instead of inventing an aggregate.
            source = {"evidence": []}
            for path in ("count", "lastTimestamp", "eventTime", "series.count", "series.lastObservedTime"):
                add(source | {"kind": "Event"}, event, path)
            target["sources"].append(source)
        timestamp_candidates = [(event.get(field), source["evidence"][0])
                                for event, source in zip(events, target["sources"])
                                for field in ("lastTimestamp", "eventTime") if event.get(field) and source["evidence"]]
        timestamp_candidates.extend((event.get("series", {}).get("lastObservedTime"), source["evidence"][0])
                                    for event, source in zip(events, target["sources"])
                                    if event.get("series", {}).get("lastObservedTime") and source["evidence"])
        if timestamp_candidates:
            target["latestOccurrence"] = max(timestamp_candidates, key=lambda pair: pair[0])[1]
        findings.append(target)

    for pvc in resources["persistentvolumeclaims"]:
        if pvc.get("status", {}).get("phase") != "Bound":
            target = finding("PersistentVolumeClaim", pvc)
            for path in ("status.phase", "spec.storageClassName", "status.capacity.storage"):
                add(target, pvc, path)
            findings.append(target)
    for key, kind in (("jobs", "Job"), ("cronjobs", "CronJob")):
        for item in resources[key]:
            if item.get("status", {}).get("failed"):
                target = finding(kind, item)
                for path in ("status.active", "status.succeeded", "status.failed", "status.lastScheduleTime", "status.lastSuccessfulTime"):
                    add(target, item, path)
                findings.append(target)
    return {"namespace": namespace, "findings": findings, "evidence": evidence, "errors": errors}



def inspect_namespace(namespace: str) -> dict[str, Any]:
    """Return a compact snapshot of supported resources in an allowlisted namespace."""
    call_id = uuid.uuid4().hex[:12]
    started_at = time.monotonic()
    LOGGER.info("tool_call_started tool=inspect_namespace call_id=%s", call_id)
    try:
        result = _inspect_namespace(namespace)
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - started_at) * 1000)
        LOGGER.error(
            "tool_call_failed tool=inspect_namespace call_id=%s error_type=%s elapsed_ms=%d",
            call_id,
            type(exc).__name__,
            elapsed_ms,
        )
        raise

    result.setdefault("evidence", [])
    elapsed_ms = round((time.monotonic() - started_at) * 1000)
    top_level_error = result.get("error")
    if top_level_error:
        LOGGER.warning(
            "tool_call_rejected tool=inspect_namespace call_id=%s error_code=%s elapsed_ms=%d",
            call_id,
            top_level_error.get("code", "unknown"),
            elapsed_ms,
        )
    elif result.get("errors"):
        error_codes = sorted(
            {
                error.get("code", "unknown")
                for error in result["errors"].values()
            }
        )
        LOGGER.warning(
            "tool_call_completed_with_resource_errors tool=inspect_namespace call_id=%s "
            "error_codes=%s elapsed_ms=%d",
            call_id,
            ",".join(error_codes),
            elapsed_ms,
        )
    else:
        LOGGER.info(
            "tool_call_completed tool=inspect_namespace call_id=%s elapsed_ms=%d",
            call_id,
            elapsed_ms,
        )
    return result



def register(mcp: Any) -> None:
    """Register Kubernetes tools on the shared FastMCP application."""
    mcp.tool()(inspect_namespace)
