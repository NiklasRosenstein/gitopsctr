"""Build and publish a set of OCI container images."""

from __future__ import annotations

from dataclasses import dataclass
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
from gitopsctr.driver import (
    DriverError,
    PlanningCapability,
    PlanningContext,
    ReconciliationCapability,
    ReconciliationContext,
    ReconciliationResult,
    UnitPlugin,
)
from gitopsctr.execution import CommandOutput, DriverExecution

from ._common import require_strings, select_result_fields
from ._oci import (
    OCI_DIGEST_RE,
    ArtifactUnitIdentity,
    ResolvedCredentialProvider,
    artifact_unit_identity,
    registry_authentication,
    repository_registry,
    resolve_credential_provider,
)


class OciImageArtifact(TypedDict):
    type: str
    uri: str


class ContainersDocument(TypedDict):
    schema: int
    unit: ArtifactUnitIdentity
    artifacts: dict[str, OciImageArtifact]


class OciImagesResult(TypedDict):
    artifacts: dict[str, ContainersDocument]


@dataclass(frozen=True, kw_only=True)
class OciBuild(StrictModel):
    dockerfile: str
    platform: str


@dataclass(frozen=True, kw_only=True)
class OciPublication(StrictModel):
    repositories: dict[str, str]
    credentialProvider: AwsEcrCredentialProvider | None = None


@dataclass(frozen=True, kw_only=True)
class OciImagesUnit(SchemaDocument):
    schema: Literal[1]
    name: str
    driver: Literal["oci-images"]
    source: AuthoredSource
    build: OciBuild | None = None
    publish: OciPublication | None = None
    artifacts: list[Literal["containers.json"]] | None = None
    environment: str | None = None


@dataclass(frozen=True, kw_only=True)
class OciImagesDesiredUnit(SchemaDocument):
    schema: Literal[1]
    name: str
    driver: Literal["oci-images"]
    source: DesiredSource
    build: OciBuild | None = None
    publish: OciPublication | None = None
    artifacts: list[Literal["containers.json"]] | None = None
    inputs: dict[str, Any] | None = None
    resolvedInputs: dict[str, dict[str, str]] | None = None
    materialization: MaterializationDocument | None = None


@dataclass(frozen=True, kw_only=True)
class ArtifactIdentityModel(StrictModel):
    name: str
    driver: str
    inputHashVersion: Literal[1]
    inputHash: str
    sourceRevision: str


@dataclass(frozen=True, kw_only=True)
class OciImageArtifactModel(StrictModel):
    type: Literal["oci-image"]
    uri: str


@dataclass(frozen=True, kw_only=True)
class ContainersDocumentModel(StrictModel):
    schema: Literal[1]
    unit: ArtifactIdentityModel
    artifacts: dict[str, OciImageArtifactModel]


@dataclass(frozen=True, kw_only=True)
class OciImagesResultModel(StrictModel):
    artifacts: dict[str, ContainersDocumentModel]


@dataclass(frozen=True)
class OciRuntime:
    repositories: dict[str, str]
    registries: set[str]
    credential_provider: ResolvedCredentialProvider | None
    dockerfile: str
    platform: str
    input_hash: str
    unit_identity: ArtifactUnitIdentity
    tag: str
    local_image: str


def oci_digest(
    execution: DriverExecution,
    repository: str,
    tag: str,
    docker_environment: dict[str, str] | None = None,
) -> str | None:
    reference = f"{repository}:{tag}"
    result = execution.run(
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        "--format",
        "{{.Manifest.Digest}}",
        reference,
        check=False,
        output=CommandOutput.CAPTURE,
        env=docker_environment,
    )
    digest = result.stdout.strip()
    if result.returncode == 0:
        if not OCI_DIGEST_RE.fullmatch(digest):
            raise DriverError(f"OCI registry returned an invalid digest for {reference}")
        return digest
    error = result.stderr.strip()
    lowered = error.lower()
    if (
        "manifest unknown" in lowered
        or "no such manifest" in lowered
        or "name unknown" in lowered
        or f"{reference.lower()}: not found" in lowered
    ):
        return None
    raise DriverError(error or f"could not inspect {reference}")


class OciImagesPlugin(UnitPlugin, PlanningCapability, ReconciliationCapability):
    version = 2
    schema_base_uri = schema_url("drivers/oci-images", version, "").removesuffix(".schema.json")
    unit_contract = MashumaroContract(OciImagesUnit, schema_url("drivers/oci-images", version, "unit"))
    desired_unit_contract = MashumaroContract(
        OciImagesDesiredUnit,
        schema_url("drivers/oci-images", version, "desired-unit"),
    )
    result_contract = MashumaroContract(OciImagesResultModel, schema_url("drivers/oci-images", version, "result"))
    _select_semantic_result = staticmethod(select_result_fields("artifacts"))

    @staticmethod
    def _runtime(context: PlanningContext | ReconciliationContext) -> OciRuntime:
        specification = context.unit
        build = specification.get("build")
        publication = specification.get("publish")
        outputs = specification.get("artifacts")
        if not isinstance(build, dict) or not isinstance(publication, dict):
            raise DriverError("oci-images requires build and publish objects")
        repositories = publication.get("repositories")
        dockerfile = build.get("dockerfile")
        platform = build.get("platform")
        if not isinstance(repositories, dict) or not repositories:
            raise DriverError("oci-images requires named repositories")
        require_strings(repositories, tuple(repositories), "oci-images repositories")
        repositories = cast(dict[str, str], repositories)
        registries = {repository_registry(repository) for repository in repositories.values()}
        credential_provider = resolve_credential_provider(publication.get("credentialProvider"), registries)
        if not all(isinstance(value, str) and value for value in (dockerfile, platform)):
            raise DriverError("oci-images requires dockerfile and platform")
        dockerfile = cast(str, dockerfile)
        platform = cast(str, platform)
        if outputs != ["containers.json"]:
            raise DriverError("oci-images currently produces the containers.json artifact")

        source = specification.get("source")
        input_hash = source.get("inputHash") if isinstance(source, dict) else None
        if not isinstance(input_hash, str) or not OCI_DIGEST_RE.fullmatch(input_hash):
            raise DriverError("oci-images requires a resolved source inputHash")
        unit_identity = artifact_unit_identity(context, input_hash, "oci-images unit")
        tag = f"input-{input_hash.removeprefix('sha256:')}"
        local_image = f"{unit_identity['name']}:{input_hash.removeprefix('sha256:')}"
        return OciRuntime(
            repositories,
            registries,
            credential_provider,
            dockerfile,
            platform,
            input_hash,
            unit_identity,
            tag,
            local_image,
        )

    @staticmethod
    def _build_image(context: PlanningContext | ReconciliationContext, runtime: OciRuntime) -> None:
        context.execution.run(
            "docker",
            "build",
            "--platform",
            runtime.platform,
            "--provenance=false",
            "--file",
            str(context.source_root / runtime.dockerfile),
            "--tag",
            runtime.local_image,
            str(context.source_root),
        )

    def plan(self, context: PlanningContext) -> None:
        self._build_image(context, self._runtime(context))

    def reconcile(self, context: ReconciliationContext) -> OciImagesResult:
        runtime = self._runtime(context)
        repositories = runtime.repositories
        tag = runtime.tag
        local_image = runtime.local_image

        def build_image() -> None:
            self._build_image(context, runtime)

        with registry_authentication(
            context.execution,
            runtime.credential_provider,
            runtime.registries,
        ) as docker_environment:
            existing = {
                name: oci_digest(context.execution, repository, tag, docker_environment)
                for name, repository in repositories.items()
            }
            if not any(existing.values()):
                legacy_tag = f"git-{context.source_revision}"
                existing = {
                    name: oci_digest(context.execution, repository, legacy_tag, docker_environment)
                    for name, repository in repositories.items()
                }
            published_digests = {digest for digest in existing.values() if digest is not None}
            if len(published_digests) > 1:
                raise DriverError("published repositories disagree on the artifact digest")
            if any(digest is None for digest in existing.values()):
                available = [(name, digest) for name, digest in existing.items() if digest is not None]
                if available:
                    name, digest = available[0]
                    source_image = f"{repositories[name]}@{digest}"
                    context.execution.run("docker", "pull", source_image, env=docker_environment)
                    context.execution.run("docker", "tag", source_image, local_image)
                else:
                    build_image()

            artifacts: dict[str, OciImageArtifact] = {}
            for name, repository in repositories.items():
                digest = existing[name]
                if digest is None:
                    target = f"{repository}:{tag}"
                    context.execution.run("docker", "tag", local_image, target)
                    context.execution.run("docker", "push", target, env=docker_environment)
                    digest = oci_digest(context.execution, repository, tag, docker_environment)
                if digest is None:
                    raise DriverError(f"OCI registry did not return a digest for {repository}:{tag}")
                artifacts[name] = {"type": "oci-image", "uri": f"{repository}@{digest}"}

            if len({artifact["uri"].rsplit("@", 1)[1] for artifact in artifacts.values()}) > 1:
                raise DriverError("published repositories disagree on the artifact digest")

        return {
            "artifacts": {
                "containers.json": {
                    "schema": 1,
                    "unit": runtime.unit_identity,
                    "artifacts": artifacts,
                }
            }
        }

    def semantic_result(self, result: object) -> ReconciliationResult:
        return self._select_semantic_result(result)


PLUGIN = OciImagesPlugin()
