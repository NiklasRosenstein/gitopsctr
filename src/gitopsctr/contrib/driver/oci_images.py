"""Build and publish a set of OCI container images."""

from __future__ import annotations

import subprocess
from typing import TypedDict, cast

from gitopsctr.driver import Driver, DriverContext, DriverError, DriverResult

from ._common import require_strings, run, select_result_fields
from ._oci import (
    OCI_DIGEST_RE,
    registry_authentication,
    repository_registry,
    resolve_credential_provider,
)


class OciImageArtifact(TypedDict):
    type: str
    uri: str


class UnitIdentity(TypedDict):
    name: str
    driver: str
    inputHashVersion: int
    inputHash: str
    sourceRevision: str


class ContainersDocument(TypedDict):
    schema: int
    unit: UnitIdentity
    artifacts: dict[str, OciImageArtifact]


class OciImagesResult(TypedDict):
    artifacts: dict[str, ContainersDocument]


def oci_digest(repository: str, tag: str, docker_environment: dict[str, str] | None = None) -> str | None:
    reference = f"{repository}:{tag}"
    result = subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "--format",
            "{{.Manifest.Digest}}",
            reference,
        ],
        check=False,
        capture_output=True,
        text=True,
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


class OciImagesDriver(Driver):
    version = 2
    _select_semantic_result = staticmethod(select_result_fields("artifacts"))

    def reconcile(self, context: DriverContext) -> OciImagesResult | dict[str, object]:
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
        require_strings(specification, ("name", "driver"), "oci-images unit")
        unit_name = cast(str, specification["name"])
        driver_name = cast(str, specification["driver"])
        tag = f"input-{input_hash.removeprefix('sha256:')}"
        local_image = f"{unit_name}:{input_hash.removeprefix('sha256:')}"
        if context.dry:
            run(
                "docker",
                "build",
                "--platform",
                platform,
                "--provenance=false",
                "--file",
                str(context.source_root / dockerfile),
                "--tag",
                local_image,
                str(context.source_root),
            )
            return {}

        with registry_authentication(credential_provider, registries) as docker_environment:
            existing = {
                name: oci_digest(repository, tag, docker_environment) for name, repository in repositories.items()
            }
            if not any(existing.values()):
                legacy_tag = f"git-{context.source_revision}"
                existing = {
                    name: oci_digest(repository, legacy_tag, docker_environment)
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
                    run("docker", "pull", source_image, env=docker_environment)
                    run("docker", "tag", source_image, local_image)
                else:
                    run(
                        "docker",
                        "build",
                        "--platform",
                        platform,
                        "--provenance=false",
                        "--file",
                        str(context.source_root / dockerfile),
                        "--tag",
                        local_image,
                        str(context.source_root),
                    )

            artifacts: dict[str, OciImageArtifact] = {}
            for name, repository in repositories.items():
                digest = existing[name]
                if digest is None:
                    target = f"{repository}:{tag}"
                    run("docker", "tag", local_image, target)
                    run("docker", "push", target, env=docker_environment)
                    digest = oci_digest(repository, tag, docker_environment)
                if digest is None:
                    raise DriverError(f"OCI registry did not return a digest for {repository}:{tag}")
                artifacts[name] = {"type": "oci-image", "uri": f"{repository}@{digest}"}

            if len({artifact["uri"].rsplit("@", 1)[1] for artifact in artifacts.values()}) > 1:
                raise DriverError("published repositories disagree on the artifact digest")

        return {
            "artifacts": {
                "containers.json": {
                    "schema": 1,
                    "unit": {
                        "name": unit_name,
                        "driver": driver_name,
                        "inputHashVersion": 1,
                        "inputHash": input_hash,
                        "sourceRevision": context.source_revision,
                    },
                    "artifacts": artifacts,
                }
            }
        }

    def semantic_result(self, result: object) -> DriverResult:
        return self._select_semantic_result(result)


PLUGIN = OciImagesDriver()
