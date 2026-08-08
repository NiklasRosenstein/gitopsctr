"""Publish a frontend OCI bundle through S3 and CloudFront."""

from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypedDict, cast

from gitopsctr.contracts import (
    AuthoredSource,
    AwsEcrCredentialProvider,
    DesiredSource,
    MashumaroContract,
    MaterializationDocument,
    SchemaDocument,
    StrictModel,
    schema_url,
)
from gitopsctr.document import JsonObject
from gitopsctr.driver import (
    DriverError,
    PlanningCapability,
    PlanningContext,
    ReconciliationCapability,
    ReconciliationContext,
    ReconciliationResult,
    UnitDriver,
    UnitExecutionContext,
)
from gitopsctr.execution import CommandOutput

from ._common import require_strings, select_result_fields
from ._oci import (
    FRONTEND_ARCHIVE,
    OCI_DIGEST_RE,
    ResolvedCredentialProvider,
    oras_authentication,
    oras_registry_args,
    repository_registry,
    resolve_credential_provider,
)


class RuntimeAuth(TypedDict):
    mode: str
    issuer: str
    clientId: str


class RuntimeConfiguration(TypedDict):
    schema: int
    apiBase: str
    auth: RuntimeAuth


class FrontendInputs(TypedDict):
    bundle: str
    bucket: str
    distributionId: str
    url: str
    runtimeConfig: RuntimeConfiguration


class PublishedFrontend(TypedDict):
    sourceRevision: str
    path: str
    bundle: str
    artifactDigest: str
    runtimeConfigHash: str
    url: str


class FrontendResult(TypedDict):
    published: PublishedFrontend


@dataclass(frozen=True, kw_only=True)
class FrontendPull(StrictModel):
    credentialProvider: AwsEcrCredentialProvider | None = None


@dataclass(frozen=True, kw_only=True)
class FrontendAuthoredInputs(StrictModel):
    bundle: Any = None
    bucket: Any = None
    distributionId: Any = None
    url: Any = None
    runtimeConfig: Any = None


@dataclass(frozen=True, kw_only=True)
class RuntimeAuthModel(StrictModel):
    mode: Literal["cognito"]
    issuer: str
    clientId: str


@dataclass(frozen=True, kw_only=True)
class RuntimeConfigurationModel(StrictModel):
    schema: Literal[1]
    apiBase: str
    auth: RuntimeAuthModel


@dataclass(frozen=True, kw_only=True)
class FrontendDesiredInputs(StrictModel):
    bundle: str
    bucket: str
    distributionId: str
    url: str
    runtimeConfig: RuntimeConfigurationModel


@dataclass(frozen=True, kw_only=True)
class FrontendUnit(SchemaDocument):
    schema: Literal[1]
    name: str
    driver: Literal["frontend-s3-cloudfront"]
    source: AuthoredSource
    inputs: FrontendAuthoredInputs | None = None
    pull: FrontendPull | None = None


@dataclass(frozen=True, kw_only=True)
class FrontendDesiredUnit(SchemaDocument):
    schema: Literal[1]
    name: str
    driver: Literal["frontend-s3-cloudfront"]
    source: DesiredSource
    inputs: FrontendDesiredInputs | FrontendAuthoredInputs | None = None
    pull: FrontendPull | None = None
    resolvedInputs: dict[str, dict[str, str]] | None = None
    materialization: MaterializationDocument | None = None


@dataclass(frozen=True, kw_only=True)
class PublishedFrontendModel(StrictModel):
    sourceRevision: str
    path: str
    bundle: str
    artifactDigest: str
    runtimeConfigHash: str
    url: str


@dataclass(frozen=True, kw_only=True)
class FrontendResultModel(StrictModel):
    published: PublishedFrontendModel


@dataclass(frozen=True)
class FrontendRuntime:
    inputs: FrontendInputs
    repository: str
    artifact_digest: str
    credential_provider: ResolvedCredentialProvider | None
    runtime_bytes: bytes
    runtime_digest: str


def parse_oci_uri(uri: str) -> tuple[str, str]:
    repository, separator, digest = uri.rpartition("@")
    if not separator or not OCI_DIGEST_RE.fullmatch(digest):
        raise DriverError("frontend-s3-cloudfront bundle must be an immutable OCI digest URI")
    repository_registry(repository)
    return repository, digest


def safe_extract_bundle(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
                raise DriverError("frontend OCI artifact contains an unsafe archive entry")
        archive.extractall(destination, filter="data")


def runtime_configuration(inputs: JsonObject) -> RuntimeConfiguration:
    configuration = inputs.get("runtimeConfig")
    if not isinstance(configuration, dict) or set(configuration) != {"schema", "apiBase", "auth"}:
        raise DriverError("frontend-s3-cloudfront requires an exact runtimeConfig object")
    auth = configuration.get("auth")
    if configuration.get("schema") != 1 or not isinstance(auth, dict):
        raise DriverError("frontend-s3-cloudfront runtimeConfig must use schema 1")
    require_strings(configuration, ("apiBase",), "frontend runtimeConfig")
    require_strings(auth, ("mode", "issuer", "clientId"), "frontend runtimeConfig auth")
    if auth["mode"] != "cognito" or set(auth) != {"mode", "issuer", "clientId"}:
        raise DriverError("hosted frontend authentication must use the Cognito contract")
    return cast(RuntimeConfiguration, configuration)


class FrontendS3CloudfrontDriver(UnitDriver, PlanningCapability, ReconciliationCapability):
    api_version = "unit.gitopsctr.io/v1"
    kind = "FrontendS3Cloudfront"
    driver_name = "frontend-s3-cloudfront"
    version = 1
    schema_base_uri = schema_url("drivers/frontend-s3-cloudfront", version, "").removesuffix(".schema.json")
    unit_contract = MashumaroContract(
        FrontendUnit,
        schema_url("drivers/frontend-s3-cloudfront", version, "unit"),
    )
    desired_unit_contract = MashumaroContract(
        FrontendDesiredUnit,
        schema_url("drivers/frontend-s3-cloudfront", version, "desired-unit"),
    )
    result_contract = MashumaroContract(
        FrontendResultModel,
        schema_url("drivers/frontend-s3-cloudfront", version, "result"),
    )
    _select_semantic_result = staticmethod(select_result_fields("published"))

    @staticmethod
    def _runtime(context: UnitExecutionContext) -> FrontendRuntime:
        require_strings(
            context.inputs,
            ("bundle", "bucket", "distributionId", "url"),
            "frontend-s3-cloudfront inputs",
        )
        configuration = runtime_configuration(context.inputs)
        inputs = cast(FrontendInputs, context.inputs)
        repository, artifact_digest = parse_oci_uri(inputs["bundle"])
        publication = context.unit.get("pull", {})
        if not isinstance(publication, dict):
            raise DriverError("frontend-s3-cloudfront pull must be an object")
        registry = repository_registry(repository)
        credential_provider = resolve_credential_provider(publication.get("credentialProvider"), {registry})
        runtime_bytes = json.dumps(configuration, indent=2, sort_keys=True).encode() + b"\n"
        return FrontendRuntime(
            inputs=inputs,
            repository=repository,
            artifact_digest=artifact_digest,
            credential_provider=credential_provider,
            runtime_bytes=runtime_bytes,
            runtime_digest=f"sha256:{hashlib.sha256(runtime_bytes).hexdigest()}",
        )

    def plan(self, context: PlanningContext) -> None:
        self._runtime(context)

    def reconcile(self, context: ReconciliationContext) -> FrontendResult:
        runtime = self._runtime(context)
        inputs = runtime.inputs

        with tempfile.TemporaryDirectory(prefix="gitopsctr-frontend-deploy-") as directory:
            temporary = Path(directory)
            pulled = temporary / "pulled"
            distribution = temporary / "distribution"
            pulled.mkdir()
            distribution.mkdir()
            with oras_authentication(
                context.execution,
                runtime.credential_provider,
                {repository_registry(runtime.repository)},
            ) as registry_config:
                context.execution.run(
                    "oras",
                    "pull",
                    *oras_registry_args(registry_config),
                    "--output",
                    str(pulled),
                    inputs["bundle"],
                )
            archive = pulled / FRONTEND_ARCHIVE
            if not archive.is_file():
                raise DriverError(f"frontend OCI artifact did not contain {FRONTEND_ARCHIVE}")
            safe_extract_bundle(archive, distribution)
            runtime_path = distribution / "runtime-config.json"
            runtime_path.write_bytes(runtime.runtime_bytes)
            index_path = distribution / "index.html"
            if not index_path.is_file():
                raise DriverError("frontend OCI artifact did not contain index.html")
            index_text = index_path.read_text()
            context.execution.run("aws", "s3", "sync", str(distribution), f"s3://{inputs['bucket']}", "--delete")
            # Deterministic bundles give index.html a fixed timestamp, while hashed assets can
            # change without changing its byte length. Always replace the entry point.
            context.execution.run(
                "aws",
                "s3",
                "cp",
                str(index_path),
                f"s3://{inputs['bucket']}/index.html",
                "--cache-control",
                "no-cache",
                "--content-type",
                "text/html",
            )
            context.execution.run(
                "aws",
                "s3",
                "cp",
                str(runtime_path),
                f"s3://{inputs['bucket']}/runtime-config.json",
                "--cache-control",
                "no-store",
                "--content-type",
                "application/json",
            )
        invalidation = context.execution.run(
            "aws",
            "cloudfront",
            "create-invalidation",
            "--distribution-id",
            inputs["distributionId"],
            "--paths",
            "/*",
            "--query",
            "Invalidation.Id",
            "--output",
            "text",
            output=CommandOutput.CAPTURE,
        ).stdout.strip()
        if not invalidation:
            raise DriverError("CloudFront did not return an invalidation ID")
        context.execution.run(
            "aws",
            "cloudfront",
            "wait",
            "invalidation-completed",
            "--distribution-id",
            inputs["distributionId"],
            "--id",
            invalidation,
        )
        curl_args = (
            "curl",
            "--fail",
            "--show-error",
            "--silent",
            "--retry",
            "6",
            "--retry-all-errors",
            "--retry-delay",
            "5",
        )
        served_index = context.execution.run(
            *curl_args,
            inputs["url"],
            output=CommandOutput.CAPTURE,
        ).stdout
        if served_index != index_text:
            raise DriverError("CloudFront did not serve the frontend index from this deployment")
        context.execution.run(
            *curl_args,
            f"{inputs['url']}/runtime-config.json",
        )
        return {
            "published": {
                "sourceRevision": context.source_revision,
                "path": context.source_path,
                "bundle": inputs["bundle"],
                "artifactDigest": runtime.artifact_digest,
                "runtimeConfigHash": runtime.runtime_digest,
                "url": inputs["url"],
            }
        }

    def semantic_result(self, result: object) -> ReconciliationResult:
        return self._select_semantic_result(result)


DRIVER = FrontendS3CloudfrontDriver()
PLUGIN = DRIVER
