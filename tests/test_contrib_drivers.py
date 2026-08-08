"""Deployment drivers own their optional report artifacts."""

import subprocess
import tarfile
import tomllib
from contextlib import contextmanager
from pathlib import Path

import pytest

from gitopsctr import driver as driver_registry
from gitopsctr.contrib.driver import _oci, frontend_s3_cloudfront, oci_images, terraform, vite_oci_bundle
from gitopsctr.driver import (
    Driver,
    DriverContext,
    DriverError,
    VerificationCapability,
    VerificationResult,
    VerificationStatus,
)

DIGEST = "sha256:" + "1" * 64
OTHER_DIGEST = "sha256:" + "2" * 64
REGISTRY = "482956200750.dkr.ecr.eu-west-1.amazonaws.com"


def test_contributed_driver_entry_points_load_one_module_per_driver():
    configuration = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    entry_points = configuration["project"]["entry-points"]["gitopsctr.drivers"]

    assert entry_points == {
        "frontend-s3-cloudfront": "gitopsctr.contrib.driver.frontend_s3_cloudfront:PLUGIN",
        "oci-images": "gitopsctr.contrib.driver.oci_images:PLUGIN",
        "terraform": "gitopsctr.contrib.driver.terraform:PLUGIN",
        "vite-oci-bundle": "gitopsctr.contrib.driver.vite_oci_bundle:PLUGIN",
    }
    assert {plugin.reconcile.__module__ for plugin in driver_registry.DRIVER_PLUGINS.values()} == {
        "gitopsctr.contrib.driver.frontend_s3_cloudfront",
        "gitopsctr.contrib.driver.oci_images",
        "gitopsctr.contrib.driver.terraform",
        "gitopsctr.contrib.driver.vite_oci_bundle",
    }
    assert all(isinstance(plugin, Driver) for plugin in driver_registry.DRIVER_PLUGINS.values())


def test_driver_capabilities_are_independent_and_explicit():
    assert isinstance(terraform.PLUGIN, VerificationCapability)
    assert not isinstance(oci_images.PLUGIN, VerificationCapability)
    assert not isinstance(vite_oci_bundle.PLUGIN, VerificationCapability)
    assert not isinstance(frontend_s3_cloudfront.PLUGIN, VerificationCapability)
    assert driver_registry.VERIFICATION_DRIVERS == {"terraform": terraform.PLUGIN.verify}


def test_driver_base_class_requires_core_operations():
    class IncompleteDriver(Driver):
        version = 1

    with pytest.raises(TypeError, match="abstract"):
        IncompleteDriver()


def _oci_context(
    tmp_path: Path,
    *,
    credential_provider: object = None,
    repositories: dict[str, str] | None = None,
    dry: bool = False,
) -> DriverContext:
    publication: dict[str, object] = {
        "repositories": repositories
        or {
            "control": f"{REGISTRY}/example-application-control",
            "worker": f"{REGISTRY}/example-application-worker",
        }
    }
    if credential_provider is not None:
        publication["credentialProvider"] = credential_provider
    return DriverContext(
        source_root=tmp_path,
        source_revision="a" * 40,
        source_path=".",
        unit={
            "name": "application-images",
            "driver": "oci-images",
            "source": {"inputHash": "sha256:" + "b" * 64},
            "build": {"dockerfile": "Dockerfile", "platform": "linux/amd64"},
            "publish": publication,
            "artifacts": ["containers.json"],
        },
        inputs={},
        dry=dry,
    )


def test_oci_digest_distinguishes_missing_manifest_from_registry_failure(monkeypatch):
    responses = iter(
        (
            subprocess.CompletedProcess((), 0, f"{DIGEST}\n", ""),
            subprocess.CompletedProcess((), 1, "", "manifest unknown"),
            subprocess.CompletedProcess((), 1, "", "unauthorized: authentication required"),
        )
    )
    monkeypatch.setattr(oci_images.subprocess, "run", lambda *_args, **_kwargs: next(responses))

    repository = "registry.example.com/team/application"
    assert oci_images.oci_digest(repository, "current") == DIGEST
    assert oci_images.oci_digest(repository, "missing") is None
    with pytest.raises(DriverError, match="unauthorized"):
        oci_images.oci_digest(repository, "private")


def test_oci_digest_rejects_malformed_success(monkeypatch):
    monkeypatch.setattr(
        oci_images.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess((), 0, "latest\n", ""),
    )

    with pytest.raises(DriverError, match="invalid digest"):
        oci_images.oci_digest("registry.example.com/team/application", "latest")


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

    def fake_digest(_repository, _tag, docker_environment=None):
        inspect_environments.append(docker_environment)
        isolated_plugins = Path(docker_environment["DOCKER_CONFIG"]) / "cli-plugins"
        assert isolated_plugins.resolve() == plugins.resolve()
        return DIGEST

    monkeypatch.setattr(oci_images, "run", fake_run)
    monkeypatch.setattr(oci_images, "oci_digest", fake_digest)
    monkeypatch.setattr(_oci, "run", fake_run)
    monkeypatch.setattr(_oci, "docker_cli_plugins", lambda: plugins)
    monkeypatch.setenv("AWS_PROFILE", "example-profile")

    result = oci_images.PLUGIN.reconcile(_oci_context(tmp_path, credential_provider={"type": "aws-ecr"}))

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
    assert f"AUTH    {REGISTRY}: aws-ecr via AWS profile 'example-profile' (region eu-west-1)" in status
    assert (f"LOGIN   {REGISTRY}: Docker login as AWS via password stdin (isolated temporary config)") in status
    assert "short-lived-password" not in status


def test_oci_images_without_provider_uses_existing_docker_auth(tmp_path, monkeypatch, capsys):
    def unexpected_run(*args, **_kwargs):
        raise AssertionError(f"did not expect command: {args}")

    environments: list[dict[str, str] | None] = []

    def fake_digest(_repository, _tag, docker_environment=None):
        environments.append(docker_environment)
        return DIGEST

    monkeypatch.setattr(oci_images, "run", unexpected_run)
    monkeypatch.setattr(oci_images, "oci_digest", fake_digest)

    result = oci_images.PLUGIN.reconcile(_oci_context(tmp_path))

    assert environments == [None, None]
    assert result["artifacts"]["containers.json"]["artifacts"]["control"]["uri"] == (
        f"{REGISTRY}/example-application-control@{DIGEST}"
    )
    assert f"AUTH    {REGISTRY}: existing Docker credentials or anonymous access" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("provider", "repositories", "message"),
    (
        ({"type": "unknown"}, None, "unknown credential provider"),
        ({"type": "aws-ecr", "profile": "example-profile"}, None, "unsupported fields"),
        (
            {"type": "aws-ecr"},
            {"control": "registry.example.com/team/control"},
            "private ECR registry",
        ),
        (None, {"control": "team/control"}, "not registry-qualified"),
        (None, {"control": f"{REGISTRY}/team/control:latest"}, "tag-free"),
    ),
)
def test_oci_images_rejects_invalid_provider_and_repository_configuration(
    tmp_path, monkeypatch, provider, repositories, message
):
    monkeypatch.setattr(
        oci_images,
        "run",
        lambda *_args, **_kwargs: pytest.fail("invalid configuration must fail before commands run"),
    )

    with pytest.raises(DriverError, match=message):
        oci_images.PLUGIN.reconcile(
            _oci_context(
                tmp_path,
                credential_provider=provider,
                repositories=repositories,
                dry=True,
            )
        )


def test_oci_images_dry_run_validates_but_does_not_request_credentials(tmp_path, monkeypatch):
    commands: list[tuple[str, ...]] = []

    def fake_run(*args, **_kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(oci_images, "run", fake_run)
    monkeypatch.setattr(
        oci_images,
        "oci_digest",
        lambda *_args, **_kwargs: pytest.fail("dry reconciliation must not inspect registries"),
    )

    assert oci_images.PLUGIN.reconcile(_oci_context(tmp_path, credential_provider={"type": "aws-ecr"}, dry=True)) == {}
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

    def fake_digest(repository, tag, _docker_environment=None):
        key = (repository, tag)
        calls[key] = calls.get(key, 0) + 1
        if repository.endswith("-control"):
            return DIGEST
        return DIGEST if calls[key] > 1 else None

    monkeypatch.setattr(oci_images, "run", fake_run)
    monkeypatch.setattr(oci_images, "oci_digest", fake_digest)

    result = oci_images.PLUGIN.reconcile(_oci_context(tmp_path))

    assert not any(command[:2] == ("docker", "build") for command in commands)
    assert any(command[:2] == ("docker", "pull") for command in commands)
    assert len([command for command in commands if command[:2] == ("docker", "push")]) == 1
    artifacts = result["artifacts"]["containers.json"]["artifacts"]
    assert {artifact["uri"].rsplit("@", 1)[1] for artifact in artifacts.values()} == {DIGEST}


def test_oci_images_rejects_disagreeing_repository_digests(tmp_path, monkeypatch):
    monkeypatch.setattr(oci_images, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        oci_images,
        "oci_digest",
        lambda repository, *_args, **_kwargs: DIGEST if repository.endswith("-control") else OTHER_DIGEST,
    )

    with pytest.raises(DriverError, match="disagree"):
        oci_images.PLUGIN.reconcile(_oci_context(tmp_path))


def test_frontend_bundle_reuses_matching_oci_artifact_without_building(tmp_path, monkeypatch):
    context = DriverContext(
        source_root=tmp_path,
        source_revision="a" * 40,
        source_path="frontend",
        unit={
            "name": "frontend-bundle",
            "driver": "vite-oci-bundle",
            "source": {"inputHash": "sha256:" + "b" * 64},
            "build": {"nodeVersion": "24"},
            "publish": {
                "repository": f"{REGISTRY}/example-application-frontend",
                "credentialProvider": {"type": "aws-ecr"},
            },
            "artifacts": ["frontend.json"],
        },
        inputs={},
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
    monkeypatch.setattr(
        vite_oci_bundle,
        "run",
        lambda *_args, **_kwargs: pytest.fail("existing artifact must skip all commands"),
    )

    result = vite_oci_bundle.PLUGIN.reconcile(context)

    assert result["artifacts"]["frontend.json"]["artifacts"]["bundle"]["uri"] == (
        f"{REGISTRY}/example-application-frontend@{DIGEST}"
    )


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
    monkeypatch.setattr(frontend_s3_cloudfront, "run", fake_run)
    context = DriverContext(
        source_root=tmp_path,
        source_revision="a" * 40,
        source_path="scripts/deployment_drivers.py",
        unit={"pull": {}},
        inputs={
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
    )

    if stale_index:
        with pytest.raises(DriverError, match="did not serve"):
            frontend_s3_cloudfront.PLUGIN.reconcile(context)
    else:
        result = frontend_s3_cloudfront.PLUGIN.reconcile(context)
        assert result["published"]["bundle"] == context.inputs["bundle"]

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


def _terraform_context(tmp_path: Path, report: Path) -> DriverContext:
    terraform_root = tmp_path / "source/infra/deploy"
    terraform_root.mkdir(parents=True)
    return DriverContext(
        source_root=tmp_path / "source",
        source_revision="a" * 40,
        source_path="infra/deploy",
        unit={
            "terraform": {
                "backend": {"key": "application/dev.tfstate"},
                "variables": {"environment": "dev"},
                "observeOutputs": [],
            }
        },
        inputs={},
        dry=True,
        report=report,
    )


def test_terraform_dry_run_saves_binary_plan_and_rendered_report(tmp_path, monkeypatch):
    report = tmp_path / "report"
    commands: list[tuple[str, ...]] = []

    def fake_run(*args, **_kwargs):
        commands.append(args)
        if args[1] == "plan":
            plan = Path(next(value for value in args if value.startswith("-out=")).split("=", 1)[1])
            plan.write_bytes(b"saved plan")
            return subprocess.CompletedProcess(args, 0, "planning\n", "")
        if args[1] == "show":
            return subprocess.CompletedProcess(args, 0, "Plan: 1 to add, 0 to change, 0 to destroy.\n", "")
        return subprocess.CompletedProcess(args, 0, "initialized\n", "")

    monkeypatch.setattr(terraform, "run", fake_run)

    result = terraform.PLUGIN.reconcile(_terraform_context(tmp_path, report))

    assert result == {"planned": {"sourceRevision": "a" * 40}}
    assert (report / "plan.tfplan").read_bytes() == b"saved plan"
    assert (report / "plan.txt").read_text() == ("Plan: 1 to add, 0 to change, 0 to destroy.\n")
    plan_command = next(command for command in commands if command[1] == "plan")
    assert "-refresh=false" in plan_command
    assert "-lock=false" in plan_command
    assert "-input=false" in plan_command
    assert "-no-color" in plan_command


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

    monkeypatch.setattr(terraform, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        terraform.PLUGIN.reconcile(_terraform_context(tmp_path, report))

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

    def fake_run(*args, **kwargs):
        commands.append((args, kwargs))
        if args[1] == "plan":
            plan = Path(next(value for value in args if value.startswith("-out=")).split("=", 1)[1])
            plan.write_bytes(b"verification plan")
            return subprocess.CompletedProcess(args, exit_code, "verification output\n", "")
        return subprocess.CompletedProcess(args, 0, "initialized\n", "")

    monkeypatch.setattr(terraform, "run", fake_run)

    result = driver_registry.VERIFICATION_DRIVERS["terraform"](_terraform_context(tmp_path, report))

    assert result == VerificationResult(expected_status)
    assert (report / "verify.tfplan").read_bytes() == b"verification plan"
    assert (report / "verify.txt").read_text() == "verification output\n"
    plan_command, plan_kwargs = next(item for item in commands if item[0][1] == "plan")
    assert "-detailed-exitcode" in plan_command
    assert "-input=false" in plan_command
    assert "-no-color" in plan_command
    assert not any(value.startswith("-refresh=") for value in plan_command)
    assert "-lock=false" not in plan_command
    assert plan_kwargs["capture"] is True
    assert plan_kwargs["check"] is False
    assert not any(command[1] == "apply" for command, _ in commands)


def test_terraform_verification_turns_other_exit_codes_into_driver_errors(tmp_path, monkeypatch):
    report = tmp_path / "report"

    def fake_run(*args, **_kwargs):
        if args[1] == "plan":
            return subprocess.CompletedProcess(args, 1, "", "Error: state lock failed\n")
        return subprocess.CompletedProcess(args, 0, "initialized\n", "")

    monkeypatch.setattr(terraform, "run", fake_run)

    with pytest.raises(DriverError, match="state lock failed"):
        terraform.PLUGIN.verify(_terraform_context(tmp_path, report))

    assert (report / "verify.txt").read_text() == "Error: state lock failed\n"


@pytest.mark.parametrize(
    ("driver", "result", "expected"),
    (
        (
            "oci-images",
            {"artifacts": {"containers.json": {}}, "controller": {"run": "ignored"}},
            {"artifacts": {"containers.json": {}}},
        ),
        (
            "vite-oci-bundle",
            {"artifacts": {"frontend.json": {}}, "plan": {"ignored": True}},
            {"artifacts": {"frontend.json": {}}},
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
    assert driver_registry.semantic_driver_result(driver, result) == expected


def test_every_reconciliation_driver_defines_result_semantics():
    assert set(driver_registry.DRIVER_PLUGINS) == set(driver_registry.RECONCILIATION_DRIVERS)
    assert driver_registry.load_driver_plugins() == driver_registry.DRIVER_PLUGINS


@pytest.mark.parametrize(
    ("driver", "result", "message"),
    (
        ("unknown", {}, "unsupported driver"),
        ("terraform", {"applied": {}}, "missing semantic fields: outputs"),
        ("oci-images", None, "driver result must be an object"),
    ),
)
def test_driver_semantics_fail_loudly_for_unknown_or_incomplete_results(driver, result, message):
    with pytest.raises(DriverError, match=message):
        driver_registry.semantic_driver_result(driver, result)
