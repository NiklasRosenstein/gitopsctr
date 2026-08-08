"""Build a deterministic Vite bundle and publish it as an OCI artifact."""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import tarfile
import tempfile
from pathlib import Path
from typing import TypedDict, cast

from gitopsctr.driver import Driver, DriverContext, DriverError, DriverResult

from ._common import require_strings, run, select_result_fields
from ._oci import (
    FRONTEND_ARCHIVE,
    OCI_DIGEST_RE,
    oras_authentication,
    oras_digest,
    oras_registry_args,
    repository_registry,
    resolve_credential_provider,
)

FRONTEND_ARTIFACT_TYPE = "application/vnd.gitopsctr.frontend.v1"
FRONTEND_LAYER_TYPE = "application/vnd.gitopsctr.frontend.layer.v1.tar+gzip"


class BuiltBundle(TypedDict):
    sourceRevision: str
    path: str
    digest: str


class ViteBundlePlanResult(TypedDict):
    built: BuiltBundle


class UnitIdentity(TypedDict):
    name: str
    driver: str
    inputHashVersion: int
    inputHash: str
    sourceRevision: str


class BundleArtifact(TypedDict):
    type: str
    artifactType: str
    uri: str


class FrontendDocument(TypedDict):
    schema: int
    unit: UnitIdentity
    artifacts: dict[str, BundleArtifact]


class ViteBundleResult(TypedDict):
    artifacts: dict[str, FrontendDocument]


def require_node_24() -> None:
    version = run("node", "--version", capture=True).stdout.strip()
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


class ViteOciBundleDriver(Driver):
    version = 1
    _select_semantic_result = staticmethod(select_result_fields("artifacts"))

    def reconcile(self, context: DriverContext) -> ViteBundlePlanResult | ViteBundleResult:
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
        require_strings(specification, ("name", "driver"), "vite-oci-bundle unit")
        unit_name = cast(str, specification["name"])
        driver_name = cast(str, specification["driver"])
        tag = f"input-{input_hash.removeprefix('sha256:')}"
        frontend_root = context.source_root / context.source_path
        build_environment = {name: value for name, value in os.environ.items() if not name.startswith("VITE_")}

        def build_archive(archive: Path) -> None:
            require_node_24()
            run("npm", "ci", cwd=frontend_root, env=build_environment)
            run("npm", "run", "build", cwd=frontend_root, env=build_environment)
            deterministic_archive(frontend_root / "dist", archive)

        if context.dry:
            with tempfile.TemporaryDirectory(prefix="gitopsctr-frontend-") as directory:
                archive = Path(directory) / FRONTEND_ARCHIVE
                build_archive(archive)
                return {
                    "built": {
                        "sourceRevision": context.source_revision,
                        "path": context.source_path,
                        "digest": f"sha256:{hashlib.sha256(archive.read_bytes()).hexdigest()}",
                    }
                }

        with oras_authentication(credential_provider, {registry}) as registry_config:
            digest = oras_digest(repository, tag, registry_config)
            if digest is None:
                with tempfile.TemporaryDirectory(prefix="gitopsctr-frontend-") as directory:
                    archive = Path(directory) / FRONTEND_ARCHIVE
                    build_archive(archive)
                    run(
                        "oras",
                        "push",
                        *oras_registry_args(registry_config),
                        "--artifact-type",
                        FRONTEND_ARTIFACT_TYPE,
                        "--annotation",
                        f"org.opencontainers.image.revision={context.source_revision}",
                        "--annotation",
                        f"dev.gitopsctr.input-hash={input_hash}",
                        f"{repository}:{tag}",
                        f"{FRONTEND_ARCHIVE}:{FRONTEND_LAYER_TYPE}",
                        cwd=Path(directory),
                    )
                    digest = oras_digest(repository, tag, registry_config)
            if digest is None:
                raise DriverError(f"OCI registry did not return a digest for {repository}:{tag}")

        return {
            "artifacts": {
                "frontend.json": {
                    "schema": 1,
                    "unit": {
                        "name": unit_name,
                        "driver": driver_name,
                        "inputHashVersion": 1,
                        "inputHash": input_hash,
                        "sourceRevision": context.source_revision,
                    },
                    "artifacts": {
                        "bundle": {
                            "type": "oci-artifact",
                            "artifactType": FRONTEND_ARTIFACT_TYPE,
                            "uri": f"{repository}@{digest}",
                        }
                    },
                }
            }
        }

    def semantic_result(self, result: object) -> DriverResult:
        return self._select_semantic_result(result)


PLUGIN = ViteOciBundleDriver()
