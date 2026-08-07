"""Driver plugins distributed with gitopsctr."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from gitopsctr.driver import (
    DriverContext,
    DriverError,
    DriverPlugin,
    SemanticResultSelector,
    VerificationResult,
    VerificationStatus,
)

OCI_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
FRONTEND_ARTIFACT_TYPE = "application/vnd.gitopsctr.frontend.v1"
FRONTEND_LAYER_TYPE = "application/vnd.gitopsctr.frontend.layer.v1.tar+gzip"
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


CredentialProviderValidator = Callable[[str, dict[str, Any]], None]
CredentialProviderLoader = Callable[[str, dict[str, Any]], RegistryCredentials]


@dataclass(frozen=True)
class CredentialProvider:
    validate: CredentialProviderValidator
    load: CredentialProviderLoader


def run(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "check": check,
        "text": True,
        "cwd": cwd,
        "env": env,
        "input": input_text,
    }
    if capture:
        kwargs["capture_output"] = True
    else:
        kwargs.update(stdout=sys.stderr, stderr=sys.stderr)
    return subprocess.run(args, **kwargs)


def require_strings(values: dict[str, Any], names: tuple[str, ...], contract: str) -> None:
    missing = [name for name in names if not isinstance(values.get(name), str) or not values[name]]
    if missing:
        raise DriverError(f"{contract} is missing string values: {', '.join(missing)}")


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


def validate_aws_ecr_provider(registry: str, configuration: dict[str, Any]) -> None:
    unsupported = set(configuration) - {"type"}
    if unsupported:
        raise DriverError(f"aws-ecr credentialProvider has unsupported fields: {', '.join(sorted(unsupported))}")
    if PRIVATE_ECR_REGISTRY_RE.fullmatch(registry) is None:
        raise DriverError(f"aws-ecr requires a private ECR registry, got {registry!r}")


def aws_ecr_credentials(registry: str, configuration: dict[str, Any]) -> RegistryCredentials:
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
    driver_status(
        "AUTH",
        f"{registry}: aws-ecr via {credential_source} (region {region})",
    )
    password = run(
        "aws",
        "ecr",
        "get-login-password",
        "--region",
        region,
        capture=True,
    ).stdout
    if not password:
        raise DriverError(f"aws-ecr returned an empty password for {registry}")
    return RegistryCredentials(username="AWS", password=password)


CREDENTIAL_PROVIDERS: dict[str, CredentialProvider] = {
    "aws-ecr": CredentialProvider(
        validate=validate_aws_ecr_provider,
        load=aws_ecr_credentials,
    ),
}


def resolve_credential_provider(
    configuration: Any, registries: set[str]
) -> tuple[CredentialProvider, dict[str, Any]] | None:
    if configuration is None:
        return None
    if not isinstance(configuration, dict):
        raise DriverError("oci-images credentialProvider must be an object")
    provider_type = configuration.get("type")
    if not isinstance(provider_type, str) or provider_type not in CREDENTIAL_PROVIDERS:
        raise DriverError(f"oci-images uses an unknown credential provider: {provider_type!r}")
    provider = CREDENTIAL_PROVIDERS[provider_type]
    for registry in registries:
        provider.validate(registry, configuration)
    return provider, configuration


def docker_cli_plugins() -> Path | None:
    configured_root = os.environ.get("DOCKER_CONFIG")
    docker_config = Path(configured_root) if configured_root else Path.home() / ".docker"
    plugins = docker_config / "cli-plugins"
    return plugins.resolve() if plugins.is_dir() else None


@contextmanager
def registry_authentication(
    provider: tuple[CredentialProvider, dict[str, Any]] | None,
    registries: set[str],
):
    if provider is None:
        for registry in sorted(registries):
            driver_status(
                "AUTH",
                f"{registry}: existing Docker credentials or anonymous access",
            )
        yield None
        return
    credential_provider, configuration = provider
    with tempfile.TemporaryDirectory(prefix="gitopsctr-docker-") as docker_config:
        if plugins := docker_cli_plugins():
            (Path(docker_config) / "cli-plugins").symlink_to(plugins, target_is_directory=True)
        docker_environment = os.environ | {"DOCKER_CONFIG": docker_config}
        for registry in sorted(registries):
            credentials = credential_provider.load(registry, configuration)
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
    provider: tuple[CredentialProvider, dict[str, Any]] | None,
    registries: set[str],
):
    if provider is None:
        for registry in sorted(registries):
            driver_status("AUTH", f"{registry}: existing ORAS credentials or anonymous access")
        yield None
        return
    credential_provider, configuration = provider
    with tempfile.TemporaryDirectory(prefix="gitopsctr-oras-") as directory:
        registry_config = Path(directory) / "config.json"
        for registry in sorted(registries):
            credentials = credential_provider.load(registry, configuration)
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
    lowered = error.lower()
    if any(marker in lowered for marker in ("manifest unknown", "not found", "name unknown", "tag invalid")):
        return None
    raise DriverError(error or f"could not inspect {reference}")


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


def apply_oci_images(context: DriverContext) -> dict[str, Any]:
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
    registries = {repository_registry(repository) for repository in repositories.values()}
    credential_provider = resolve_credential_provider(publication.get("credentialProvider"), registries)
    if not all(isinstance(value, str) and value for value in (dockerfile, platform)):
        raise DriverError("oci-images requires dockerfile and platform")
    if outputs != ["containers.json"]:
        raise DriverError("oci-images currently produces the containers.json artifact")

    input_hash = specification.get("source", {}).get("inputHash")
    if not isinstance(input_hash, str) or not OCI_DIGEST_RE.fullmatch(input_hash):
        raise DriverError("oci-images requires a resolved source inputHash")
    tag = f"input-{input_hash.removeprefix('sha256:')}"
    local_image = f"{specification['name']}:{input_hash.removeprefix('sha256:')}"
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
        existing = {name: oci_digest(repository, tag, docker_environment) for name, repository in repositories.items()}
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

        artifacts: dict[str, dict[str, str]] = {}
        for name, repository in repositories.items():
            digest = existing[name]
            if digest is None:
                target = f"{repository}:{tag}"
                run("docker", "tag", local_image, target)
                run("docker", "push", target, env=docker_environment)
                digest = oci_digest(repository, tag, docker_environment)
            if digest is None:
                raise DriverError(f"OCI registry did not return a digest for {repository}:{tag}")
            artifacts[name] = {
                "type": "oci-image",
                "uri": f"{repository}@{digest}",
            }

        if len({artifact["uri"].rsplit("@", 1)[1] for artifact in artifacts.values()}) > 1:
            raise DriverError("published repositories disagree on the artifact digest")

    return {
        "artifacts": {
            "containers.json": {
                "schema": 1,
                "unit": {
                    "name": specification["name"],
                    "driver": specification["driver"],
                    "inputHashVersion": 1,
                    "inputHash": input_hash,
                    "sourceRevision": context.source_revision,
                },
                "artifacts": artifacts,
            }
        }
    }


def terraform_runtime(
    context: DriverContext,
) -> tuple[Path, dict[str, str], str, list[str], list[Any]]:
    configuration = context.unit.get("terraform")
    if not isinstance(configuration, dict):
        raise DriverError("terraform driver requires a terraform configuration")
    backend = configuration.get("backend")
    variables = configuration.get("variables")
    output_names = configuration.get("observeOutputs")
    checks = configuration.get("checks", [])
    if not isinstance(backend, dict) or not isinstance(variables, dict):
        raise DriverError("terraform driver requires backend and variables objects")
    backend_key = backend.get("key")
    if not isinstance(backend_key, str) or not backend_key:
        raise DriverError("terraform backend requires a key")
    if not isinstance(output_names, list) or not all(isinstance(name, str) for name in output_names):
        raise DriverError("terraform observeOutputs must be a list of names")
    if not isinstance(checks, list):
        raise DriverError("terraform checks must be a list")

    terraform_root = context.source_root / context.source_path
    terraform_environment = os.environ | {
        f"TF_VAR_{name}": value if isinstance(value, str) else json.dumps(value) for name, value in variables.items()
    }
    return terraform_root, terraform_environment, backend_key, output_names, checks


def apply_terraform(context: DriverContext) -> dict[str, Any]:
    terraform_root, terraform_environment, backend_key, output_names, checks = terraform_runtime(context)
    report_text: Path | None = None
    if context.report is not None:
        context.report.mkdir(parents=True, exist_ok=True)
        plan = context.report / "plan.tfplan"
        report_text = context.report / "plan.txt"
        for previous in (plan, report_text):
            if previous.exists():
                previous.unlink()
    else:
        plan = context.source_root / ".reconcile.tfplan"

    def terraform(
        *args: str,
        reported: bool = False,
        emit: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if report_text is None:
            return run(
                "terraform",
                *args,
                cwd=terraform_root,
                env=terraform_environment,
            )
        try:
            result = run(
                "terraform",
                *args,
                cwd=terraform_root,
                env=terraform_environment,
                capture=True,
            )
        except subprocess.CalledProcessError as exc:
            output = "".join(part for part in (exc.stdout, exc.stderr) if part)
            if output:
                print(output, end="" if output.endswith("\n") else "\n", file=sys.stderr)
            report_text.write_text(output or f"terraform {' '.join(args)} failed\n")
            raise
        output = "".join(part for part in (result.stdout, result.stderr) if part)
        if output and emit:
            print(output, end="" if output.endswith("\n") else "\n", file=sys.stderr)
        if reported:
            report_text.write_text(output)
        return result

    terraform("init", f"-backend-config=key={backend_key}")
    plan_args = ["plan", f"-out={plan}"]
    if context.dry:
        plan_args.extend(("-refresh=false", "-lock=false", "-input=false", "-no-color"))
    terraform(*plan_args, emit=report_text is None)
    if report_text is not None:
        terraform("show", "-no-color", str(plan), reported=True)
    if context.dry:
        return {"planned": {"sourceRevision": context.source_revision}}
    terraform("apply", "-auto-approve", str(plan))
    try:
        raw_outputs = json.loads(
            run(
                "terraform",
                "output",
                "-json",
                cwd=terraform_root,
                env=terraform_environment,
                capture=True,
            ).stdout
        )
        outputs = {name: raw_outputs[name]["value"] for name in output_names}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DriverError(f"Terraform did not return the expected outputs: {exc}") from exc

    for check in checks:
        if not isinstance(check, dict) or check.get("type") != "http":
            raise DriverError("terraform currently supports only HTTP checks")
        output_name = check.get("urlOutput")
        path = check.get("path", "")
        if output_name not in outputs or not isinstance(path, str):
            raise DriverError("terraform HTTP check has invalid urlOutput or path")
        run(
            "curl",
            "--fail",
            "--show-error",
            "--silent",
            "--retry",
            "12",
            "--retry-all-errors",
            "--retry-delay",
            "5",
            f"{outputs[output_name]}{path}",
        )

    return {
        "applied": {
            "sourceRevision": context.source_revision,
            "path": context.source_path,
        },
        "outputs": outputs,
    }


def verify_terraform(context: DriverContext) -> VerificationResult:
    terraform_root, terraform_environment, backend_key, _, _ = terraform_runtime(context)
    report_text: Path | None = None
    if context.report is not None:
        context.report.mkdir(parents=True, exist_ok=True)
        plan = context.report / "verify.tfplan"
        report_text = context.report / "verify.txt"
        for previous in (plan, report_text):
            if previous.exists():
                previous.unlink()
    else:
        plan = context.source_root / ".verify.tfplan"

    run(
        "terraform",
        "init",
        f"-backend-config=key={backend_key}",
        cwd=terraform_root,
        env=terraform_environment,
    )
    result = run(
        "terraform",
        "plan",
        "-detailed-exitcode",
        "-input=false",
        "-no-color",
        f"-out={plan}",
        cwd=terraform_root,
        env=terraform_environment,
        capture=True,
        check=False,
    )
    output = "".join(part for part in (result.stdout, result.stderr) if part)
    if output:
        print(output, end="" if output.endswith("\n") else "\n", file=sys.stderr)
    if report_text is not None:
        report_text.write_text(output)

    if result.returncode == 0:
        return VerificationResult(VerificationStatus.CLEAN)
    if result.returncode == 2:
        return VerificationResult(VerificationStatus.DRIFT)
    raise DriverError(output.strip() or f"Terraform verification failed with exit code {result.returncode}")


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


def apply_vite_oci_bundle(context: DriverContext) -> dict[str, Any]:
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
    input_hash = specification.get("source", {}).get("inputHash")
    if not isinstance(input_hash, str) or not OCI_DIGEST_RE.fullmatch(input_hash):
        raise DriverError("vite-oci-bundle requires a resolved source inputHash")
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
                    "name": specification["name"],
                    "driver": specification["driver"],
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


def runtime_configuration(inputs: dict[str, Any]) -> dict[str, Any]:
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
    return configuration


def apply_frontend_s3_cloudfront(context: DriverContext) -> dict[str, Any]:
    require_strings(
        context.inputs,
        ("bundle", "bucket", "distributionId", "url"),
        "frontend-s3-cloudfront inputs",
    )
    configuration = runtime_configuration(context.inputs)
    repository, artifact_digest = parse_oci_uri(context.inputs["bundle"])
    publication = context.unit.get("pull", {})
    if not isinstance(publication, dict):
        raise DriverError("frontend-s3-cloudfront pull must be an object")
    registry = repository_registry(repository)
    credential_provider = resolve_credential_provider(publication.get("credentialProvider"), {registry})
    runtime_bytes = json.dumps(configuration, indent=2, sort_keys=True).encode() + b"\n"
    runtime_digest = f"sha256:{hashlib.sha256(runtime_bytes).hexdigest()}"
    if context.dry:
        return {
            "planned": {
                "bundle": context.inputs["bundle"],
                "runtimeConfigHash": runtime_digest,
            }
        }

    with tempfile.TemporaryDirectory(prefix="gitopsctr-frontend-deploy-") as directory:
        temporary = Path(directory)
        pulled = temporary / "pulled"
        distribution = temporary / "distribution"
        pulled.mkdir()
        distribution.mkdir()
        with oras_authentication(credential_provider, {registry}) as registry_config:
            run(
                "oras",
                "pull",
                *oras_registry_args(registry_config),
                "--output",
                str(pulled),
                context.inputs["bundle"],
            )
        archive = pulled / FRONTEND_ARCHIVE
        if not archive.is_file():
            raise DriverError(f"frontend OCI artifact did not contain {FRONTEND_ARCHIVE}")
        safe_extract_bundle(archive, distribution)
        runtime_path = distribution / "runtime-config.json"
        runtime_path.write_bytes(runtime_bytes)
        index_path = distribution / "index.html"
        if not index_path.is_file():
            raise DriverError("frontend OCI artifact did not contain index.html")
        index_text = index_path.read_text()
        run(
            "aws",
            "s3",
            "sync",
            str(distribution),
            f"s3://{context.inputs['bucket']}",
            "--delete",
        )
        # `aws s3 sync` compares size and timestamps by default. Deterministic bundles give
        # index.html a fixed timestamp, and Vite's hashed asset names commonly change without
        # changing the HTML byte length. Always replace the entry point so it cannot keep
        # referring to assets that `sync --delete` just removed.
        run(
            "aws",
            "s3",
            "cp",
            str(index_path),
            f"s3://{context.inputs['bucket']}/index.html",
            "--cache-control",
            "no-cache",
            "--content-type",
            "text/html",
        )
        run(
            "aws",
            "s3",
            "cp",
            str(runtime_path),
            f"s3://{context.inputs['bucket']}/runtime-config.json",
            "--cache-control",
            "no-store",
            "--content-type",
            "application/json",
        )
    invalidation = run(
        "aws",
        "cloudfront",
        "create-invalidation",
        "--distribution-id",
        context.inputs["distributionId"],
        "--paths",
        "/*",
        "--query",
        "Invalidation.Id",
        "--output",
        "text",
        capture=True,
    ).stdout.strip()
    if not invalidation:
        raise DriverError("CloudFront did not return an invalidation ID")
    run(
        "aws",
        "cloudfront",
        "wait",
        "invalidation-completed",
        "--distribution-id",
        context.inputs["distributionId"],
        "--id",
        invalidation,
    )
    served_index = run(
        "curl",
        "--fail",
        "--show-error",
        "--silent",
        "--retry",
        "6",
        "--retry-all-errors",
        "--retry-delay",
        "5",
        context.inputs["url"],
        capture=True,
    ).stdout
    if served_index != index_text:
        raise DriverError("CloudFront did not serve the frontend index from this deployment")
    run(
        "curl",
        "--fail",
        "--show-error",
        "--silent",
        "--retry",
        "6",
        "--retry-all-errors",
        "--retry-delay",
        "5",
        f"{context.inputs['url']}/runtime-config.json",
    )
    return {
        "published": {
            "sourceRevision": context.source_revision,
            "path": context.source_path,
            "bundle": context.inputs["bundle"],
            "artifactDigest": artifact_digest,
            "runtimeConfigHash": runtime_digest,
            "url": context.inputs["url"],
        }
    }


def select_result_fields(*names: str) -> SemanticResultSelector:
    def select(result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise DriverError("driver result must be an object")
        missing = [name for name in names if name not in result]
        if missing:
            raise DriverError(f"driver result is missing semantic fields: {', '.join(missing)}")
        return {name: result[name] for name in names}

    return select


OCI_IMAGES = DriverPlugin(
    version=2,
    reconcile=apply_oci_images,
    semantic_result=select_result_fields("artifacts"),
)
TERRAFORM = DriverPlugin(
    version=2,
    reconcile=apply_terraform,
    verify=verify_terraform,
    semantic_result=select_result_fields("applied", "outputs"),
)
VITE_OCI_BUNDLE = DriverPlugin(
    version=1,
    reconcile=apply_vite_oci_bundle,
    semantic_result=select_result_fields("artifacts"),
)
FRONTEND_S3_CLOUDFRONT = DriverPlugin(
    version=1,
    reconcile=apply_frontend_s3_cloudfront,
    semantic_result=select_result_fields("published"),
)
