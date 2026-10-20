from __future__ import annotations

from platformforge.resources import (
    build_deployment,
    build_namespace,
    build_service,
    label_selector,
    to_dict,
)


def test_deployment_metadata_and_selector(manifest):
    dep = to_dict(build_deployment(manifest))
    assert dep["apiVersion"] == "apps/v1"
    assert dep["kind"] == "Deployment"
    assert dep["metadata"]["name"] == "demo-web"
    assert dep["metadata"]["namespace"] == "platformforge-dev"
    labels = {"app.kubernetes.io/name": "demo-web", "app.kubernetes.io/managed-by": "platformforge"}
    assert dep["metadata"]["labels"] == labels
    assert dep["spec"]["selector"]["matchLabels"] == labels
    assert dep["spec"]["template"]["metadata"]["labels"] == labels
    assert dep["spec"]["replicas"] == 2
    assert dep["spec"]["strategy"]["rollingUpdate"] == {"maxSurge": 1, "maxUnavailable": 0}


def test_deployment_probes(manifest):
    container = to_dict(build_deployment(manifest))["spec"]["template"]["spec"]["containers"][0]
    assert container["readinessProbe"] == {
        "httpGet": {"path": "/", "port": "http", "scheme": "HTTP"},
        "initialDelaySeconds": 2,
        "periodSeconds": 5,
        "timeoutSeconds": 2,
        "failureThreshold": 3,
    }
    assert container["livenessProbe"]["initialDelaySeconds"] == 10
    assert container["livenessProbe"]["periodSeconds"] == 10
    assert container["ports"] == [{"containerPort": 8080, "name": "http", "protocol": "TCP"}]


def test_deployment_resources(manifest):
    container = to_dict(build_deployment(manifest))["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"] == {
        "requests": {"cpu": "50m", "memory": "32Mi"},
        "limits": {"cpu": "200m", "memory": "64Mi"},
    }


def test_deployment_security_defaults(manifest):
    pod = to_dict(build_deployment(manifest))["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert "hostNetwork" not in pod
    assert "hostPID" not in pod
    csc = pod["containers"][0]["securityContext"]
    assert csc == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "runAsNonRoot": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert pod["volumes"] == [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]


def test_deployment_env(manifest):
    container = to_dict(build_deployment(manifest))["spec"]["template"]["spec"]["containers"][0]
    assert container["env"] == [{"name": "LOG_LEVEL", "value": "info"}]


def test_service_is_cluster_ip_only(manifest):
    svc = to_dict(build_service(manifest))
    assert svc["spec"]["type"] == "ClusterIP"
    assert svc["spec"]["ports"] == [
        {"name": "http", "port": 8080, "protocol": "TCP", "targetPort": "http"}
    ]
    assert "externalIPs" not in svc["spec"]
    assert "loadBalancerIP" not in svc["spec"]
    assert svc["spec"]["selector"]["app.kubernetes.io/name"] == "demo-web"


def test_namespace_enforces_restricted_pod_security():
    ns = to_dict(build_namespace("platformforge-dev"))
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    assert ns["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "platformforge"


def test_label_selector(manifest):
    assert label_selector(manifest) == (
        "app.kubernetes.io/name=demo-web,app.kubernetes.io/managed-by=platformforge"
    )
