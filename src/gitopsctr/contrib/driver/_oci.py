"""Shared OCI registry and credential helpers for contributed drivers."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

from gitopsctr.driver import DriverError, JsonObject, ReconciliationContext

from ._common import require_strings, run

OCI_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
FRONTEND_ARCHIVE = "frontend-bundle.tar.gz"
OCI_REPOSITORY_RE = re.compile(
    r"^(?P<registry>(?:localhost|[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::[1-9][0-9]{0,4})?)/"
    r"(?P<path>[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*)$"
)
PRIVATE_ECR_REGISTRY_RE = re.compile(
    r"^[0-9]{12}\.dkr\.ecr(?:-fips)?\."
    r"(?P<region>[a-z]{2}(?:-gov)?-[a-z]+-[0-9])\.amazonaws\.com(?:\.cn)?$"
)


@dataclass(frozen=True)
class RegistryCredentials:
    username: str
    password: str


class ArtifactUnitIdentity(TypedDict):
    name: str
    driver: str
    inputHashVersion: int
    inputHash: str
    sourceRevision: str


def artifact_unit_identity(context: ReconciliationContext, input_hash: str, contract: str) -> ArtifactUnitIdentity:
    require_strings(context.unit, ("name", "driver"), contract)
    return {
        "name": cast(str, context.unit["name"]),
        "driver": cast(str, context.unit["driver"]),
        "inputHashVersion": 1,
        "inputHash": input_hash,
        "sourceRevision": context.source_revision,
    }


CredentialProviderValidator = Callable[[str, JsonObject], None]
CredentialProviderLoader = Callable[[str, JsonObject], RegistryCredentials]


@dataclass(frozen=True)
class CredentialProvider:
    validate: CredentialProviderValidator
    load: CredentialProviderLoader


@dataclass(frozen=True)
class ResolvedCredentialProvider:
    provider: CredentialProvider
    configuration: JsonObject


def driver_status(status: str, message: str) -> None:
    print(f"    {status:<7} {message}", file=sys.stderr, flush=True)


def repository_registry(repository: str) -> str:
    match = OCI_REPOSITORY_RE.fullmatch(repository)
    if match is None:
        raise DriverError(
            f"oci-images repositories must be full, lowercase, tag-free registry references: {repository!r}"
        )
    registry = match.group("registry")
    if registry != "localhost" and "." not in registry and ":" not in registry:
        raise DriverError(f"oci-images repository is not registry-qualified: {repository!r}")
    return registry


def validate_aws_ecr_provider(registry: str, configuration: JsonObject) -> None:
    unsupported = set(configuration) - {"type"}
    if unsupported:
        raise DriverError(f"aws-ecr credentialProvider has unsupported fields: {', '.join(sorted(unsupported))}")
    if PRIVATE_ECR_REGISTRY_RE.fullmatch(registry) is None:
        raise DriverError(f"aws-ecr requires a private ECR registry, got {registry!r}")


def aws_ecr_credentials(registry: str, configuration: JsonObject) -> RegistryCredentials:
    validate_aws_ecr_provider(registry, configuration)
    match = PRIVATE_ECR_REGISTRY_RE.fullmatch(registry)
    assert match is not None
    region = match.group("region")
    if profile := os.environ.get("AWS_PROFILE"):
        credential_source = f"AWS profile {profile!r}"
    elif os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE"):
        credential_source = "AWS web identity environment"
    elif os.environ.get("AWS_ACCESS_KEY_ID"):
        credential_source = "AWS environment credentials"
    else:
        credential_source = "AWS default credential chain"
    driver_status("AUTH", f"{registry}: aws-ecr via {credential_source} (region {region})")
    password = run("aws", "ecr", "get-login-password", "--region", region, capture=True).stdout
    if not password:
        raise DriverError(f"aws-ecr returned an empty password for {registry}")
    return RegistryCredentials(username="AWS", password=password)


CREDENTIAL_PROVIDERS: dict[str, CredentialProvider] = {
    "aws-ecr": CredentialProvider(validate=validate_aws_ecr_provider, load=aws_ecr_credentials),
}


def resolve_credential_provider(configuration: object, registries: set[str]) -> ResolvedCredentialProvider | None:
    if configuration is None:
        return None
    if not isinstance(configuration, dict):
        raise DriverError("oci-images credentialProvider must be an object")
    provider_type = configuration.get("type")
    if not isinstance(provider_type, str) or provider_type not in CREDENTIAL_PROVIDERS:
        raise DriverError(f"oci-images uses an unknown credential provider: {provider_type!r}")
    configuration = cast(JsonObject, configuration)
    provider = CREDENTIAL_PROVIDERS[provider_type]
    for registry in registries:
        provider.validate(registry, configuration)
    return ResolvedCredentialProvider(provider, configuration)


def docker_cli_plugins() -> Path | None:
    configured_root = os.environ.get("DOCKER_CONFIG")
    docker_config = Path(configured_root) if configured_root else Path.home() / ".docker"
    plugins = docker_config / "cli-plugins"
    return plugins.resolve() if plugins.is_dir() else None


@contextmanager
def registry_authentication(
    provider: ResolvedCredentialProvider | None,
    registries: set[str],
) -> Iterator[dict[str, str] | None]:
    if provider is None:
        for registry in sorted(registries):
            driver_status("AUTH", f"{registry}: existing Docker credentials or anonymous access")
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="gitopsctr-docker-") as docker_config:
        if plugins := docker_cli_plugins():
            (Path(docker_config) / "cli-plugins").symlink_to(plugins, target_is_directory=True)
        docker_environment = os.environ | {"DOCKER_CONFIG": docker_config}
        for registry in sorted(registries):
            credentials = provider.provider.load(registry, provider.configuration)
            driver_status(
                "LOGIN",
                f"{registry}: Docker login as {credentials.username} via password stdin (isolated temporary config)",
            )
            run(
                "docker",
                "login",
                "--username",
                credentials.username,
                "--password-stdin",
                registry,
                env=docker_environment,
                input_text=credentials.password,
            )
        yield docker_environment


@contextmanager
def oras_authentication(
    provider: ResolvedCredentialProvider | None,
    registries: set[str],
) -> Iterator[Path | None]:
    if provider is None:
        for registry in sorted(registries):
            driver_status("AUTH", f"{registry}: existing ORAS credentials or anonymous access")
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="gitopsctr-oras-") as directory:
        registry_config = Path(directory) / "config.json"
        for registry in sorted(registries):
            credentials = provider.provider.load(registry, provider.configuration)
            driver_status(
                "LOGIN",
                f"{registry}: ORAS login as {credentials.username} via password stdin (isolated temporary config)",
            )
            run(
                "oras",
                "login",
                "--registry-config",
                str(registry_config),
                "--username",
                credentials.username,
                "--password-stdin",
                registry,
                input_text=credentials.password,
            )
        yield registry_config


def oras_registry_args(registry_config: Path | None) -> list[str]:
    return ["--registry-config", str(registry_config)] if registry_config else []


def oras_digest(repository: str, tag: str, registry_config: Path | None = None) -> str | None:
    reference = f"{repository}:{tag}"
    result = subprocess.run(
        ["oras", "resolve", *oras_registry_args(registry_config), reference],
        check=False,
        capture_output=True,
        text=True,
    )
    digest = result.stdout.strip()
    if result.returncode == 0:
        if not OCI_DIGEST_RE.fullmatch(digest):
            raise DriverError(f"OCI registry returned an invalid digest for {reference}")
        return digest
    error = result.stderr.strip()
    if any(marker in error.lower() for marker in ("manifest unknown", "not found", "name unknown", "tag invalid")):
        return None
    raise DriverError(error or f"could not inspect {reference}")
