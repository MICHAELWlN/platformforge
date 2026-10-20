"""Milestone 3: Istio sidecars, workload identity, STRICT mTLS, authorization, canary
release, and canary failure/recovery on the local kind cluster.

Prerequisites: Istio 1.31.2 installed per docs/ISTIO.md, images loaded (`make images
kind-load`), and test_services_kind.py run first (same pytest session, alphabetical order)
or the services already deployed.

What these tests write (all in platformforge-dev): PlatformForge deployments via the CLI,
the reviewed Istio objects in deploy/istio/platformforge-dev/, and two long-lived test
client pods. Nothing is deleted. The module leaves the mesh in its stable configuration:
STRICT mTLS, inventory AuthorizationPolicy, 100% of inventory traffic on v0.1.0, and a
healthy v0.2.0 canary receiving no traffic.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from kubernetes import client

from platformforge.kubecontext import KubeClients
from tests.conftest import REPO_ROOT
from tests.integration import mesh
from tests.integration.conftest import NAMESPACE, run_cli

pytestmark = pytest.mark.integration

ISTIO_VERSION = "1.31.2"
MANIFESTS = REPO_ROOT / "services" / "manifests"
INVENTORY = MANIFESTS / "inventory-service.yaml"
CANARY = MANIFESTS / "inventory-service-v0.2.0.yaml"
CANARY_FAULTY = MANIFESTS / "incidents" / "inventory-service-v0.2.0-faulty.yaml"
ORDERS = MANIFESTS / "orders-service.yaml"

INVENTORY_SELECTOR = "app.kubernetes.io/name=inventory-service"
ORDERS_SELECTOR = "app.kubernetes.io/name=orders-service"
STABLE = {mesh.subset_cluster("v0-1-0"): 100}
CANARY_SPLIT = {mesh.subset_cluster("v0-1-0"): 90, mesh.subset_cluster("v0-2-0"): 10}

# Canary sample: N requests, p = 0.10 -> mean 100, sd = sqrt(N p (1-p)) = 9.49.
# [60, 140] is about +/-4.2 sd (false-failure odds ~3e-5). Pod-proportional kube-proxy
# balancing over 2 x v0.1.0 + 1 x v0.2.0 pods would instead give ~333 (1/3).
CANARY_N = 1000
CANARY_BOUNDS = (60, 140)


@pytest.fixture(scope="module")
def k(kube: KubeClients) -> KubeClients:
    istiod = kube.apps.read_namespaced_deployment("istiod", "istio-system")
    if not (istiod.status.available_replicas or 0) >= 1:
        pytest.fail("istiod is not available; install Istio per docs/ISTIO.md")
    mesh.ensure_client_pod(kube, mesh.MESH_CLIENT, mesh=True)
    mesh.ensure_client_pod(kube, mesh.PLAIN_CLIENT, mesh=False)
    return kube


def rid(prefix: str = "mesh") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def pod_names(k: KubeClients, selector: str) -> list[str]:
    return [p.metadata.name for p in mesh.pods_of(k, selector)]


def orders_pod(k: KubeClients) -> str:
    return mesh.first_pod(k, "orders-service")


def call_orders(k: KubeClients, count: int, prefix: str) -> list[dict]:
    """The full chain: test client -> orders-service -> (orders' sidecar routes) inventory."""
    return mesh.http(
        k,
        mesh.MESH_CLIENT,
        "client",
        f"{mesh.ORDERS_URL}/orders/check/widget",
        count=count,
        rid_prefix=prefix,
        bodies=True,
    )


def find_log(k: KubeClients, selector: str, request_id: str, timeout: float = 20) -> list[dict]:
    deadline = time.monotonic() + timeout
    while True:
        hits = [
            entry | {"_pod": pod}
            for pod in pod_names(k, selector)
            for entry in mesh.envoy_access_logs(k, pod, since_seconds=300)
            if entry.get("request_id") == request_id
        ]
        if hits or time.monotonic() > deadline:
            return hits
        time.sleep(1)


# --- Phase A: installation and sidecars ---------------------------------------------------


def test_a1_istio_control_plane_healthy(k):
    istiod = k.apps.read_namespaced_deployment("istiod", "istio-system")
    assert istiod.spec.template.spec.containers[0].image.endswith(f"/pilot:{ISTIO_VERSION}")
    assert istiod.status.available_replicas == istiod.spec.replicas
    cni = k.apps.read_namespaced_daemon_set("istio-cni-node", "istio-system")
    assert cni.spec.template.spec.containers[0].image.endswith(f"/install-cni:{ISTIO_VERSION}")
    assert cni.status.number_ready == cni.status.desired_number_scheduled >= 1


def test_a2_injection_enabled_only_for_platformforge_dev(k):
    injected = k.core.list_namespace(label_selector="istio-injection=enabled").items
    assert [ns.metadata.name for ns in injected] == [NAMESPACE]
    labels = k.core.read_namespace(NAMESPACE).metadata.labels
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"


@pytest.mark.parametrize("service", ["inventory-service", "orders-service", "demo-web"])
def test_a3_workloads_run_hardened_native_sidecars(k, service):
    pods = mesh.pods_of(k, f"app.kubernetes.io/name={service}")
    assert pods, f"no ready pods for {service}"
    for pod in pods:
        inits = {c.name: c for c in pod.spec.init_containers or []}
        # Istio CNI: no privileged istio-init container; the proxy is a native sidecar.
        assert "istio-init" not in inits
        proxy = inits["istio-proxy"]
        assert proxy.restart_policy == "Always"
        assert proxy.image.endswith(f"/proxyv2:{ISTIO_VERSION}")
        sc = proxy.security_context
        assert sc.run_as_non_root is True and sc.allow_privilege_escalation is False
        assert sc.capabilities.drop == ["ALL"] and not sc.capabilities.add
        assert sc.privileged in (None, False)
        statuses = {s.name: s for s in pod.status.init_container_statuses or []}
        assert statuses["istio-proxy"].ready is True
        assert all(c.ready for c in pod.status.container_statuses)


# --- Phase B: workload identity -----------------------------------------------------------


@pytest.mark.parametrize("service", ["inventory-service", "orders-service"])
def test_b1_dedicated_service_accounts(k, service):
    sa = k.core.read_namespaced_service_account(service, NAMESPACE)
    assert sa.metadata.labels["app.kubernetes.io/managed-by"] == "platformforge"
    assert sa.automount_service_account_token is False
    for pod in mesh.pods_of(k, f"app.kubernetes.io/name={service}"):
        assert pod.spec.service_account_name == service
        assert pod.spec.automount_service_account_token is False
        # The only projected token is Istio's, audience istio-ca (not the API server).
        audiences = [
            src.service_account_token.audience
            for vol in pod.spec.volumes or []
            if vol.projected
            for src in vol.projected.sources
            if src.service_account_token
        ]
        assert audiences == ["istio-ca"]


@pytest.mark.parametrize("service", ["inventory-service", "orders-service"])
def test_b2_service_accounts_have_no_api_permissions(k, service):
    authz = client.AuthorizationV1Api(k.core.api_client)
    user = f"system:serviceaccount:{NAMESPACE}:{service}"
    for verb, resource, group in [
        ("get", "pods", ""),
        ("list", "secrets", ""),
        ("get", "configmaps", ""),
        ("create", "pods", ""),
        ("list", "services", ""),
        ("list", "authorizationpolicies", "security.istio.io"),
    ]:
        review = authz.create_subject_access_review(
            client.V1SubjectAccessReview(
                spec=client.V1SubjectAccessReviewSpec(
                    user=user,
                    groups=["system:serviceaccounts", f"system:serviceaccounts:{NAMESPACE}"],
                    resource_attributes=client.V1ResourceAttributes(
                        namespace=NAMESPACE, verb=verb, resource=resource, group=group
                    ),
                )
            )
        )
        assert review.status.allowed is False, f"{service} may {verb} {resource}"
    rbac = client.RbacAuthorizationV1Api(k.core.api_client)
    bindings = [
        *rbac.list_namespaced_role_binding(NAMESPACE).items,
        *rbac.list_cluster_role_binding().items,
    ]
    for binding in bindings:
        for subject in binding.subjects or []:
            assert not (subject.kind == "ServiceAccount" and subject.name == service), (
                f"{binding.metadata.name} binds {service}"
            )


@pytest.mark.parametrize(
    ("service", "principal"),
    [
        ("inventory-service", mesh.INVENTORY_PRINCIPAL),
        ("orders-service", mesh.ORDERS_PRINCIPAL),
    ],
)
def test_b3_workload_certificates_carry_spiffe_identity(k, service, principal):
    for pod in pod_names(k, f"app.kubernetes.io/name={service}"):
        certs = json.loads(mesh.envoy(k, pod, "certs"))["certificates"]
        sans = {
            san["uri"]
            for entry in certs
            for chain in entry.get("cert_chain", [])
            for san in chain.get("subject_alt_names", [])
            if "uri" in san
        }
        assert sans == {principal}, f"{pod}: {sans}"


def test_b4_redeploy_preserves_identity_and_labels(k):
    before = {p.metadata.name for p in mesh.pods_of(k, ORDERS_SELECTOR)}
    result = run_cli("deploy", str(ORDERS), "--timeout", "180")
    assert result.exit_code == 0, result.output
    assert "ServiceAccount/orders-service: updated" in result.output
    dep = k.apps.read_namespaced_deployment("orders-service", NAMESPACE)
    assert dep.spec.template.spec.service_account_name == "orders-service"
    assert dep.spec.template.metadata.labels["app.kubernetes.io/name"] == "orders-service"
    # Re-applying an identical manifest does not roll the pods.
    assert {p.metadata.name for p in mesh.pods_of(k, ORDERS_SELECTOR)} == before


# --- Phase C: STRICT mTLS -----------------------------------------------------------------


def test_c1_strict_peer_authentication_is_namespace_scoped(k):
    mesh.apply_mesh_config(k, "peer-authentication.yaml")
    pa = mesh.get_mesh_object(k, "PeerAuthentication", "default")
    assert pa["spec"] == {"mtls": {"mode": "STRICT"}}
    api = client.CustomObjectsApi(k.core.api_client)
    mesh_wide = api.list_namespaced_custom_object(
        "security.istio.io", "v1", "istio-system", "peerauthentications"
    )["items"]
    assert mesh_wide == [], "no mesh-wide PeerAuthentication is expected"


@pytest.mark.parametrize("service", ["inventory-service", "orders-service"])
def test_c2_plaintext_from_non_mesh_client_is_rejected(k, service):
    for pod in mesh.pods_of(k, f"app.kubernetes.io/name={service}"):
        result = mesh.raw_plaintext(k, mesh.PLAIN_CLIENT, "client", pod.status.pod_ip, 8080)
        print(pod.metadata.name, result)
        # TCP reaches the sidecar (so this is not a network/routing failure) ...
        assert result["connected"] is True
        # ... but no HTTP response is ever produced for plaintext.
        assert not result.get("received", "").startswith("HTTP/")
    via_service = mesh.http_one(
        k,
        mesh.PLAIN_CLIENT,
        "client",
        f"http://{service}.{NAMESPACE}.svc.cluster.local:8080/healthz",
    )
    print("via Service:", via_service)
    assert via_service["status"] is None and "Reset" in via_service["error"]


def test_c3_same_pods_accept_mtls_from_a_mesh_client(k):
    # Control for c2: the destination pods that reset plaintext accept mTLS via the Service.
    request_id = rid("mtls-control")
    result = mesh.http_one(
        k, mesh.MESH_CLIENT, "client", f"{mesh.ORDERS_URL}/healthz", rid_prefix=request_id
    )
    assert result["status"] == 200
    [entry] = find_log(k, ORDERS_SELECTOR, request_id)
    assert entry["downstream_tls_version"] == "TLSv1.3"
    assert entry["downstream_peer_uri_san"] == mesh.DEFAULT_PRINCIPAL


def test_c4_mesh_client_bypassing_service_discovery_is_rejected(k):
    # A raw pod IP is unknown to the caller's sidecar, so it is forwarded as plaintext via
    # PassthroughCluster (no mTLS) - and STRICT rejects it like any other plaintext peer.
    pod = mesh.pods_of(k, ORDERS_SELECTOR)[0]
    target = f"{pod.status.pod_ip}:8080"
    request_id = rid("pod-ip")
    result = mesh.http_one(
        k, mesh.MESH_CLIENT, "client", f"http://{target}/healthz", rid_prefix=request_id
    )
    assert result["status"] == 503
    [entry] = find_log(k, f"app.kubernetes.io/name={mesh.MESH_CLIENT}", request_id)
    print("client sidecar:", entry)
    assert entry["authority"] == target
    assert entry["upstream_cluster"].startswith("PassthroughCluster")
    assert entry["upstream_tls_version"] is None
    assert entry["response_flags"] == "UC"


def test_c5_orders_to_inventory_is_mutual_tls_on_both_ends(k):
    request_id = rid("mtls")
    result = mesh.http_one(
        k,
        orders_pod(k),
        "orders-service",
        f"{mesh.INVENTORY_URL}/inventory/widget",
        rid_prefix=request_id,
    )
    assert result["status"] == 200
    client_side = find_log(k, ORDERS_SELECTOR, request_id)
    server_side = find_log(k, INVENTORY_SELECTOR, request_id)
    print("orders outbound:", client_side)
    print("inventory inbound:", server_side)
    assert len(client_side) == 1 and len(server_side) == 1
    assert client_side[0]["upstream_tls_version"] == "TLSv1.3"
    assert client_side[0]["upstream_peer_uri_san"] == mesh.INVENTORY_PRINCIPAL
    assert server_side[0]["downstream_tls_version"] == "TLSv1.3"
    assert server_side[0]["downstream_peer_uri_san"] == mesh.ORDERS_PRINCIPAL


def test_c6_inventory_reports_every_orders_request_as_mutual_tls(k):
    rows = [
        r
        for r in mesh.istio_requests(k, pod_names(k, INVENTORY_SELECTOR))
        if r["reporter"] == "destination" and r["source_principal"] == mesh.ORDERS_PRINCIPAL
    ]
    assert rows and sum(r["value"] for r in rows) > 0
    assert {r["connection_security_policy"] for r in rows} == {"mutual_tls"}


# --- Phase D: authorization ---------------------------------------------------------------


def test_d1_policy_admits_exactly_the_orders_identity(k):
    mesh.apply_mesh_config(k, "inventory-authorization.yaml")
    policy = mesh.get_mesh_object(k, "AuthorizationPolicy", "inventory-service")
    spec = policy["spec"]
    assert spec["selector"] == {"matchLabels": {"app.kubernetes.io/name": "inventory-service"}}
    assert spec["action"] == "ALLOW"
    assert len(spec["rules"]) == 1
    rule = spec["rules"][0]
    assert rule["from"] == [
        {"source": {"principals": ["cluster.local/ns/platformforge-dev/sa/orders-service"]}}
    ]
    assert rule["to"] == [
        {"operation": {"methods": ["GET"], "paths": ["/inventory/*"], "ports": ["8080"]}}
    ]
    api = client.CustomObjectsApi(k.core.api_client)
    names = [
        p["metadata"]["name"]
        for p in api.list_namespaced_custom_object(
            "security.istio.io", "v1", NAMESPACE, "authorizationpolicies"
        )["items"]
    ]
    assert names == ["inventory-service"], "no other (possibly broader) policies expected"
    time.sleep(3)  # allow istiod to push the RBAC filter to the inventory sidecars


def test_d2_authorized_identity_is_allowed(k):
    direct = mesh.http_one(
        k, orders_pod(k), "orders-service", f"{mesh.INVENTORY_URL}/inventory/widget"
    )
    assert direct["status"] == 200 and direct["json"]["item_id"] == "widget"
    chain = call_orders(k, 20, rid("authz-chain"))
    assert {r["status"] for r in chain} == {200}


def test_d3_other_mesh_identity_is_denied(k):
    request_id = rid("authz-deny")
    result = mesh.http_one(
        k,
        mesh.MESH_CLIENT,
        "client",
        f"{mesh.INVENTORY_URL}/inventory/widget",
        rid_prefix=request_id,
    )
    print(result)
    assert result["status"] == 403
    assert result["body"] == "RBAC: access denied"
    [entry] = find_log(k, INVENTORY_SELECTOR, request_id)
    print("inventory inbound:", entry)
    # The caller *was* authenticated over mTLS; it is denied because of who it is.
    assert entry["downstream_peer_uri_san"] == mesh.DEFAULT_PRINCIPAL
    assert entry["downstream_tls_version"] == "TLSv1.3"
    assert entry["response_code_details"].startswith("rbac_access_denied")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/version"),
        ("GET", "/healthz"),
        ("POST", "/inventory/widget"),
        ("DELETE", "/inventory/widget"),
    ],
)
def test_d4_authorized_identity_is_limited_to_its_operation(k, method, path):
    result = mesh.http_one(
        k, orders_pod(k), "orders-service", f"{mesh.INVENTORY_URL}{path}", method=method
    )
    assert result["status"] == 403, result


def test_d5_policy_is_not_namespace_wide(k):
    # Same namespace, same unauthorized identity: orders-service is not covered by the
    # inventory policy, so the test client still reaches it.
    result = mesh.http_one(k, mesh.MESH_CLIENT, "client", f"{mesh.ORDERS_URL}/healthz")
    assert result["status"] == 200


def test_d6_health_checks_still_work_under_policy(k):
    for pod in mesh.pods_of(k, INVENTORY_SELECTOR):
        container = next(c for c in pod.spec.containers if c.name == "inventory-service")
        # Istio rewrote the kubelet probes to the sidecar agent (port 15020).
        assert container.readiness_probe.http_get.port == 15020
        assert container.readiness_probe.http_get.path == "/app-health/inventory-service/readyz"
        status = next(s for s in pod.status.container_statuses if s.name == "inventory-service")
        assert status.ready is True and status.restart_count == 0
    result = run_cli("status", str(INVENTORY))
    assert "Health:     HEALTHY" in result.output, result.output


# --- Phase E: canary release --------------------------------------------------------------


def test_e1_deploy_canary_alongside_stable(k):
    mesh.apply_mesh_config(k, "inventory-destinationrule.yaml")
    mesh.apply_mesh_config(k, "inventory-virtualservice-stable.yaml")
    mesh.wait_for_weights(k, pod_names(k, ORDERS_SELECTOR), STABLE)
    result = run_cli("deploy", str(CANARY), "--timeout", "180")
    print(result.output)
    assert result.exit_code == 0, result.output
    assert "Deployment/inventory-service-v0-2-0:" in result.output
    assert "Rollout complete: 1/1 available" in result.output
    v1 = k.apps.read_namespaced_deployment("inventory-service-v0-1-0", NAMESPACE)
    assert v1.status.available_replicas == 2
    slices = client.DiscoveryV1Api(k.core.api_client)
    endpoints = [
        e
        for s in slices.list_namespaced_endpoint_slice(
            NAMESPACE, label_selector="kubernetes.io/service-name=inventory-service"
        ).items
        for e in s.endpoints or []
        if e.conditions.ready
    ]
    assert len(endpoints) == 3  # 2 x v0.1.0 + 1 x v0.2.0 behind one Service


def test_e2_stable_route_sends_nothing_to_ready_canary(k):
    """Control: a Ready canary endpoint gets 0% unless the VirtualService says otherwise."""
    orders = pod_names(k, ORDERS_SELECTOR)
    before = mesh.inventory_counts_by_version(k, orders)
    results = call_orders(k, 200, rid("stable"))
    after = mesh.inventory_counts_by_version(k, orders)
    delta = after - before
    print("stable delta:", dict(delta))
    assert {r["status"] for r in results} == {200}
    assert delta[("v0.2.0", "200")] == 0
    assert delta[("v0.1.0", "200")] == 200


def test_e3_weighted_canary_split(k):
    mesh.apply_mesh_config(k, "inventory-virtualservice-canary.yaml")
    orders = pod_names(k, ORDERS_SELECTOR)
    mesh.wait_for_weights(k, orders, CANARY_SPLIT)
    before = mesh.inventory_counts_by_version(k, orders)
    results = call_orders(k, CANARY_N, rid("canary"))
    after = mesh.inventory_counts_by_version(k, orders)
    delta = after - before
    stable, canary = delta[("v0.1.0", "200")], delta[("v0.2.0", "200")]
    print(
        f"canary split over {CANARY_N}: v0.1.0={stable} ({stable / CANARY_N:.1%}) "
        f"v0.2.0={canary} ({canary / CANARY_N:.1%}) other={dict(delta)}"
    )
    assert {r["status"] for r in results} == {200}
    assert stable + canary == CANARY_N
    assert CANARY_BOUNDS[0] <= canary <= CANARY_BOUNDS[1]


# --- Phase F: canary failure and recovery -------------------------------------------------


def test_f1_faulty_canary_is_observable(k):
    result = run_cli("deploy", str(CANARY_FAULTY), "--timeout", "180")
    assert result.exit_code == 0, result.output
    orders = pod_names(k, ORDERS_SELECTOR)
    mesh.wait_for_weights(k, orders, CANARY_SPLIT)
    before = mesh.inventory_counts_by_version(k, orders)
    results = call_orders(k, 500, rid("incident"))
    delta = mesh.inventory_counts_by_version(k, orders) - before
    statuses = {}
    for r in results:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    print(f"during incident: client statuses={statuses} mesh delta={dict(delta)}")
    failed = [r for r in results if r["status"] != 200]
    assert 20 <= len(failed) <= 85  # ~10% of 500 (mean 50, sd 6.7)
    assert {r["status"] for r in failed} == {502}
    assert all('"inventory_bad_status"' in r["body"] for r in failed)
    # Every failure came from v0.2.0; v0.1.0 served only successes.
    assert delta[("v0.2.0", "500")] == len(failed)
    assert delta[("v0.2.0", "200")] == 0
    assert delta[("v0.1.0", "200")] == 500 - len(failed)
    canary_pod = pod_names(k, "app.kubernetes.io/version=v0.2.0," + INVENTORY_SELECTOR)[0]
    logs = k.core.read_namespaced_pod_log(canary_pod, NAMESPACE, container="inventory-service")
    assert logs.count('"injected failure"') >= len(failed)


def test_f2_shift_all_traffic_to_stable_recovers(k):
    orders = pod_names(k, ORDERS_SELECTOR)
    start = time.monotonic()
    mesh.apply_mesh_config(k, "inventory-virtualservice-stable.yaml")
    mesh.wait_for_weights(k, orders, STABLE)
    print(f"sidecars converged on stable routing in {time.monotonic() - start:.1f}s")
    before = mesh.inventory_counts_by_version(k, orders)
    results = call_orders(k, 300, rid("recovered"))
    delta = mesh.inventory_counts_by_version(k, orders) - before
    print("after remediation:", dict(delta))
    assert {r["status"] for r in results} == {200}
    assert delta[("v0.1.0", "200")] == 300
    assert sum(v for (version, _), v in delta.items() if version == "v0.2.0") == 0


def test_f3_restore_healthy_canary_with_stable_routing(k):
    result = run_cli("deploy", str(CANARY), "--timeout", "180")
    assert result.exit_code == 0, result.output
    canary_pods = mesh.pods_of(k, "app.kubernetes.io/version=v0.2.0," + INVENTORY_SELECTOR)
    assert len(canary_pods) == 1
    env = {e.name: e.value for e in canary_pods[0].spec.containers[0].env}
    assert env["INVENTORY_FAILURE_PERCENT"] == "0"
    # The fixed canary serves correctly (checked in-pod; the mesh routes it 0% for now).
    check = mesh.http_one(
        k,
        canary_pods[0].metadata.name,
        "inventory-service",
        "http://127.0.0.1:8080/inventory/widget",
    )
    assert check["status"] == 200
    vs = mesh.get_mesh_object(k, "VirtualService", "inventory-service")
    weights = {d["destination"]["subset"]: d["weight"] for d in vs["spec"]["http"][0]["route"]}
    assert weights == {"v0-1-0": 100, "v0-2-0": 0}
    assert {r["status"] for r in call_orders(k, 50, rid("final"))} == {200}
