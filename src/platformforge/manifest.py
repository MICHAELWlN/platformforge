"""PlatformForge service manifest schema and loader.

A service manifest is a PlatformForge-specific document. It is intentionally not a
Kubernetes object: arbitrary Kubernetes fields are rejected, and the platform decides
how the manifest is rendered into Kubernetes resources.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

API_VERSION = "platformforge.dev/v1alpha1"
KIND = "ServiceManifest"

# Only these namespaces may ever be targeted. Changing this list is a code change that
# must go through review; it is deliberately not configurable at runtime.
ALLOWED_NAMESPACES: frozenset[str] = frozenset({"platformforge-dev"})

MAX_MANIFEST_BYTES = 64 * 1024
MAX_REPLICAS = 5
MAX_CPU_MILLICORES = 2000
MAX_MEMORY_BYTES = 2 * 1024**3
MAX_ENV_VARS = 50

_DNS1035_LABEL = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
_DNS1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CREDENTIAL_HINT = re.compile(
    r"PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIAL", re.IGNORECASE
)
_HTTP_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{0,255}$")
_IMAGE = re.compile(
    r"^(?:[A-Za-z0-9.-]+(?::[0-9]{1,5})?/)?"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?"
    r"(?:@(?P<digest>sha256:[a-f0-9]{64}))?$"
)
_CPU = re.compile(r"^(?:[0-9]{1,6}m|[0-9]{1,3}(?:\.[0-9]{1,3})?)$")
_MEMORY = re.compile(r"^[0-9]{1,12}(?:Ki|Mi|Gi)?$")
_MEMORY_FACTORS = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}


class ManifestError(Exception):
    """Raised when a manifest cannot be loaded or fails validation."""


def validate_namespace(namespace: str) -> str:
    """Return ``namespace`` if it is a valid, allowlisted namespace name."""
    if len(namespace) > 63 or not _DNS1123_LABEL.fullmatch(namespace):
        raise ValueError(f"namespace {namespace!r} is not a valid DNS-1123 label")
    if namespace not in ALLOWED_NAMESPACES:
        allowed = ", ".join(sorted(ALLOWED_NAMESPACES))
        raise ValueError(f"namespace {namespace!r} is not allowlisted (allowed: {allowed})")
    return namespace


def cpu_to_millicores(value: str) -> int:
    if not _CPU.fullmatch(value):
        raise ValueError(f"invalid CPU quantity {value!r} (examples: '250m', '1', '0.5')")
    if value.endswith("m"):
        return int(value[:-1])
    try:
        return int(Decimal(value) * 1000)
    except InvalidOperation as exc:  # pragma: no cover - regex already guarantees format
        raise ValueError(f"invalid CPU quantity {value!r}") from exc


def memory_to_bytes(value: str) -> int:
    if not _MEMORY.fullmatch(value):
        raise ValueError(f"invalid memory quantity {value!r} (examples: '128Mi', '1Gi')")
    for suffix, factor in _MEMORY_FACTORS.items():
        if value.endswith(suffix):
            return int(value[: -len(suffix)]) * factor
    return int(value)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Metadata(_Model):
    name: str
    namespace: str

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if len(value) > 63 or not _DNS1035_LABEL.fullmatch(value):
            raise ValueError(
                "must be a DNS-1035 label: lowercase alphanumerics or '-', start with a "
                "letter, end with an alphanumeric, at most 63 characters"
            )
        return value

    @field_validator("namespace")
    @classmethod
    def _check_namespace(cls, value: str) -> str:
        return validate_namespace(value)


class EnvVar(_Model):
    name: str
    value: str = Field(max_length=4096)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if len(value) > 128 or not _ENV_NAME.fullmatch(value):
            raise ValueError("must be a C identifier ([A-Za-z_][A-Za-z0-9_]*), max 128 chars")
        if _CREDENTIAL_HINT.search(value):
            raise ValueError(
                "looks like a credential; plaintext credentials are not allowed in manifests"
            )
        return value


class ResourceQuantities(_Model):
    cpu: str
    memory: str

    @field_validator("cpu", "memory", mode="before")
    @classmethod
    def _int_to_str(cls, value: object) -> object:
        # YAML parses `cpu: 1` as an int; accept it as the quantity "1".
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value

    @field_validator("cpu")
    @classmethod
    def _check_cpu(cls, value: str) -> str:
        cpu_to_millicores(value)
        return value

    @field_validator("memory")
    @classmethod
    def _check_memory(cls, value: str) -> str:
        memory_to_bytes(value)
        return value

    @property
    def millicores(self) -> int:
        return cpu_to_millicores(self.cpu)

    @property
    def memory_bytes(self) -> int:
        return memory_to_bytes(self.memory)


class Resources(_Model):
    requests: ResourceQuantities
    limits: ResourceQuantities

    @model_validator(mode="after")
    def _check_bounds(self) -> Resources:
        if self.requests.millicores <= 0 or self.requests.memory_bytes <= 0:
            raise ValueError("resource requests must be greater than zero")
        if self.requests.millicores > self.limits.millicores:
            raise ValueError("requests.cpu must not exceed limits.cpu")
        if self.requests.memory_bytes > self.limits.memory_bytes:
            raise ValueError("requests.memory must not exceed limits.memory")
        if self.limits.millicores > MAX_CPU_MILLICORES:
            raise ValueError(f"limits.cpu must not exceed {MAX_CPU_MILLICORES}m")
        if self.limits.memory_bytes > MAX_MEMORY_BYTES:
            raise ValueError("limits.memory must not exceed 2Gi")
        return self


class HttpProbe(_Model):
    path: str = "/"
    initial_delay_seconds: int = Field(default=0, ge=0, le=300, alias="initialDelaySeconds")
    period_seconds: int = Field(default=10, ge=1, le=300, alias="periodSeconds")
    timeout_seconds: int = Field(default=1, ge=1, le=60, alias="timeoutSeconds")
    failure_threshold: int = Field(default=3, ge=1, le=30, alias="failureThreshold")

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        if not _HTTP_PATH.fullmatch(value):
            raise ValueError("must be an absolute HTTP path using [A-Za-z0-9._~/-] only")
        return value


class Probes(_Model):
    readiness: HttpProbe
    liveness: HttpProbe


class ServiceSpec(_Model):
    image: str
    replicas: int = Field(default=1, ge=1, le=MAX_REPLICAS)
    port: int = Field(ge=1024, le=65535)
    env: list[EnvVar] = Field(default_factory=list, max_length=MAX_ENV_VARS)
    resources: Resources
    probes: Probes

    @field_validator("image")
    @classmethod
    def _check_image(cls, value: str) -> str:
        match = _IMAGE.fullmatch(value) if len(value) <= 255 else None
        if match is None:
            raise ValueError("is not a valid container image reference")
        tag, digest = match.group("tag"), match.group("digest")
        if not tag and not digest:
            raise ValueError("must be pinned with an explicit tag or sha256 digest")
        if tag == "latest" and not digest:
            raise ValueError("must not use the mutable ':latest' tag")
        return value

    @field_validator("env")
    @classmethod
    def _check_env_unique(cls, value: list[EnvVar]) -> list[EnvVar]:
        names = [item.name for item in value]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate env var names: {', '.join(duplicates)}")
        return value


class ServiceManifest(_Model):
    api_version: Literal["platformforge.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["ServiceManifest"]
    metadata: Metadata
    spec: ServiceSpec

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def namespace(self) -> str:
        return self.metadata.namespace


def _format_validation_error(error: ValidationError) -> str:
    lines = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "<root>"
        message = item["msg"].removeprefix("Value error, ")
        lines.append(f"  {location}: {message}")
    return "manifest validation failed:\n" + "\n".join(lines)


def parse_manifest(text: str) -> ServiceManifest:
    """Parse and validate manifest YAML text."""
    try:
        # Reject anchors/aliases before constructing objects to rule out alias-expansion
        # ("billion laughs") documents.
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent):
                raise ManifestError("YAML aliases are not allowed in manifests")
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a single YAML mapping")
    try:
        return ServiceManifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(_format_validation_error(exc)) from exc


def load_manifest(path: Path) -> ServiceManifest:
    """Load and validate a manifest file."""
    if not path.is_file():
        raise ManifestError(f"manifest file not found: {path}")
    size = path.stat().st_size
    if size > MAX_MANIFEST_BYTES:
        raise ManifestError(f"manifest is {size} bytes; the limit is {MAX_MANIFEST_BYTES}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError("manifest must be UTF-8 text") from exc
    return parse_manifest(text)
