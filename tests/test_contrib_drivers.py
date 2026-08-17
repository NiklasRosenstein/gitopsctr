"""Deployment drivers own their optional report artifacts."""

import json
import subprocess
import sys
import tarfile
import tomllib
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from gitopsctr import registry as driver_registry
from gitopsctr.contrib.drivers import (
    _oci,
    frontend_s3_cloudfront,
    kubernetes_manifests,
    oci_images,
    terraform,
    vite_oci_bundle,
)
from gitopsctr.driver import (
    DriverError,
    PlanningCapability,
    PlanningContext,
    ReconciliationCapability,
    ReconciliationContext,
    TeardownContext,
    UnitDriver,
    VerificationCapability,
    VerificationContext,
    VerificationResult,
    VerificationStatus,
)
from gitopsctr.execution import CommandOutput, DriverExecution, TextDriverOutput
from gitopsctr.resource_api import ContractError

DIGEST = "sha256:" + "1" * 64
OTHER_DIGEST = "sha256:" + "2" * 64
REGISTRY = "482956200750.dkr.ecr.eu-west-1.amazonaws.com"


class FakeCommandExecutor:
    def __init__(self, runner):
        self.runner = runner

    def run(
        self,
        args,
        *,
        cwd=None,
        env=None,
        input_text=None,
        output=CommandOutput.STREAM,
        check=True,
        sensitive=False,
    ):
        return self.runner(
            *args,
            cwd=cwd,
            env=env,
            input_text=input_text,
            output=output,
            check=check,
            sensitive=sensitive,
        )


def execution_for(runner) -> DriverExecution:
    transcript = TextDriverOutput(sys.stderr)
    return DriverExecution(output=transcript, commands=FakeCommandExecutor(runner))


def test_contributed_api_entry_points_register_resource_kinds_and_drivers():
    configuration = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    entry_points = configuration["project"]["entry-points"]["gitopsctr.apis"]

    assert entry_points == {
        "artifact.gitopsctr.io/v1/ContainerImages": "gitopsctr.artifacts:CONTAINER_IMAGES",
        "artifact.gitopsctr.io/v1/FrontendBundle": "gitopsctr.artifacts:FRONTEND_BUNDLE",
        "gitopsctr.io/v1/Environment": "gitopsctr.core_api:ENVIRONMENT",
        "gitopsctr.io/v1/Project": "gitopsctr.core_api:PROJECT",
        "gitopsctr.io/v1/Promotion": "gitopsctr.core_api:PROMOTION",
        "gitopsctr.io/v1/Receipt": "gitopsctr.core_api:RECEIPT",
        "gitopsctr.io/v1/Stack": "gitopsctr.core_api:STACK",
        "gitopsctr.io/v1/StackTemplate": "gitopsctr.core_api:STACK_TEMPLATE",
        "inspection.gitopsctr.io/v1/ResourceList": "gitopsctr.inspection_api:RESOURCE_LIST",
        "unit.gitopsctr.io/v1/FrontendS3Cloudfront": "gitopsctr.contrib.drivers.frontend_s3_cloudfront:API_KIND",
        "unit.gitopsctr.io/v1/KubernetesManifests": "gitopsctr.contrib.drivers.kubernetes_manifests:API_KIND",
        "unit.gitopsctr.io/v1/OciImages": "gitopsctr.contrib.drivers.oci_images:API_KIND",
        "unit.gitopsctr.io/v1/Terraform": "gitopsctr.contrib.drivers.terraform:API_KIND",
        "unit.gitopsctr.io/v1/ViteOciBundle": "gitopsctr.contrib.drivers.vite_oci_bundle:API_KIND",
    }
    assert {str(gvk) for gvk in driver_registry.API_KINDS} == set(entry_points)
    assert {plugin.reconcile.__module__ for plugin in driver_registry.RECONCILIATION_DRIVERS.values()} == {
        "gitopsctr.contrib.drivers.frontend_s3_cloudfront",
        "gitopsctr.contrib.drivers.kubernetes_manifests",
        "gitopsctr.contrib.drivers.oci_images",
        "gitopsctr.contrib.drivers.terraform",
        "gitopsctr.contrib.drivers.vite_oci_bundle",
    }
    assert all(isinstance(plugin, UnitDriver) for plugin in driver_registry.UNIT_DRIVERS.values())


def test_driver_capabilities_are_independent_and_explicit():
    assert all(isinstance(plugin, PlanningCapability) for plugin in driver_registry.PLANNING_DRIVERS.values())
    assert isinstance(terraform.DRIVER, VerificationCapability)
    assert isinstance(kubernetes_manifests.DRIVER, VerificationCapability)
    assert not isinstance(oci_images.DRIVER, VerificationCapability)
    assert not isinstance(vite_oci_bundle.DRIVER, VerificationCapability)
    assert not isinstance(frontend_s3_cloudfront.DRIVER, VerificationCapability)
    assert driver_registry.VERIFICATION_DRIVERS == {
        "kubernetes-manifests": kubernetes_manifests.DRIVER,
        "terraform": terraform.DRIVER,
    }


def test_reconciliation_capability_requires_core_operations():
    class IncompletePlugin(UnitDriver, ReconciliationCapability):
        version = 1

    with pytest.raises(TypeError, match="abstract"):
        IncompletePlugin()


def _oci_context(
    tmp_path: Path,
    *,
    credential_provider: object = None,
    repositories: dict[str, str] | None = None,
    targets: dict[str, object] | None = None,
) -> ReconciliationContext:
    publication: dict[str, object] = {
        "targets": targets
        or {
            name: {"type": "registry", "repository": repository}
            for name, repository in (
                repositories
                or {
                    "control": f"{REGISTRY}/example-application-control",
                    "worker": f"{REGISTRY}/example-application-worker",
                }
            ).items()
        }
    }
    if credential_provider is not None:
        publication["credentialProvider"] = credential_provider
    return ReconciliationContext(
        environment="dev",
        desired_root=tmp_path,
        desired_revision="d" * 40,
        source_root=tmp_path,
        source_revision="a" * 40,
        source_path=".",
        unit_name="application-images",
        unit=oci_images.OciImagesDesiredUnit.from_dict(
            {
                "source": {"path": ".", "revision": "a" * 40, "inputHash": "sha256:" + "b" * 64},
                "build": {"dockerfile": "Dockerfile", "platform": "linux/amd64"},
                "publish": publication,
            }
        ),
    )


def _planning_context(context: ReconciliationContext) -> PlanningContext:
    return PlanningContext(
        environment=context.environment,
        desired_root=context.desired_root,
        desired_revision=context.desired_revision,
        source_root=context.source_root,
        source_revision=context.source_revision,
        source_path=context.source_path,
        unit_name=context.unit_name,
        unit=context.unit,
        report=context.report,
        execution=context.execution,
    )


def test_oci_digest_distinguishes_missing_manifest_from_registry_failure(monkeypatch):
    responses = iter(
        (
            subprocess.CompletedProcess((), 0, f"{DIGEST}\n", ""),
            subprocess.CompletedProcess((), 1, "", "manifest unknown"),
            subprocess.CompletedProcess((), 1, "", "unauthorized: authentication required"),
        )
    )
    execution = execution_for(lambda *_args, **_kwargs: next(responses))

    repository = "registry.example.com/team/application"
    assert oci_images.oci_digest(execution, repository, "current") == DIGEST
    assert oci_images.oci_digest(execution, repository, "missing") is None
    with pytest.raises(DriverError, match="unauthorized"):
        oci_images.oci_digest(execution, repository, "private")


def test_oci_digest_rejects_malformed_success(monkeypatch):
    execution = execution_for(lambda *_args, **_kwargs: subprocess.CompletedProcess((), 0, "latest\n", ""))

    with pytest.raises(DriverError, match="invalid digest"):
        oci_images.oci_digest(execution, "registry.example.com/team/application", "latest")


def test_oci_images_uses_optional_aws_ecr_provider_without_persisting_credentials(tmp_path, monkeypatch, capsys):
    commands: list[tuple[tuple[str, ...], dict[str, object]]] = []
    plugins = tmp_path / "cli-plugins"
    plugins.mkdir()

    def fake_run(*args, **kwargs):
        commands.append((args, kwargs))
        if args[:3] == ("aws", "ecr", "get-login-password"):
            return subprocess.CompletedProcess(args, 0, "short-lived-password\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    inspect_environments: list[dict[str, str] | None] = []

    def fake_digest(_execution, _repository, _tag, docker_environment=None):
        inspect_environments.append(docker_environment)
        isolated_plugins = Path(docker_environment["DOCKER_CONFIG"]) / "cli-plugins"
        assert isolated_plugins.resolve() == plugins.resolve()
        return DIGEST

    monkeypatch.setattr(oci_images, "oci_digest", fake_digest)
    monkeypatch.setattr(_oci, "docker_cli_plugins", lambda: plugins)
    monkeypatch.setenv("AWS_PROFILE", "example-profile")

    context = replace(
        _oci_context(tmp_path, credential_provider={"type": "aws-ecr"}),
        execution=execution_for(fake_run),
    )
    result = oci_images.DRIVER.reconcile(context)

    aws_command = next(command for command, _ in commands if command[0] == "aws")
    assert aws_command == ("aws", "ecr", "get-login-password", "--region", "eu-west-1")
    assert "--profile" not in aws_command
    logins = [(command, kwargs) for command, kwargs in commands if command[:2] == ("docker", "login")]
    assert len(logins) == 1
    login, login_kwargs = logins[0]
    assert login[-1] == REGISTRY
    assert "short-lived-password" not in login
    assert login_kwargs["input_text"] == "short-lived-password\n"
    docker_config = login_kwargs["env"]["DOCKER_CONFIG"]
    assert docker_config
    assert all(environment["DOCKER_CONFIG"] == docker_config for environment in inspect_environments)
    assert "short-lived-password" not in repr(result)
    status = capsys.readouterr().err
    assert f"| AUTH {REGISTRY}: aws-ecr via AWS profile 'example-profile' (region eu-west-1)" in status
    assert (f"| LOGIN {REGISTRY}: Docker login as AWS via password stdin (isolated temporary config)") in status
    assert "short-lived-password" not in status


def test_oci_images_without_provider_uses_existing_docker_auth(tmp_path, monkeypatch, capsys):
    def unexpected_run(*args, **_kwargs):
        raise AssertionError(f"did not expect command: {args}")

    environments: list[dict[str, str] | None] = []

    def fake_digest(_execution, _repository, _tag, docker_environment=None):
        environments.append(docker_environment)
        return DIGEST

    monkeypatch.setattr(oci_images, "oci_digest", fake_digest)

    result = oci_images.DRIVER.reconcile(replace(_oci_context(tmp_path), execution=execution_for(unexpected_run)))

    assert environments == [None, None]
    assert result.artifacts["containers"]["images"]["control"]["uri"] == (
        f"{REGISTRY}/example-application-control@{DIGEST}"
    )
    assert f"| AUTH {REGISTRY}: existing Docker credentials or anonymous access" in capsys.readouterr().err


def test_oci_images_artifact_records_local_and_qualified_producer_names(tmp_path, monkeypatch):
    monkeypatch.setattr(oci_images, "oci_digest", lambda *_args, **_kwargs: DIGEST)
    context = replace(
        _oci_context(tmp_path),
        unit_name="image",
        qualified_name="application/image",
        execution=execution_for(lambda *_args, **_kwargs: None),
    )

    result = oci_images.DRIVER.reconcile(context)

    producer = result.artifacts["containers"]["producer"]
    assert producer["name"] == "image"
    assert producer["qualifiedName"] == "application/image"


@pytest.mark.parametrize(
    ("provider", "targets", "message"),
    (
        ({"type": "unknown"}, None, "unknown credential provider"),
        ({"type": "aws-ecr", "profile": "example-profile"}, None, "unsupported fields"),
        (
            {"type": "aws-ecr"},
            {"control": {"type": "registry", "repository": "registry.example.com/team/control"}},
            "private ECR registry",
        ),
        (None, {"control": {"type": "registry", "repository": "team/control"}}, "not registry-qualified"),
        (
            None,
            {"control": {"type": "registry", "repository": f"{REGISTRY}/team/control:latest"}},
            "tag-free",
        ),
        (None, {"control": {"type": "kind", "cluster": ""}}, "requires cluster"),
        (None, {"control": {"type": "minikube"}}, "requires profile"),
        (None, {"control": {"type": "unknown"}}, "unknown type"),
        (
            {"type": "aws-ecr"},
            {"control": {"type": "kind", "cluster": "dev"}},
            "requires at least one registry",
        ),
    ),
)
def test_oci_images_rejects_invalid_publication_target_configuration(tmp_path, monkeypatch, provider, targets, message):
    no_commands = execution_for(
        lambda *_args, **_kwargs: pytest.fail("invalid configuration must fail before commands run")
    )

    with pytest.raises((DriverError, ValueError)):
        oci_images.DRIVER.reconcile(
            replace(
                _oci_context(
                    tmp_path,
                    credential_provider=provider,
                    targets=targets,
                ),
                execution=no_commands,
            )
        )


def test_oci_images_plan_builds_without_requesting_credentials(tmp_path, monkeypatch):
    commands: list[tuple[str, ...]] = []

    def fake_run(*args, **_kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(
        oci_images,
        "oci_digest",
        lambda *_args, **_kwargs: pytest.fail("planning must not inspect registries"),
    )

    context = _planning_context(
        replace(
            _oci_context(tmp_path, credential_provider={"type": "aws-ecr"}),
            execution=execution_for(fake_run),
        )
    )
    assert oci_images.DRIVER.plan(context) is None
    assert commands == [
        (
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "--provenance=false",
            "--file",
            str(tmp_path / "Dockerfile"),
            "--tag",
            f"application-images:{'b' * 64}",
            str(tmp_path),
        )
    ]


def test_oci_images_recovers_partial_publication_without_rebuilding(tmp_path, monkeypatch):
    commands: list[tuple[str, ...]] = []
    calls: dict[tuple[str, str], int] = {}

    def fake_run(*args, **_kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_digest(_execution, repository, tag, _docker_environment=None):
        key = (repository, tag)
        calls[key] = calls.get(key, 0) + 1
        if repository.endswith("-control"):
            return DIGEST
        return DIGEST if calls[key] > 1 else None

    monkeypatch.setattr(oci_images, "oci_digest", fake_digest)

    result = oci_images.DRIVER.reconcile(replace(_oci_context(tmp_path), execution=execution_for(fake_run)))

    assert not any(command[:2] == ("docker", "build") for command in commands)
    assert any(command[:2] == ("docker", "pull") for command in commands)
    assert len([command for command in commands if command[:2] == ("docker", "push")]) == 1
    artifacts = result.artifacts["containers"]["images"]
    assert {artifact["uri"].rsplit("@", 1)[1] for artifact in artifacts.values()} == {DIGEST}


def test_oci_images_rejects_disagreeing_repository_digests(tmp_path, monkeypatch):
    monkeypatch.setattr(
        oci_images,
        "oci_digest",
        lambda _execution, repository, *_args, **_kwargs: DIGEST if repository.endswith("-control") else OTHER_DIGEST,
    )

    with pytest.raises(DriverError, match="disagree"):
        oci_images.DRIVER.reconcile(
            replace(_oci_context(tmp_path), execution=execution_for(lambda *_args, **_kwargs: None))
        )


@pytest.mark.parametrize(
    ("target", "expected_load"),
    (
        (
            {"type": "kind", "cluster": "example-kind"},
            ("kind", "load", "docker-image", f"application-images:{'b' * 64}", "--name", "example-kind"),
        ),
        (
            {"type": "minikube", "profile": "example-minikube"},
            (
                "minikube",
                "--profile",
                "example-minikube",
                "image",
                "load",
                f"application-images:{'b' * 64}",
                "--daemon",
            ),
        ),
    ),
)
def test_oci_images_builds_once_and_exports_to_local_cluster(tmp_path, target, expected_load):
    commands: list[tuple[str, ...]] = []

    def fake_run(*args, **_kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    context = replace(
        _oci_context(tmp_path, targets={"application": target}),
        execution=execution_for(fake_run),
    )
    result = oci_images.DRIVER.reconcile(context)

    assert len([command for command in commands if command[:2] == ("docker", "build")]) == 1
    assert commands[-1] == expected_load
    assert result.artifacts["containers"]["images"]["application"] == {
        "uri": f"application-images:{'b' * 64}",
    }


def test_oci_images_reuses_registry_image_when_exporting_to_cluster(tmp_path, monkeypatch):
    commands: list[tuple[str, ...]] = []

    def fake_run(*args, **_kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(oci_images, "oci_digest", lambda *_args, **_kwargs: DIGEST)
    targets = {
        "registry": {"type": "registry", "repository": f"{REGISTRY}/example-application"},
        "cluster": {"type": "kind", "cluster": "example"},
    }
    result = oci_images.DRIVER.reconcile(
        replace(_oci_context(tmp_path, targets=targets), execution=execution_for(fake_run))
    )

    assert not any(command[:2] == ("docker", "build") for command in commands)
    assert any(command[:2] == ("docker", "pull") for command in commands)
    assert commands[-1][:3] == ("kind", "load", "docker-image")
    assert result.artifacts["containers"]["images"]["registry"]["uri"].endswith(f"@{DIGEST}")


def test_frontend_bundle_reuses_matching_oci_artifact_without_building(tmp_path, monkeypatch):
    context = ReconciliationContext(
        environment="dev",
        desired_root=tmp_path,
        desired_revision="d" * 40,
        source_root=tmp_path,
        source_revision="a" * 40,
        source_path="frontend",
        unit_name="frontend-bundle",
        unit=vite_oci_bundle.ViteOciBundleDesiredUnit.from_dict(
            {
                "source": {"path": "frontend", "revision": "a" * 40, "inputHash": "sha256:" + "b" * 64},
                "build": {"nodeVersion": "24"},
                "publish": {
                    "repository": f"{REGISTRY}/example-application-frontend",
                    "credentialProvider": {"type": "aws-ecr"},
                },
            }
        ),
    )
    monkeypatch.setattr(
        vite_oci_bundle,
        "oras_digest",
        lambda *_args, **_kwargs: DIGEST,
    )

    @contextmanager
    def fake_authentication(*_args, **_kwargs):
        yield None

    monkeypatch.setattr(vite_oci_bundle, "oras_authentication", fake_authentication)
    context = replace(
        context,
        execution=execution_for(lambda *_args, **_kwargs: pytest.fail("existing artifact must skip all commands")),
    )

    result = vite_oci_bundle.DRIVER.reconcile(context)

    assert result.artifacts["frontend"]["bundle"]["uri"] == (f"{REGISTRY}/example-application-frontend@{DIGEST}")


def test_frontend_bundle_archive_is_deterministic_and_contains_dist_tree(tmp_path):
    distribution = tmp_path / "dist"
    (distribution / "assets").mkdir(parents=True)
    (distribution / "index.html").write_text("<main>example</main>")
    (distribution / "assets/app.js").write_text("console.log('example')")
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    vite_oci_bundle.deterministic_archive(distribution, first)
    vite_oci_bundle.deterministic_archive(distribution, second)

    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, "r:gz") as archive:
        assert set(archive.getnames()) == {"assets", "assets/app.js", "index.html"}


def test_frontend_s3_cloudfront_accepts_source_less_units():
    authored = frontend_s3_cloudfront.FrontendUnit.from_dict(
        {
            "inputs": {
                "bundle": f"{REGISTRY}/example-application-frontend@{DIGEST}",
                "bucket": "frontend-bucket",
                "distributionId": "distribution-id",
                "url": "https://frontend.example.test",
                "runtimeConfig": {
                    "schema": 1,
                    "apiBase": "https://api.example.test",
                    "auth": {
                        "mode": "cognito",
                        "issuer": "https://issuer.example.test",
                        "clientId": "client-id",
                    },
                },
            }
        }
    )

    assert authored.source is None
    frontend_s3_cloudfront.DRIVER.unit_contract.validate(authored.to_dict())


@pytest.mark.parametrize("stale_index", [False, True])
def test_frontend_deploy_overwrites_index_and_verifies_cloudfront(tmp_path, monkeypatch, stale_index):
    distribution = tmp_path / "dist"
    (distribution / "assets").mkdir(parents=True)
    index_text = '<script type="module" src="/assets/app-new.js"></script>\n'
    (distribution / "index.html").write_text(index_text)
    (distribution / "assets/app-new.js").write_text("console.log('new')")
    bundle = tmp_path / vite_oci_bundle.FRONTEND_ARCHIVE
    vite_oci_bundle.deterministic_archive(distribution, bundle)
    commands = []

    @contextmanager
    def fake_authentication(*_args, **_kwargs):
        yield None

    def fake_run(*args, **kwargs):
        commands.append(args)
        if args[:2] == ("oras", "pull"):
            output = Path(args[args.index("--output") + 1])
            (output / vite_oci_bundle.FRONTEND_ARCHIVE).write_bytes(bundle.read_bytes())
        if args[:4] == ("aws", "cloudfront", "create-invalidation", "--distribution-id"):
            return subprocess.CompletedProcess(args, 0, "invalidation-id\n", "")
        if args[0] == "curl" and args[-1] == "https://frontend.example.test":
            served = '<script type="module" src="/assets/app-old.js"></script>\n'
            if not stale_index:
                served = index_text
            return subprocess.CompletedProcess(args, 0, served, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(frontend_s3_cloudfront, "oras_authentication", fake_authentication)
    context = ReconciliationContext(
        environment="dev",
        desired_root=tmp_path,
        desired_revision="d" * 40,
        source_root=tmp_path,
        source_revision="a" * 40,
        source_path="scripts/deployment_drivers.py",
        unit_name="frontend",
        unit=frontend_s3_cloudfront.FrontendDesiredUnit.from_dict(
            {
                "source": {"path": "scripts/deployment_drivers.py", "revision": "a" * 40},
                "pull": {},
                "inputs": {
                    "bundle": f"{REGISTRY}/example-application-frontend@{DIGEST}",
                    "bucket": "frontend-bucket",
                    "distributionId": "distribution-id",
                    "url": "https://frontend.example.test",
                    "runtimeConfig": {
                        "schema": 1,
                        "apiBase": "https://api.example.test",
                        "auth": {
                            "mode": "cognito",
                            "issuer": "https://issuer.example.test",
                            "clientId": "client-id",
                        },
                    },
                },
            }
        ),
        execution=execution_for(fake_run),
    )

    if stale_index:
        with pytest.raises(DriverError, match="did not serve"):
            frontend_s3_cloudfront.DRIVER.reconcile(context)
    else:
        result = frontend_s3_cloudfront.DRIVER.reconcile(context)
        assert result.result.published.bundle == context.unit.inputs.bundle

    index_upload = next(
        command for command in commands if command[:3] == ("aws", "s3", "cp") and command[4].endswith("/index.html")
    )
    assert index_upload[-4:] == ("--cache-control", "no-cache", "--content-type", "text/html")


def test_hosted_frontend_runtime_config_requires_cognito_contract():
    with pytest.raises(DriverError, match="Cognito"):
        frontend_s3_cloudfront.runtime_configuration(
            {
                "runtimeConfig": {
                    "schema": 1,
                    "apiBase": "https://api.example.test",
                    "auth": {"mode": "local", "issuer": "unused", "clientId": "unused"},
                }
            }
        )


def _terraform_context(
    tmp_path: Path,
    report: Path,
    backend: dict[str, object] | None = None,
) -> ReconciliationContext:
    terraform_root = tmp_path / "source/infra/deploy"
    terraform_root.mkdir(parents=True)
    return ReconciliationContext(
        environment="dev",
        desired_root=tmp_path,
        desired_revision="d" * 40,
        source_root=tmp_path / "source",
        source_revision="a" * 40,
        source_path="infra/deploy",
        unit_name="terraform",
        unit=terraform.TerraformDesiredUnit.from_dict(
            {
                "source": {"path": "infra/deploy", "revision": "a" * 40},
                "terraform": {
                    "backend": backend or {"key": "application/dev.tfstate"},
                    "variables": {"environment": "dev"},
                    "observeOutputs": [],
                },
            }
        ),
        report=report,
    )


def test_terraform_rejects_structured_backend_values(tmp_path):
    with pytest.raises(ContractError):
        terraform.DRIVER.desired_unit_contract.parse(
            {
                "source": {"path": "infra/deploy", "revision": "a" * 40},
                "terraform": {
                    "backend": {"path": ["not", "a", "scalar"]},
                    "variables": {"environment": "dev"},
                    "observeOutputs": [],
                },
            }
        )


def test_terraform_plan_saves_binary_plan_and_rendered_report(tmp_path, monkeypatch):
    report = tmp_path / "report"
    state = tmp_path / "state/demo.tfstate"
    commands: list[tuple[str, ...]] = []
    variable_file: Path | None = None

    def fake_run(*args, **kwargs):
        nonlocal variable_file
        commands.append(args)
        if args[1] == "plan":
            variable_file = Path(next(value for value in args if value.startswith("-var-file=")).split("=", 1)[1])
            assert json.loads(variable_file.read_text()) == {"environment": "dev"}
            assert "TF_VAR_environment" not in kwargs["env"]
            plan = Path(next(value for value in args if value.startswith("-out=")).split("=", 1)[1])
            plan.write_bytes(b"saved plan")
            return subprocess.CompletedProcess(args, 0, "planning\n", "")
        if args[1] == "show":
            return subprocess.CompletedProcess(args, 0, "Plan: 1 to add, 0 to change, 0 to destroy.\n", "")
        return subprocess.CompletedProcess(args, 0, "initialized\n", "")

    reconciliation = replace(
        _terraform_context(tmp_path, report, {"path": str(state)}),
        execution=execution_for(fake_run),
    )
    result = terraform.DRIVER.plan(_planning_context(reconciliation))

    assert result is None
    assert (report / "plan.tfplan").read_bytes() == b"saved plan"
    assert (report / "plan.txt").read_text() == ("Plan: 1 to add, 0 to change, 0 to destroy.\n")
    init = next(command for command in commands if command[1] == "init")
    assert init == ("terraform", "init", f"-backend-config=path={state}")
    plan_command = next(command for command in commands if command[1] == "plan")
    assert variable_file is not None
    assert f"-var-file={variable_file}" in plan_command
    assert "-refresh=false" in plan_command
    assert "-lock=false" in plan_command
    assert "-input=false" in plan_command
    assert "-no-color" in plan_command
    assert not variable_file.exists()


def test_terraform_teardown_uses_resolved_variables(tmp_path):
    commands: list[tuple[str, ...]] = []
    variable_file: Path | None = None

    def fake_run(*args, **_kwargs):
        nonlocal variable_file
        commands.append(args)
        if args[1] == "destroy":
            variable_file = Path(next(value for value in args if value.startswith("-var-file=")).split("=", 1)[1])
            assert json.loads(variable_file.read_text()) == {"environment": "dev"}
        return subprocess.CompletedProcess(args, 0, "", "")

    reconciliation = _terraform_context(tmp_path, tmp_path / "report")
    result = terraform.DRIVER.teardown(
        TeardownContext(
            environment=reconciliation.environment,
            desired_root=reconciliation.desired_root,
            desired_revision=reconciliation.desired_revision,
            source_root=reconciliation.source_root,
            source_revision=reconciliation.source_revision,
            source_path=reconciliation.source_path,
            unit_name=reconciliation.unit_name,
            unit=reconciliation.unit,
            resource_uid="d1-terraform",
            deletion_generation=1,
            execution=execution_for(fake_run),
        )
    )

    assert result.details == {"resourceUid": "d1-terraform", "deletionGeneration": 1}
    assert variable_file is not None
    destroy = next(command for command in commands if command[1] == "destroy")
    assert f"-var-file={variable_file}" in destroy
    assert not variable_file.exists()


def test_terraform_report_contains_plan_failure_diagnostics(tmp_path, monkeypatch):
    report = tmp_path / "report"

    def fake_run(*args, **_kwargs):
        if args[1] == "plan":
            raise subprocess.CalledProcessError(
                1,
                args,
                output="Error: speculative plan failed\n",
                stderr="",
            )
        return subprocess.CompletedProcess(args, 0, "initialized\n", "")

    with pytest.raises(subprocess.CalledProcessError):
        reconciliation = replace(_terraform_context(tmp_path, report), execution=execution_for(fake_run))
        terraform.DRIVER.plan(_planning_context(reconciliation))

    assert (report / "plan.txt").read_text() == "Error: speculative plan failed\n"
    assert not (report / "plan.tfplan").exists()


@pytest.mark.parametrize(
    ("exit_code", "expected_status"),
    (
        (0, VerificationStatus.CLEAN),
        (2, VerificationStatus.DRIFT),
    ),
)
def test_terraform_verification_uses_refresh_enabled_read_only_saved_plan(
    tmp_path, monkeypatch, exit_code, expected_status
):
    report = tmp_path / "report"
    commands: list[tuple[tuple[str, ...], dict[str, object]]] = []
    variable_file: Path | None = None

    def fake_run(*args, **kwargs):
        nonlocal variable_file
        commands.append((args, kwargs))
        if args[1] == "plan":
            variable_file = Path(next(value for value in args if value.startswith("-var-file=")).split("=", 1)[1])
            assert json.loads(variable_file.read_text()) == {"environment": "dev"}
            assert "TF_VAR_environment" not in kwargs["env"]
            plan = Path(next(value for value in args if value.startswith("-out=")).split("=", 1)[1])
            plan.write_bytes(b"verification plan")
            return subprocess.CompletedProcess(args, exit_code, "verification output\n", "")
        return subprocess.CompletedProcess(args, 0, "initialized\n", "")

    reconciliation = replace(_terraform_context(tmp_path, report), execution=execution_for(fake_run))
    result = driver_registry.VERIFICATION_DRIVERS["terraform"].verify(
        VerificationContext(
            environment=reconciliation.environment,
            desired_root=reconciliation.desired_root,
            desired_revision=reconciliation.desired_revision,
            source_root=reconciliation.source_root,
            source_revision=reconciliation.source_revision,
            source_path=reconciliation.source_path,
            unit_name=reconciliation.unit_name,
            unit=reconciliation.unit,
            report=reconciliation.report,
            execution=reconciliation.execution,
        )
    )

    assert result == VerificationResult(expected_status)
    assert variable_file is not None
    assert (report / "verify.tfplan").read_bytes() == b"verification plan"
    assert (report / "verify.txt").read_text() == "verification output\n"
    plan_command, plan_kwargs = next(item for item in commands if item[0][1] == "plan")
    assert f"-var-file={variable_file}" in plan_command
    assert "-detailed-exitcode" in plan_command
    assert "-input=false" in plan_command
    assert "-no-color" in plan_command
    assert not any(value.startswith("-refresh=") for value in plan_command)
    assert "-lock=false" not in plan_command
    assert plan_kwargs["output"] is CommandOutput.TEE
    assert plan_kwargs["check"] is False
    assert not any(command[1] == "apply" for command, _ in commands)
    assert not variable_file.exists()


def test_terraform_verification_turns_other_exit_codes_into_driver_errors(tmp_path, monkeypatch):
    report = tmp_path / "report"

    def fake_run(*args, **_kwargs):
        if args[1] == "plan":
            return subprocess.CompletedProcess(args, 1, "", "Error: state lock failed\n")
        return subprocess.CompletedProcess(args, 0, "initialized\n", "")

    with pytest.raises(DriverError, match="state lock failed"):
        terraform.DRIVER.verify(replace(_terraform_context(tmp_path, report), execution=execution_for(fake_run)))

    assert (report / "verify.txt").read_text() == "Error: state lock failed\n"


@pytest.mark.parametrize(
    ("driver", "result", "expected"),
    (
        (
            "oci-images",
            {},
            {},
        ),
        (
            "vite-oci-bundle",
            {},
            {},
        ),
        (
            "terraform",
            {
                "applied": {"sourceRevision": "a" * 40},
                "outputs": {"url": "https://example.test"},
                "controller": {"workflow_url": "ignored"},
                "planned": {"ignored": True},
            },
            {
                "applied": {"sourceRevision": "a" * 40},
                "outputs": {"url": "https://example.test"},
            },
        ),
        (
            "frontend-s3-cloudfront",
            {"published": {"url": "https://example.test"}, "desired": {"ignored": True}},
            {"published": {"url": "https://example.test"}},
        ),
    ),
)
def test_driver_semantics_select_only_driver_defined_result_fields(driver, result, expected):
    assert driver_registry.semantic_reconciliation_result(driver, result) == expected


def test_every_reconciliation_driver_defines_result_semantics():
    assert set(driver_registry.UNIT_DRIVERS) == set(driver_registry.RECONCILIATION_DRIVERS)
    assert driver_registry.load_unit_drivers(driver_registry.API_KINDS) == driver_registry.UNIT_DRIVERS


@pytest.mark.parametrize(
    ("driver", "result", "message"),
    (
        ("unknown", {}, "does not support reconciliation"),
        ("terraform", {"applied": {}}, "missing semantic fields: outputs"),
        ("oci-images", None, "receipt result must be empty"),
    ),
)
def test_driver_semantics_fail_loudly_for_unknown_or_incomplete_results(driver, result, message):
    with pytest.raises(DriverError, match=message):
        driver_registry.semantic_reconciliation_result(driver, result)
