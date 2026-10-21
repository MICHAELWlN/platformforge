from pathlib import Path

from platformforge.secretscan import blocked_name, content_findings, scan_files

ROOT = Path(__file__).resolve().parents[2]


def test_repository_publishable_files_are_clean() -> None:
    from platformforge.secretscan import publishable_files

    assert scan_files(publishable_files(ROOT)) == []


def test_private_key_and_blocked_names_are_rejected(tmp_path: Path) -> None:
    key = tmp_path / "notes.txt"
    marker = "-----BEGIN " + "PRIVATE KEY-----"
    key.write_text(marker + "\nabc\n", encoding="utf-8")
    state = tmp_path / "terraform.tfstate"
    state.write_text("{}", encoding="utf-8")
    example = tmp_path / "terraform.tfvars.example"
    example.write_text('project_id = "example"\n', encoding="utf-8")
    findings = scan_files([key, state, example])
    assert any("private-key" in item for item in findings)
    assert any("terraform.tfstate" in item for item in findings)
    assert not any("tfvars.example" in item for item in findings)


def test_token_patterns() -> None:
    assert content_findings("token ghp_" + ("a" * 20)) == ["github-token"]
    assert content_findings("roles/iam.workloadIdentityUser") == []
    assert blocked_name(Path("deploy/gke/image-lock.yaml")) == "image-lock.yaml"
    assert blocked_name(Path("deploy/gke/image-lock.example.yaml")) is None
