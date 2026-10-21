"""Static checks for the GitHub Actions workflows. They do not call GitHub."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
PIN = re.compile(r"^[0-9a-f]{40}$")


def _load(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _uses(document: dict) -> list[str]:
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "uses" and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(document)
    return found


def test_actions_are_pinned_to_commits() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        for uses in _uses(_load(path.name)):
            action, _, ref = uses.partition("@")
            assert action
            assert PIN.fullmatch(ref), uses


def _trigger(document: dict) -> dict:
    # PyYAML 1.1 parses the workflow key "on" as boolean true.
    trigger = document.get(True, document.get("on"))
    assert isinstance(trigger, dict)
    return trigger


def test_ci_has_no_cloud_credentials() -> None:
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    document = _load("ci.yml")
    assert document["permissions"] == {"contents": "read"}
    assert "id-token" not in text
    assert "secrets." not in text
    assert "pull_request" in _trigger(document)
    assert document["concurrency"]["cancel-in-progress"] is True
    assert document["jobs"]["secrets"]["steps"][-1]["run"] == "python -m platformforge.secretscan"
    assert document["jobs"]["unit-tests"]["steps"][-1]["run"].endswith('not gke"')


def test_delivery_cannot_run_from_a_pull_request() -> None:
    document = _load("delivery.yml")
    text = (WORKFLOWS / "delivery.yml").read_text(encoding="utf-8")
    assert list(_trigger(document)) == ["workflow_dispatch"]
    assert "pull_request" not in _trigger(document)
    assert document["concurrency"]["cancel-in-progress"] is False
    assert document["jobs"]["publish"]["permissions"]["id-token"] == "write"
    assert "GCP_DELIVERY_ENABLED" in text
    assert "secrets." not in text
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in text
    assert "refuse-apply" in text
    assert "65532:65532" in text
    assert 'test "$status" -eq 2' in text
    for job in document["jobs"].values():
        condition = job["if"]
        assert "workflow_dispatch" in condition
        assert "refs/heads/main" in condition
        assert job["environment"] == "gke-delivery"


def test_github_delivery_terraform_is_off_by_default() -> None:
    text = (ROOT / "infra/terraform/variables.tf").read_text(encoding="utf-8")
    assert 'variable "enable_github_delivery"' in text
    assert "default     = false" in text.split('variable "enable_github_delivery"', 1)[1][:400]
    module = (ROOT / "infra/terraform/github_delivery.tf").read_text(encoding="utf-8")
    assert "count  = var.enable_github_delivery ? 1 : 0" in module


def test_zonal_cluster_condition_matches_live_resource_name() -> None:
    text = (ROOT / "infra/terraform/modules/github_delivery/main.tf").read_text(encoding="utf-8")
    assert "google_service_account_key" not in text
    assert "attribute.repository/*" not in text
    assert "attribute.repository/${var.github_repository}" in text
    assert "container.googleapis.com/Cluster" in text
    assert "projects/${var.project_id}/zones/${var.zone}/clusters/${var.cluster_name}" in text
    assert "locations/${var.zone}" not in text


def test_delivery_role_cannot_delete_or_expose_services() -> None:
    text = (ROOT / "deploy/gke/delivery-rbac.yaml").read_text(encoding="utf-8")
    assert "delete" not in text
    assert "LoadBalancer" not in text
    assert "platformforge-dev" in text
