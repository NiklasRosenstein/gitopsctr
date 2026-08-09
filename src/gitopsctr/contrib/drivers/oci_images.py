"""Build and publish a set of OCI container images."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

from gitopsctr.artifacts import (
    ARTIFACT_API_VERSION,
    CONTAINER_IMAGES,
    ArtifactMetadata,
    ArtifactProducer,
    ContainerImage,
    ContainerImagesResource,
)
from gitopsctr.contracts import (
    AuthoredSource,
    AwsEcrCredentialProvider,
    DesiredSource,
    EmptyResultModel,
    MashumaroContract,
    ResolvedInputs,
    StrictModel,
    schema_url,
)
from gitopsctr.document import JsonObject, ResolvedJsonObjectValue
from gitopsctr.driver import (
    DriverError,
    PlanningCapability,
    PlanningContext,
    ReconciliationCapability,
    ReconciliationContext,
    ReconciliationOutput,
    ReconciliationResult,
    UnitDriver,
    UnitResolution,
    UnitResolutionContext,
    unit_driver_api,
)
from gitopsctr.execution import CommandOutput, DriverExecution

from ._oci import (
    OCI_DIGEST_RE,
    ResolvedCredentialProvider,
    artifact_producer_identity,
    registry_authentication,
    repository_registry,
    resolve_credential_provider,
)


class OciImageArtifact(TypedDict):
    uri: str


@dataclass(frozen=True, kw_only=True)
class OciBuild(StrictModel):
    dockerfile: str
    platform: str


@dataclass(frozen=True, kw_only=True)
class RegistryTarget(StrictModel):
    type: Literal["registry"]
    repository: str


@dataclass(frozen=True, kw_only=True)
class KindTarget(StrictModel):
    type: Literal["kind"]
    cluster: str


@dataclass(frozen=True, kw_only=True)
class MinikubeTarget(StrictModel):
    type: Literal["minikube"]
    profile: str


PublicationTarget = RegistryTarget | KindTarget | MinikubeTarget


@dataclass(frozen=True, kw_only=True)
class OciPublication(StrictModel):
    targets: dict[str, PublicationTarget]
    credentialProvider: AwsEcrCredentialProvider | None = None


@dataclass(frozen=True, kw_only=True)
class OciImagesUnit(StrictModel):
    source: AuthoredSource
    build: OciBuild | None = None
    publish: OciPublication | None = None
    environment: str | None = None


@dataclass(frozen=True, kw_only=True)
class OciImagesDesiredUnit(StrictModel):
    source: DesiredSource
    build: OciBuild | None = None
    publish: OciPublication | None = None
    inputs: ResolvedJsonObjectValue | None = None
    resolvedInputs: ResolvedInputs | None = None


@dataclass(frozen=True)
class OciRuntime:
    targets: dict[str, dict[str, str]]
    repositories: dict[str, str]
    registries: set[str]
    credential_provider: ResolvedCredentialProvider | None
    dockerfile: str
    platform: str
    input_hash: str
    producer: ArtifactProducer
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


class OciImagesDriver(
    UnitDriver[OciImagesUnit, OciImagesDesiredUnit, OciImagesDesiredUnit, EmptyResultModel],
    PlanningCapability[OciImagesDesiredUnit],
    ReconciliationCapability[OciImagesDesiredUnit, EmptyResultModel],
):
    api_version = "unit.gitopsctr.io/v1"
    kind = "OciImages"
    driver_name = "oci-images"
    version = 1
    schema_base_uri = schema_url("drivers/oci-images", version, "").removesuffix(".schema.json")
    unit_contract = MashumaroContract(OciImagesUnit, schema_url("drivers/oci-images", version, "unit"))
    resolved_unit_contract = MashumaroContract(
        OciImagesDesiredUnit,
        schema_url("drivers/oci-images", version, "resolved-unit"),
    )
    desired_unit_contract = MashumaroContract(
        OciImagesDesiredUnit,
        schema_url("drivers/oci-images", version, "desired-unit"),
    )
    result_contract = MashumaroContract(EmptyResultModel, schema_url("drivers/oci-images", version, "result"))
    artifact_outputs = {"containers": CONTAINER_IMAGES}

    def scaffold_unit_spec(self, name: str, source_path: str) -> JsonObject:
        return {"source": {"path": source_path}}

    def resolve_unit(self, unit: OciImagesUnit, context: UnitResolutionContext) -> UnitResolution[OciImagesDesiredUnit]:
        if context.source is None:
            raise DriverError("oci-images requires a source")
        return UnitResolution(
            OciImagesDesiredUnit(
                source=context.source,
                build=unit.build,
                publish=unit.publish,
            )
        )

    @staticmethod
    def _runtime(
        context: PlanningContext[OciImagesDesiredUnit] | ReconciliationContext[OciImagesDesiredUnit],
    ) -> OciRuntime:
        specification = context.unit
        build = specification.build
        publication = specification.publish
        if build is None or publication is None:
            raise DriverError("oci-images requires build and publish objects")
        targets = publication.targets
        dockerfile = build.dockerfile
        platform = build.platform
        if not targets:
            raise DriverError("oci-images requires named publication targets")
        resolved_targets: dict[str, dict[str, str]] = {}
        repositories: dict[str, str] = {}
        for name, target in targets.items():
            if not name:
                raise DriverError("oci-images publication targets must be named objects")
            target_type = target.type
            if target_type == "registry":
                assert isinstance(target, RegistryTarget)
                repository = target.repository
                if not repository:
                    raise DriverError(f"oci-images registry target {name!r} requires repository")
                repository_registry(repository)
                repositories[name] = repository
                resolved_targets[name] = {"type": "registry", "repository": repository}
            elif target_type == "kind":
                assert isinstance(target, KindTarget)
                cluster = target.cluster
                if not cluster:
                    raise DriverError(f"oci-images kind target {name!r} requires cluster")
                resolved_targets[name] = {"type": "kind", "cluster": cluster}
            elif target_type == "minikube":
                assert isinstance(target, MinikubeTarget)
                profile = target.profile
                if not profile:
                    raise DriverError(f"oci-images minikube target {name!r} requires profile")
                resolved_targets[name] = {"type": "minikube", "profile": profile}
            else:
                raise DriverError(f"oci-images target {name!r} has unknown type: {target_type!r}")
        registries = {repository_registry(repository) for repository in repositories.values()}
        if publication.credentialProvider is not None and not registries:
            raise DriverError("oci-images credentialProvider requires at least one registry target")
        credential_provider = resolve_credential_provider(
            publication.credentialProvider.to_dict() if publication.credentialProvider is not None else None,
            registries,
        )
        input_hash = specification.source.inputHash
        if not isinstance(input_hash, str) or not OCI_DIGEST_RE.fullmatch(input_hash):
            raise DriverError("oci-images requires a resolved source inputHash")
        producer = artifact_producer_identity(
            context,
            input_hash,
            "oci-images unit",
            kind="OciImages",
            driver_version=OciImagesDriver.version,
        )
        tag = f"input-{input_hash.removeprefix('sha256:')}"
        local_image = f"{producer.name}:{input_hash.removeprefix('sha256:')}"
        return OciRuntime(
            resolved_targets,
            repositories,
            registries,
            credential_provider,
            dockerfile,
            platform,
            input_hash,
            producer,
            tag,
            local_image,
        )

    @staticmethod
    def _build_image(
        context: PlanningContext[OciImagesDesiredUnit] | ReconciliationContext[OciImagesDesiredUnit],
        runtime: OciRuntime,
    ) -> None:
        if context.source_root is None:
            raise DriverError("oci-images requires a source")
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

    def plan(self, context: PlanningContext[OciImagesDesiredUnit]) -> None:
        self._build_image(context, self._runtime(context))

    def reconcile(
        self,
        context: ReconciliationContext[OciImagesDesiredUnit],
    ) -> ReconciliationOutput[EmptyResultModel]:
        runtime = self._runtime(context)
        repositories = runtime.repositories
        targets = runtime.targets
        tag = runtime.tag
        local_image = runtime.local_image
        local_targets = {name: target for name, target in targets.items() if target["type"] != "registry"}
        image_available = False

        def build_image() -> None:
            nonlocal image_available
            self._build_image(context, runtime)
            image_available = True

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
            if any(digest is None for digest in existing.values()) or local_targets:
                available = [(name, digest) for name, digest in existing.items() if digest is not None]
                if available:
                    name, digest = available[0]
                    source_image = f"{repositories[name]}@{digest}"
                    context.execution.run("docker", "pull", source_image, env=docker_environment)
                    context.execution.run("docker", "tag", source_image, local_image)
                    image_available = True
                else:
                    build_image()

            artifacts: dict[str, OciImageArtifact] = {}
            for name, repository in repositories.items():
                digest = existing[name]
                if digest is None:
                    if not image_available:
                        build_image()
                    target = f"{repository}:{tag}"
                    context.execution.run("docker", "tag", local_image, target)
                    context.execution.run("docker", "push", target, env=docker_environment)
                    digest = oci_digest(context.execution, repository, tag, docker_environment)
                if digest is None:
                    raise DriverError(f"OCI registry did not return a digest for {repository}:{tag}")
                artifacts[name] = {"uri": f"{repository}@{digest}"}

            if len({artifact["uri"].rsplit("@", 1)[1] for artifact in artifacts.values()}) > 1:
                raise DriverError("published repositories disagree on the artifact digest")

            for name, target in local_targets.items():
                if not image_available:
                    build_image()
                if target["type"] == "kind":
                    context.execution.run("kind", "load", "docker-image", local_image, "--name", target["cluster"])
                else:
                    context.execution.run(
                        "minikube", "--profile", target["profile"], "image", "load", local_image, "--daemon"
                    )
                artifacts[name] = {"uri": local_image}

        resource = ContainerImagesResource(
            apiVersion=ARTIFACT_API_VERSION,
            kind="ContainerImages",
            metadata=ArtifactMetadata(name="containers"),
            producer=runtime.producer,
            images={name: ContainerImage(uri=image["uri"]) for name, image in artifacts.items()},
        )
        return ReconciliationOutput(
            result=EmptyResultModel(),
            artifacts={"containers": CONTAINER_IMAGES.spec.dump(resource)},
        )

    def semantic_result(self, result: object) -> ReconciliationResult:
        if isinstance(result, EmptyResultModel) or result == {}:
            return {}
        raise DriverError("oci-images receipt result must be empty")


DRIVER = OciImagesDriver()
API_KIND = unit_driver_api(DRIVER)
