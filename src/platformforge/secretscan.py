"""Reject credential material in files that would be published to Git.

The scan covers tracked files and untracked files that are not gitignored.
It does not read Terraform state, tfvars, or the image lock, because those
paths are ignored and are also rejected by name if they ever become tracked.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

BLOCKED_NAMES = frozenset(
    {
        "terraform.tfstate",
        "terraform.tfvars",
        "image-lock.yaml",
        ".env",
        "kubeconfig",
    }
)
BLOCKED_SUFFIXES = (
    ".tfstate",
    ".tfstate.backup",
    ".auto.tfvars",
    ".pem",
    ".kubeconfig",
)
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----")),
    ("service-account-key", re.compile(r'"private_key"\s*:')),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
)


def blocked_name(path: Path) -> str | None:
    name = path.name
    if name in BLOCKED_NAMES or name.startswith(".env"):
        return name
    lowered = name.lower()
    if "kubeconfig" in lowered or lowered.endswith(BLOCKED_SUFFIXES):
        return name
    return None


def content_findings(text: str) -> list[str]:
    return [label for label, pattern in PATTERNS if pattern.search(text)]


def publishable_files(root: Path) -> list[Path]:
    """Return tracked files plus untracked files Git would not ignore."""
    chunks: list[str] = []
    for args in (
        ["git", "ls-files", "-z"],
        ["git", "ls-files", "-z", "--others", "--exclude-standard"],
    ):
        completed = subprocess.run(  # noqa: S603
            args,
            cwd=root,
            check=True,
            capture_output=True,
        )
        chunks.append(completed.stdout.decode("utf-8"))
    names = [name for name in "".join(chunks).split("\0") if name]
    return [root / name for name in names]


def scan_files(paths: list[Path]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        blocked = blocked_name(path)
        if blocked is not None:
            findings.append(f"{path}: blocked filename {blocked}")
            continue
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        findings.extend(f"{path}: {label}" for label in content_findings(text))
    return findings


def main(argv: list[str] | None = None) -> int:
    root = Path(argv[0]) if argv else Path.cwd()
    findings = scan_files(publishable_files(root))
    if findings:
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("secret scan clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
