"""Build a deterministic Vite bundle and publish it as an OCI artifact."""

from __future__ import annotations

import gzip
import os
import re
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

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
from gitopsctr.execution import CommandOutput, DriverExecution

from ._common import select_result_fields
from ._oci import (
    FRONTEND_ARCHIVE,
    OCI_DIGEST_RE,
    ArtifactUnitIdentity,
    ResolvedCredentialProvider,
    artifact_unit_identity,
    oras_authentication,
    oras_digest,
    oras_registry_args,
    repository_registry,
    resolve_credential_provider,
)

FRONTEND_ARTIFACT_TYPE = "application/vnd.gitopsctr.frontend.v1"
FRONTEND_LAYER_TYPE = "application/vnd.gitopsctr.frontend.layer.v1.tar+gzip"


class BundleArtifact(TypedDict):
    type: str
    artifactType: str
    uri: str


class FrontendDocument(TypedDict):
    schema: int
    unit: ArtifactUnitIdentity
    artifacts: dict[str, BundleArtifact]


class ViteBundleResult(TypedDict):
    artifacts: dict[str, FrontendDocument]


@dataclass(frozen=True, kw_only=True)
class ViteBuild(StrictModel):
    nodeVersion: str


@dataclass(frozen=True, kw_only=True)
class VitePublication(StrictModel):
    repository: str
    credentialProvider: AwsEcrCredentialProvider | None = None


@dataclass(frozen=True, kw_only=True)
class ViteOciBundleUnit(SchemaDocument):
    schema: Literal[1]
    name: str
    driver: Literal["vite-oci-bundle"]
    source: AuthoredSource
    build: ViteBuild | None = None
    publish: VitePublication | None = None
    artifacts: list[Literal["frontend.json"]] | None = None


@dataclass(frozen=True, kw_only=True)
class ViteOciBundleDesiredUnit(SchemaDocument):
    schema: Literal[1]
    name: str
    driver: Literal["vite-oci-bundle"]
    source: DesiredSource
    build: ViteBuild | None = None
    publish: VitePublication | None = None
    artifacts: list[Literal["frontend.json"]] | None = None
    inputs: dict[str, Any] | None = None
    resolvedInputs: dict[str, dict[str, str]] | None = None
    materialization: MaterializationDocument | None = None


@dataclass(frozen=True, kw_only=True)
class BundleArtifactModel(StrictModel):
    type: Literal["oci-artifact"]
    artifactType: str
    uri: str


@dataclass(frozen=True, kw_only=True)
class ViteArtifactIdentity(StrictModel):
    name: str
    driver: str
    inputHashVersion: Literal[1]
    inputHash: str
    sourceRevision: str


@dataclass(frozen=True, kw_only=True)
class FrontendDocumentModel(StrictModel):
    schema: Literal[1]
    unit: ViteArtifactIdentity
    artifacts: dict[str, BundleArtifactModel]


@dataclass(frozen=True, kw_only=True)
class ViteBundleResultModel(StrictModel):
    artifacts: dict[str, FrontendDocumentModel]


@dataclass(frozen=True)
class ViteRuntime:
    repository: str
    registry: str
    credential_provider: ResolvedCredentialProvider | None
    input_hash: str
    unit_identity: ArtifactUnitIdentity
    tag: str
    frontend_root: Path
    build_environment: dict[str, str]


def require_node_24(execution: DriverExecution) -> None:
    version = execution.run("node", "--version", output=CommandOutput.CAPTURE).stdout.strip()
    if re.fullmatch(r"v24\.[0-9]+\.[0-9]+", version) is None:
        raise DriverError(f"vite-oci-bundle requires Node 24, got {version!r}")


def deterministic_archive(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise DriverError("frontend build did not produce dist/")
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path in sorted(source.rglob("*")):
                    if path.is_symlink():
                        raise DriverError(f"frontend bundle contains a symbolic link: {path}")
                    relative = path.relative_to(source).as_posix()
                    info = archive.gettarinfo(str(path), arcname=relative)
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = 0
                    if info.isdir():
                        info.mode = 0o755
                        archive.addfile(info)
                    elif info.isfile():
                        info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
                    else:
                        raise DriverError(f"frontend bundle contains an unsupported file: {path}")


class ViteOciBundleDriver(UnitDriver, PlanningCapability, ReconciliationCapability):
    api_version = "unit.gitopsctr.io/v1"
    kind = "ViteOciBundle"
    driver_name = "vite-oci-bundle"
    version = 1
    schema_base_uri = schema_url("drivers/vite-oci-bundle", version, "").removesuffix(".schema.json")
    unit_contract = MashumaroContract(ViteOciBundleUnit, schema_url("drivers/vite-oci-bundle", version, "unit"))
    desired_unit_contract = MashumaroContract(
        ViteOciBundleDesiredUnit,
        schema_url("drivers/vite-oci-bundle", version, "desired-unit"),
    )
    result_contract = MashumaroContract(ViteBundleResultModel, schema_url("drivers/vite-oci-bundle", version, "result"))
    _select_semantic_result = staticmethod(select_result_fields("artifacts"))

    @staticmethod
    def _runtime(context: UnitExecutionContext) -> ViteRuntime:
        specification = context.unit
        build = specification.get("build")
        publication = specification.get("publish")
        outputs = specification.get("artifacts")
        if not isinstance(build, dict) or not isinstance(publication, dict):
            raise DriverError("vite-oci-bundle requires build and publish objects")
        if build != {"nodeVersion": "24"}:
            raise DriverError("vite-oci-bundle requires nodeVersion 24")
        repository = publication.get("repository")
        if not isinstance(repository, str):
            raise DriverError("vite-oci-bundle requires a repository")
        registry = repository_registry(repository)
        credential_provider = resolve_credential_provider(publication.get("credentialProvider"), {registry})
        if outputs != ["frontend.json"]:
            raise DriverError("vite-oci-bundle produces the frontend.json artifact")
        source = specification.get("source")
        input_hash = source.get("inputHash") if isinstance(source, dict) else None
        if not isinstance(input_hash, str) or not OCI_DIGEST_RE.fullmatch(input_hash):
            raise DriverError("vite-oci-bundle requires a resolved source inputHash")
        unit_identity = artifact_unit_identity(context, input_hash, "vite-oci-bundle unit")
        tag = f"input-{input_hash.removeprefix('sha256:')}"
        return ViteRuntime(
            repository=repository,
            registry=registry,
            credential_provider=credential_provider,
            input_hash=input_hash,
            unit_identity=unit_identity,
            tag=tag,
            frontend_root=context.source_root / context.source_path,
            build_environment={name: value for name, value in os.environ.items() if not name.startswith("VITE_")},
        )

    @staticmethod
    def _build_archive(context: UnitExecutionContext, runtime: ViteRuntime, archive: Path) -> None:
        require_node_24(context.execution)
        context.execution.run("npm", "ci", cwd=runtime.frontend_root, env=runtime.build_environment)
        context.execution.run("npm", "run", "build", cwd=runtime.frontend_root, env=runtime.build_environment)
        deterministic_archive(runtime.frontend_root / "dist", archive)

    def plan(self, context: PlanningContext) -> None:
        runtime = self._runtime(context)
        with tempfile.TemporaryDirectory(prefix="gitopsctr-frontend-") as directory:
            self._build_archive(context, runtime, Path(directory) / FRONTEND_ARCHIVE)

    def reconcile(self, context: ReconciliationContext) -> ViteBundleResult:
        runtime = self._runtime(context)

        with oras_authentication(context.execution, runtime.credential_provider, {runtime.registry}) as registry_config:
            digest = oras_digest(context.execution, runtime.repository, runtime.tag, registry_config)
            if digest is None:
                with tempfile.TemporaryDirectory(prefix="gitopsctr-frontend-") as directory:
                    archive = Path(directory) / FRONTEND_ARCHIVE
                    self._build_archive(context, runtime, archive)
                    context.execution.run(
                        "oras",
                        "push",
                        *oras_registry_args(registry_config),
                        "--artifact-type",
                        FRONTEND_ARTIFACT_TYPE,
                        "--annotation",
                        f"org.opencontainers.image.revision={context.source_revision}",
                        "--annotation",
                        f"dev.gitopsctr.input-hash={runtime.input_hash}",
                        f"{runtime.repository}:{runtime.tag}",
                        f"{FRONTEND_ARCHIVE}:{FRONTEND_LAYER_TYPE}",
                        cwd=Path(directory),
                    )
                    digest = oras_digest(context.execution, runtime.repository, runtime.tag, registry_config)
            if digest is None:
                raise DriverError(f"OCI registry did not return a digest for {runtime.repository}:{runtime.tag}")

        return {
            "artifacts": {
                "frontend.json": {
                    "schema": 1,
                    "unit": runtime.unit_identity,
                    "artifacts": {
                        "bundle": {
                            "type": "oci-artifact",
                            "artifactType": FRONTEND_ARTIFACT_TYPE,
                            "uri": f"{runtime.repository}@{digest}",
                        }
                    },
                }
            }
        }

    def semantic_result(self, result: object) -> ReconciliationResult:
        return self._select_semantic_result(result)


DRIVER = ViteOciBundleDriver()
PLUGIN = DRIVER
