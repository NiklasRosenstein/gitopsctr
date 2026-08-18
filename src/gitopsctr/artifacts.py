"""Versioned artifact resource contracts produced by unit drivers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from gitopsctr.contracts import SCHEMA_ROOT, MashumaroContract, QualifiedResourceName, SchemaDocument, StrictModel
from gitopsctr.resource_api import GVK, ApiError, ApiKind, JsonObject, TypedDocumentContract, require_api_spec

ARTIFACT_API_VERSION = "artifact.gitopsctr.io/v1"


@dataclass(frozen=True)
class ArtifactApi[ResourceT]:
    """The typed interface specification implemented by artifact resource APIs."""

    contract: TypedDocumentContract[ResourceT]
    media_type: str

    def parse(self, document: object) -> ResourceT:
        return self.contract.parse(document)

    def dump(self, resource: ResourceT) -> JsonObject:
        return self.contract.dump(resource)

    def json_schema(self) -> JsonObject:
        return self.contract.json_schema()


def require_artifact_api(api_kind: ApiKind[object]) -> ArtifactApi[Any]:
    """Narrow a registered API kind to the Artifact API interface and validate its identity."""

    artifact_api = require_api_spec(api_kind, ArtifactApi, "the Artifact API interface")
    schema = artifact_api.json_schema()
    properties = schema.get("properties", {})
    api_version_schema = properties.get("apiVersion") if isinstance(properties, dict) else None
    kind_schema = properties.get("kind") if isinstance(properties, dict) else None
    group, version = api_kind.gvk.api_version.rsplit("/", 1)
    expected_schema_id = f"{SCHEMA_ROOT}/apis/{group}/{version}/{api_kind.gvk.kind}.schema.json"
    if (
        not isinstance(properties, dict)
        or not isinstance(api_version_schema, dict)
        or api_version_schema.get("const") != api_kind.gvk.api_version
        or not isinstance(kind_schema, dict)
        or kind_schema.get("const") != api_kind.gvk.kind
        or schema.get("$id") != expected_schema_id
    ):
        raise ApiError(f"artifact API kind {api_kind.gvk} metadata does not match its resource contract")
    if not artifact_api.media_type:
        raise ApiError(f"artifact API kind {api_kind.gvk} has no media type")
    return cast(ArtifactApi[Any], artifact_api)


@dataclass(frozen=True, kw_only=True)
class ArtifactMetadata(StrictModel):
    name: str


@dataclass(frozen=True, kw_only=True)
class ArtifactProducer(StrictModel):
    apiVersion: Literal["unit.gitopsctr.io/v1"]
    kind: str
    name: str
    qualifiedName: QualifiedResourceName
    driverVersion: int
    sourceRevision: str
    inputHashVersion: Literal[1]
    inputHash: str


@dataclass(frozen=True, kw_only=True)
class ContainerImage(StrictModel):
    uri: str


@dataclass(frozen=True, kw_only=True)
class ContainerImagesResource(SchemaDocument):
    apiVersion: Literal["artifact.gitopsctr.io/v1"]
    kind: Literal["ContainerImages"]
    metadata: ArtifactMetadata
    producer: ArtifactProducer
    images: dict[str, ContainerImage]


@dataclass(frozen=True, kw_only=True)
class FrontendBundle(StrictModel):
    uri: str
    artifactType: str


@dataclass(frozen=True, kw_only=True)
class FrontendBundleResource(SchemaDocument):
    apiVersion: Literal["artifact.gitopsctr.io/v1"]
    kind: Literal["FrontendBundle"]
    metadata: ArtifactMetadata
    producer: ArtifactProducer
    bundle: FrontendBundle


def artifact_schema_url(kind: str) -> str:
    return f"{SCHEMA_ROOT}/apis/artifact.gitopsctr.io/v1/{kind}.schema.json"


CONTAINER_IMAGES = ApiKind(
    GVK(ARTIFACT_API_VERSION, "ContainerImages"),
    ArtifactApi(
        MashumaroContract(ContainerImagesResource, artifact_schema_url("ContainerImages")),
        "application/vnd.gitopsctr.container-images.v1",
    ),
)
FRONTEND_BUNDLE = ApiKind(
    GVK(ARTIFACT_API_VERSION, "FrontendBundle"),
    ArtifactApi(
        MashumaroContract(FrontendBundleResource, artifact_schema_url("FrontendBundle")),
        "application/vnd.gitopsctr.frontend-bundle.v1",
    ),
)
