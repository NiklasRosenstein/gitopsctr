"""Versioned artifact resource contracts produced by unit drivers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from gitopsctr.contracts import SCHEMA_ROOT, MashumaroContract, SchemaDocument, StrictModel

ARTIFACT_API_VERSION = "artifact.gitopsctr.io/v1"


@dataclass(frozen=True, kw_only=True)
class ArtifactMetadata(StrictModel):
    name: str


@dataclass(frozen=True, kw_only=True)
class ArtifactProducer(StrictModel):
    apiVersion: Literal["unit.gitopsctr.io/v1"]
    kind: str
    name: str
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


CONTAINER_IMAGES_CONTRACT = MashumaroContract(
    ContainerImagesResource,
    artifact_schema_url("ContainerImages"),
)
FRONTEND_BUNDLE_CONTRACT = MashumaroContract(
    FrontendBundleResource,
    artifact_schema_url("FrontendBundle"),
)
