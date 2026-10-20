"""In-cluster test transport and helpers for the Istio mesh integration tests.

All HTTP calls run *inside* the cluster via the Kubernetes exec API with a fixed Python
script and JSON arguments built from test constants (never from manifest values). Three
callers exist, each with a distinct mesh identity:

* ``orders``  - exec into an orders-service container: the authorized workload identity
  ``spiffe://cluster.local/ns/platformforge-dev/sa/orders-service``.
* ``MESH_CLIENT`` - a sidecar-injected test pod running as the ``default`` ServiceAccount:
  a valid mesh identity that is *not* authorized to call inventory-service.
* ``PLAIN_CLIENT`` - a test pod without a sidecar: no mesh identity, plaintext only.

The two test pods are long-lived fixtures. They are created if absent and reused, never
deleted (matching the integration suite's no-delete rule). They have no Service, no API
token, no published ports, and comply with the namespace's `restricted` Pod Security level.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections import Counter
from pathlib import Path

import pytest
import yaml
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream

from platformforge.kubecontext import KubeClients
from tests.conftest import REPO_ROOT
from tests.integration.conftest import NAMESPACE

MESH_CLIENT = "pf-test-client-mesh"
PLAIN_CLIENT = "pf-test-client-plain"
TEST_PART_OF = "platformforge-integration-tests"
CLIENT_IMAGE = "platformforge/orders-service:0.2.0"  # already on the node; has python3.13
PYTHON = "/usr/bin/python3.13"

INVENTORY_HOST = "inventory-service.platformforge-dev.svc.cluster.local"
INVENTORY_URL = f"http://{INVENTORY_HOST}:8080"
ORDERS_URL = "http://orders-service.platformforge-dev.svc.cluster.local:8080"
ORDERS_PRINCIPAL = "spiffe://cluster.local/ns/platformforge-dev/sa/orders-service"
INVENTORY_PRINCIPAL = "spiffe://cluster.local/ns/platformforge-dev/sa/inventory-service"
DEFAULT_PRINCIPAL = "spiffe://cluster.local/ns/platformforge-dev/sa/default"

MESH_CONFIG_DIR = REPO_ROOT / "deploy" / "istio" / "platformforge-dev"
# The only Istio objects these tests may write: reviewed files, in this namespace only.
MESH_KINDS = {
    "PeerAuthentication": ("security.istio.io", "v1", "peerauthentications"),
    "AuthorizationPolicy": ("security.istio.io", "v1", "authorizationpolicies"),
    "DestinationRule": ("networking.istio.io", "v1", "destinationrules"),
    "VirtualService": ("networking.istio.io", "v1", "virtualservices"),
    "Telemetry": ("telemetry.istio.io", "v1", "telemetries"),
}
MESH_FILES = {
    "peer-authentication.yaml",
    "inventory-authorization.yaml",
    "inventory-destinationrule.yaml",
    "inventory-virtualservice-stable.yaml",
    "inventory-virtualservice-canary.yaml",
    "telemetry-tracing.yaml",
}

# Runs inside the target container. argv[1] is a JSON spec; prints one JSON document.
_CLIENT_SCRIPT = r"""
import json, socket, sys, time, urllib.error, urllib.request
spec = json.loads(sys.argv[1])
results = []
if spec["mode"] == "raw":
    r = {"connected": False}
    try:
        s = socket.create_connection((spec["host"], spec["port"]), timeout=spec["timeout"])
        r["connected"] = True
        s.sendall(spec["payload"].encode())
        s.settimeout(spec["timeout"])
        data = s.recv(4096)
        r["received"] = data.decode("latin-1")
        s.close()
    except Exception as e:
        r["error"] = type(e).__name__ + ": " + str(e)
    print(json.dumps(r))
    sys.exit(0)
keep = set(spec.get("keep_headers", []))
for i in range(spec["count"]):
    rid = spec["rid_prefix"] if spec["count"] == 1 else "%s-%d" % (spec["rid_prefix"], i)
    req = urllib.request.Request(spec["url"], method=spec["method"],
                                 headers={"X-Request-ID": rid})
    r = {"rid": rid}
    try:
        with urllib.request.urlopen(req, timeout=spec["timeout"]) as resp:
            r["status"], body, headers = resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        r["status"], body, headers = e.code, e.read(), e.headers
    except Exception as e:
        r["status"], body, headers = None, b"", {}
        r["error"] = type(e).__name__ + ": " + str(e)
    r["headers"] = {k.lower(): v for k, v in dict(headers).items() if k.lower() in keep}
    if spec["bodies"]:
        r["body"] = body.decode("utf-8", "replace")[: spec.get("body_limit", 2000)]
    results.append(r)
print(json.dumps(results))
"""


def _client_pod(name: str, mesh: bool) -> client.V1Pod:
    return client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=NAMESPACE,
            labels={
                "app.kubernetes.io/name": name,
                "app.kubernetes.io/part-of": TEST_PART_OF,
                "sidecar.istio.io/inject": "true" if mesh else "false",
            },
        ),
        spec=client.V1PodSpec(
            automount_service_account_token=False,
            enable_service_links=False,
            termination_grace_period_seconds=1,
            security_context=client.V1PodSecurityContext(
                run_as_non_root=True,
                seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
            ),
            containers=[
                client.V1Container(
                    name="client",
                    image=CLIENT_IMAGE,
                    image_pull_policy="IfNotPresent",
                    command=[PYTHON, "-c", "import time\nwhile True:\n    time.sleep(3600)"],
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": "10m", "memory": "32Mi"},
                        limits={"cpu": "200m", "memory": "64Mi"},
                    ),
                    security_context=client.V1SecurityContext(
                        allow_privilege_escalation=False,
                        read_only_root_filesystem=True,
                        run_as_non_root=True,
                        capabilities=client.V1Capabilities(drop=["ALL"]),
                    ),
                )
            ],
        ),
    )


def _pod_ready(pod: client.V1Pod) -> bool:
    return any(c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or []))


def ensure_client_pod(kube: KubeClients, name: str, mesh: bool, timeout: float = 120) -> str:
    try:
        pod = kube.core.read_namespaced_pod(name, NAMESPACE)
        if (pod.metadata.labels or {}).get("app.kubernetes.io/part-of") != TEST_PART_OF:
            pytest.fail(f"pod {name!r} exists but is not a PlatformForge test fixture")
    except ApiException as exc:
        if exc.status != 404:
            raise
        kube.core.create_namespaced_pod(NAMESPACE, _client_pod(name, mesh))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pod = kube.core.read_namespaced_pod(name, NAMESPACE)
        if _pod_ready(pod):
            return name
        time.sleep(2)
    pytest.fail(f"test client pod {name!r} not Ready after {timeout}s")


def pods_of(kube: KubeClients, selector: str) -> list[client.V1Pod]:
    pods = kube.core.list_namespaced_pod(NAMESPACE, label_selector=selector).items
    return sorted(
        (p for p in pods if p.metadata.deletion_timestamp is None and _pod_ready(p)),
        key=lambda p: p.metadata.name,
    )


def first_pod(kube: KubeClients, service: str) -> str:
    pods = pods_of(kube, f"app.kubernetes.io/name={service}")
    if not pods:
        pytest.fail(f"no Ready pods for {service}")
    return pods[0].metadata.name


def _exec(kube: KubeClients, pod: str, container: str, command: list[str], timeout=300) -> str:
    resp = stream(
        kube.core.connect_get_namespaced_pod_exec,
        pod,
        NAMESPACE,
        container=container,
        command=command,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
        _preload_content=False,
    )
    resp.run_forever(timeout=timeout)
    out, err = resp.read_stdout(), resp.read_stderr()
    code = resp.returncode
    resp.close()
    if code not in (0, None):
        raise AssertionError(f"exec in {pod}/{container} exited {code}: {err.strip()}")
    return out


def http(
    kube: KubeClients,
    pod: str,
    container: str,
    url: str,
    *,
    count: int = 1,
    method: str = "GET",
    rid_prefix: str = "it",
    timeout: float = 5,
    bodies: bool = True,
    body_limit: int = 2000,
    keep_headers: tuple[str, ...] = ("x-request-id", "server", "x-envoy-upstream-service-time"),
) -> list[dict]:
    spec = {
        "mode": "http",
        "url": url,
        "count": count,
        "method": method,
        "rid_prefix": rid_prefix,
        "timeout": timeout,
        "bodies": bodies,
        "body_limit": body_limit,
        "keep_headers": list(keep_headers),
    }
    out = _exec(kube, pod, container, [PYTHON, "-c", _CLIENT_SCRIPT, json.dumps(spec)])
    return json.loads(out)


def http_one(kube: KubeClients, pod: str, container: str, url: str, **kwargs) -> dict:
    result = http(kube, pod, container, url, count=1, **kwargs)[0]
    if result.get("body"):
        with contextlib.suppress(ValueError):
            result["json"] = json.loads(result["body"])
    return result


def raw_plaintext(kube: KubeClients, pod: str, container: str, host: str, port: int) -> dict:
    payload = f"GET /inventory/widget HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
    spec = {"mode": "raw", "host": host, "port": port, "payload": payload, "timeout": 5}
    return json.loads(_exec(kube, pod, container, [PYTHON, "-c", _CLIENT_SCRIPT, json.dumps(spec)]))


def envoy(kube: KubeClients, pod: str, path: str) -> str:
    """GET an Envoy admin path through pilot-agent in the istio-proxy container."""
    return _exec(kube, pod, "istio-proxy", ["pilot-agent", "request", "GET", path])


# Istio's standard metric labels, in the order Envoy flattens them into the stat name.
# Values may contain dots (SPIFFE IDs, hostnames, versions) or be empty.
_METRIC_LABELS = [
    "source_workload",
    "source_canonical_service",
    "source_canonical_revision",
    "source_workload_namespace",
    "source_principal",
    "source_app",
    "source_version",
    "source_cluster",
    "destination_workload",
    "destination_workload_namespace",
    "destination_principal",
    "destination_app",
    "destination_version",
    "destination_service",
    "destination_canonical_service",
    "destination_canonical_revision",
    "destination_service_name",
    "destination_service_namespace",
    "destination_cluster",
    "request_protocol",
    "response_code",
    "grpc_response_status",
    "response_flags",
    "connection_security_policy",
]
_REQUESTS_TOTAL = re.compile(
    r"^istiocustom\.istio_requests_total\.reporter\.(?P<reporter>source|destination)\."
    + r"\.".join(rf"{label}(?:\.(?P<{label}>.*?))?" for label in _METRIC_LABELS)
    + r": (?P<value>[0-9]+)$"
)


def istio_requests(kube: KubeClients, pods: list[str]) -> list[dict]:
    """Parse Istio's standard istio_requests_total counters from the given sidecars."""
    rows = []
    for pod in pods:
        for line in envoy(kube, pod, "stats").splitlines():
            match = _REQUESTS_TOTAL.match(line)
            if match:
                row = {k: v or "" for k, v in match.groupdict().items()}
                rows.append(row | {"value": int(match["value"]), "pod": pod})
    return rows


def inventory_counts_by_version(kube: KubeClients, orders_pods: list[str]) -> Counter:
    """(destination_version, response_code) -> requests, as reported by orders' sidecars."""
    counts: Counter = Counter()
    for row in istio_requests(kube, orders_pods):
        if row["reporter"] == "source" and row["destination_service"] == INVENTORY_HOST:
            counts[(row["destination_version"], row["response_code"])] += row["value"]
    return counts


def route_weights(kube: KubeClients, pod: str) -> dict[str, int]:
    """Weighted clusters Envoy currently uses for inventory-service:8080 on this sidecar."""
    dump = json.loads(envoy(kube, pod, "config_dump?resource=dynamic_route_configs"))
    weights: dict[str, int] = {}
    for config in dump.get("configs", []):
        route_config = config.get("route_config", {})
        if route_config.get("name") != "8080":
            continue
        for vhost in route_config.get("virtual_hosts", []):
            if not any(d.startswith(INVENTORY_HOST) for d in vhost.get("domains", [])):
                continue
            for route in vhost.get("routes", []):
                action = route.get("route", {})
                for cluster in action.get("weighted_clusters", {}).get("clusters", []):
                    weights[cluster["name"]] = cluster.get("weight", 0)
                if "cluster" in action:
                    weights[action["cluster"]] = 100
    # Istio omits zero-weight destinations; normalize so callers can compare either way.
    return {name: weight for name, weight in weights.items() if weight}


def subset_cluster(subset: str) -> str:
    return f"outbound|8080|{subset}|{INVENTORY_HOST}"


def wait_for_weights(kube: KubeClients, pods: list[str], expected: dict[str, int], timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        seen = {pod: route_weights(kube, pod) for pod in pods}
        if all(w == expected for w in seen.values()):
            return seen
        if time.monotonic() > deadline:
            pytest.fail(f"sidecars did not converge on {expected}: {seen}")
        time.sleep(1)


def envoy_access_logs(kube: KubeClients, pod: str, since_seconds: int = 300) -> list[dict]:
    text = kube.core.read_namespaced_pod_log(
        pod, NAMESPACE, container="istio-proxy", since_seconds=since_seconds
    )
    entries = []
    for line in text.splitlines():
        if line.startswith("{"):
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    return entries


def apply_mesh_config(kube: KubeClients, filename: str) -> dict:
    """Create or replace one reviewed Istio object from deploy/istio/platformforge-dev."""
    if filename not in MESH_FILES:
        raise AssertionError(f"{filename!r} is not an allowlisted mesh config file")
    body = yaml.safe_load((MESH_CONFIG_DIR / filename).read_text())
    group, version, plural = MESH_KINDS[body["kind"]]
    if body["metadata"]["namespace"] != NAMESPACE:
        raise AssertionError(f"{filename} targets namespace {body['metadata']['namespace']!r}")
    api = client.CustomObjectsApi(kube.core.api_client)
    name = body["metadata"]["name"]
    try:
        existing = api.get_namespaced_custom_object(group, version, NAMESPACE, plural, name)
    except ApiException as exc:
        if exc.status != 404:
            raise
        return api.create_namespaced_custom_object(group, version, NAMESPACE, plural, body)
    body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
    return api.replace_namespaced_custom_object(group, version, NAMESPACE, plural, name, body)


def get_mesh_object(kube: KubeClients, kind: str, name: str) -> dict | None:
    group, version, plural = MESH_KINDS[kind]
    api = client.CustomObjectsApi(kube.core.api_client)
    try:
        return api.get_namespaced_custom_object(group, version, NAMESPACE, plural, name)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def mesh_file(filename: str) -> Path:
    return MESH_CONFIG_DIR / filename
