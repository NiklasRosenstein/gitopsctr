import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from gitopsctr.contrib.drivers import kubernetes_manifests as kubernetes
from gitopsctr.driver import (
    DriverError,
    MaterializationContext,
    PlanningContext,
    ReconciliationContext,
    VerificationContext,
    VerificationStatus,
)
from gitopsctr.execution import CommandOutput, DriverExecution, TextDriverOutput

DIGEST = "sha256:" + "1" * 64
REVISION = "a" * 40


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


def manifest(kind: str = "ConfigMap", name: str = "web", namespace: str = "web") -> str:
    value = f"apiVersion: v1\nkind: {kind}\nmetadata:\n  name: {name}\n"
    return value + (f"  namespace: {namespace}\n" if namespace else "")


def unit(
    *,
    renderer: dict | None = None,
    delivery: dict | None = None,
    inventory: list[dict[str, str]] | None = None,
) -> dict:
    return {
        "schema": 1,
        "name": "web",
        "driver": "kubernetes-manifests",
        "source": {"path": "charts/web", "revision": REVISION},
        "materialize": renderer or {"type": "plain", "paths": ["*.yaml"]},
        "delivery": delivery or {"mode": "external"},
        "materialization": {
            "path": "manifests/web",
            "digest": DIGEST,
            "mediaType": "application/vnd.gitopsctr.kubernetes-manifests.v1",
            "metadata": {
                "renderer": "plain",
                "inventory": inventory
                or [
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "namespace": "web",
                        "name": "web",
                    }
                ],
            },
        },
    }


def materialization_context(tmp_path: Path, desired_unit: dict, runner=None) -> MaterializationContext:
    output = tmp_path / "output"
    output.mkdir()
    context = MaterializationContext(
        environment="dev",
        source_root=tmp_path / "source",
        source_revision=REVISION,
        source_path="charts/web",
        unit=desired_unit,
        output_root=output,
    )
    return replace(context, execution=execution_for(runner)) if runner is not None else context


def reconciliation_context(
    tmp_path: Path,
    desired_unit: dict,
    previous_receipt: dict | None = None,
    runner=None,
) -> ReconciliationContext:
    payload = tmp_path / "desired/manifests/web"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "manifest.yaml").write_text(manifest())
    context = ReconciliationContext(
        environment="dev",
        desired_root=tmp_path / "desired",
        desired_revision=REVISION,
        source_root=tmp_path / "source",
        source_revision=REVISION,
        source_path="charts/web",
        unit=desired_unit,
        inputs={},
        previous_receipt=previous_receipt,
    )
    return replace(context, execution=execution_for(runner)) if runner is not None else context


def planning_context(tmp_path: Path, desired_unit: dict, runner=None) -> PlanningContext:
    reconcile = reconciliation_context(tmp_path, desired_unit, runner=runner)
    return PlanningContext(
        environment=reconcile.environment,
        desired_root=reconcile.desired_root,
        desired_revision=reconcile.desired_revision,
        source_root=reconcile.source_root,
        source_revision=reconcile.source_revision,
        source_path=reconcile.source_path,
        unit=reconcile.unit,
        inputs=reconcile.inputs,
        execution=reconcile.execution,
    )


def verification_context(tmp_path: Path, desired_unit: dict, runner=None) -> VerificationContext:
    reconcile = reconciliation_context(tmp_path, desired_unit, runner=runner)
    return VerificationContext(
        environment=reconcile.environment,
        desired_root=reconcile.desired_root,
        desired_revision=reconcile.desired_revision,
        source_root=reconcile.source_root,
        source_revision=reconcile.source_revision,
        source_path=reconcile.source_path,
        unit=reconcile.unit,
        inputs={},
        execution=reconcile.execution,
    )


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_plain_materialization_preserves_paths_and_records_inventory(tmp_path):
    chart = tmp_path / "source/charts/web"
    (chart / "nested").mkdir(parents=True)
    (chart / "root.yaml").write_text(manifest())
    (chart / "nested/worker.yml").write_text(manifest("Service", "worker", ""))
    desired_unit = unit(renderer={"type": "plain", "paths": ["*.yaml", "nested/*.yml"]})

    result = kubernetes.DRIVER.materialize(materialization_context(tmp_path, desired_unit))

    assert (tmp_path / "output/root.yaml").read_text() == manifest()
    assert (tmp_path / "output/nested/worker.yml").read_text() == manifest("Service", "worker", "")
    assert result.media_type == "application/vnd.gitopsctr.kubernetes-manifests.v1"
    assert result.metadata == {
        "renderer": "plain",
        "inventory": [
            {"apiVersion": "v1", "kind": "ConfigMap", "namespace": "web", "name": "web"},
            {"apiVersion": "v1", "kind": "Service", "namespace": "", "name": "worker"},
        ],
    }


def test_helm_materialization_records_installed_version_and_resolved_values(tmp_path, monkeypatch):
    (tmp_path / "source/charts/web").mkdir(parents=True)
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ("helm", "version"):
            return completed(args, stdout="v3.17.1+g123\n")
        values_path = Path(args[args.index("--values") + 1])
        assert values_path.read_text() == "image:\n  tag: observed\n"
        return completed(args, stdout=manifest())

    desired_unit = unit(
        renderer={
            "type": "helm",
            "releaseName": "web",
            "namespace": "web",
            "values": {"image": {"tag": "observed"}},
            "allowSecrets": False,
        }
    )

    result = kubernetes.DRIVER.materialize(materialization_context(tmp_path, desired_unit, run))

    assert (tmp_path / "output/manifest.yaml").read_text() == manifest()
    assert result.metadata["renderer"] == "helm"
    assert result.metadata["version"] == "v3.17.1+g123"
    assert calls[1][0][:4] == ("helm", "template", "web", str(tmp_path / "source/charts/web"))


def test_materialization_rejects_core_secrets_by_default(tmp_path):
    chart = tmp_path / "source/charts/web"
    chart.mkdir(parents=True)
    (chart / "secret.yaml").write_text(manifest("Secret", "credentials"))

    with pytest.raises(DriverError, match="refuses core Secret"):
        kubernetes.DRIVER.materialize(materialization_context(tmp_path, unit()))

    shutil.rmtree(tmp_path / "output")
    allowed = unit(renderer={"type": "plain", "paths": ["*.yaml"], "allowSecrets": True})
    result = kubernetes.DRIVER.materialize(materialization_context(tmp_path, allowed))
    assert result.metadata["inventory"][0]["kind"] == "Secret"


def test_external_delivery_without_observer_is_materialization_only():
    desired_unit = unit()

    assert kubernetes.DRIVER.reconciliation_required(desired_unit) is False
    assert kubernetes.DRIVER.verification_supported(desired_unit) is False


def test_direct_delivery_applies_waits_then_prunes_previous_inventory(tmp_path, monkeypatch):
    previous = {
        "applied": {
            "inventory": [
                {"apiVersion": "v1", "kind": "ConfigMap", "namespace": "web", "name": "web"},
                {"apiVersion": "v1", "kind": "Service", "namespace": "web", "name": "old"},
            ]
        }
    }
    desired_unit = unit(
        delivery={
            "mode": "direct",
            "kubeContext": "kind-dev",
            "prune": True,
            "wait": [
                {
                    "resource": "deployment/web",
                    "namespace": "web",
                    "condition": "Available",
                    "timeoutSeconds": 300,
                }
            ],
        }
    )
    calls = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        return completed(args)

    result = kubernetes.DRIVER.reconcile(reconciliation_context(tmp_path, desired_unit, previous, runner))

    assert result["applied"]["manifestDigest"] == DIGEST
    assert [call[0][3] for call in calls] == ["apply", "--namespace", "delete"]
    assert calls[0][0] == (
        "kubectl",
        "--context",
        "kind-dev",
        "apply",
        "--server-side",
        "--field-manager=gitopsctr-dev-web",
        "--filename",
        str(tmp_path / "desired/manifests/web"),
    )
    assert calls[1][0][-4:] == (
        "wait",
        "--for=condition=Available",
        "--timeout=300s",
        "deployment/web",
    )
    assert calls[2][0][-4:] == ("delete", "--ignore-not-found", "--filename", "-")
    assert calls[2][1]["input_text"] == ("apiVersion: v1\nkind: Service\nmetadata:\n  name: old\n  namespace: web\n")


def test_direct_plan_has_no_kubectl_effects(tmp_path, monkeypatch):
    desired_unit = unit(delivery={"mode": "direct", "kubeContext": "dev"})

    def runner(*_args, **_kwargs):
        pytest.fail("kubectl must not run")

    result = kubernetes.DRIVER.plan(planning_context(tmp_path, desired_unit, runner))

    assert result is None


@pytest.mark.parametrize(
    ("exit_code", "status"),
    [(0, VerificationStatus.CLEAN), (1, VerificationStatus.DRIFT)],
)
def test_direct_verification_uses_kubectl_diff(exit_code, status, tmp_path, monkeypatch):
    desired_unit = unit(delivery={"mode": "direct", "kubeContext": "dev"})
    calls = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        return completed(args, returncode=exit_code)

    assert kubernetes.DRIVER.verify(verification_context(tmp_path, desired_unit, runner)).status is status
    assert calls[0][0][3:6] == ("diff", "--server-side", "--field-manager=gitopsctr-dev-web")
    assert calls[0][1]["output"] is CommandOutput.CAPTURE
    assert calls[0][1]["check"] is False


def argo_unit(access: str = "api") -> dict:
    observer = {
        "type": "argocd",
        "access": access,
        "application": "web",
        "applicationNamespace": "argocd",
        "timeoutSeconds": 30,
    }
    observer["argocdContext" if access == "api" else "kubeContext"] = "production"
    return unit(delivery={"mode": "external", "observer": observer})


def application(revision=REVISION, sync="Synced", health="Healthy", *, multi_source=False):
    specification = {"sources": [{"repoURL": "one"}, {"repoURL": "two"}]} if multi_source else {"source": {}}
    return {
        "spec": specification,
        "status": {
            "sync": {"revision": revision, "status": sync},
            "health": {"status": health},
        },
    }


@pytest.mark.parametrize("access", ["api", "kubernetes"])
def test_argo_observation_supports_both_read_only_transports(access, tmp_path, monkeypatch):
    desired_unit = argo_unit(access)
    calls = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        return completed(args, stdout=json.dumps(application()))

    result = kubernetes.DRIVER.reconcile(reconciliation_context(tmp_path, desired_unit, runner=runner))

    assert result == {
        "observed": {
            "application": "web",
            "desiredRevision": REVISION,
            "syncStatus": "Synced",
            "healthStatus": "Healthy",
        }
    }
    command = calls[0][0]
    assert command[0] == ("argocd" if access == "api" else "kubectl")
    assert "sync" not in command


def test_argo_wrong_revision_times_out_without_a_receipt(tmp_path, monkeypatch):
    desired_unit = argo_unit()

    def runner(*args, **_kwargs):
        return completed(args, stdout=json.dumps(application("b" * 40)))

    monotonic = iter([0, 31])
    monkeypatch.setattr(kubernetes.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(kubernetes.time, "sleep", lambda _seconds: None)

    with pytest.raises(DriverError, match="timed out"):
        kubernetes.DRIVER.reconcile(reconciliation_context(tmp_path, desired_unit, runner=runner))


def test_argo_degraded_and_multi_source_applications_fail(tmp_path, monkeypatch):
    desired_unit = argo_unit()
    responses = iter([application(health="Degraded"), application(multi_source=True)])

    def runner(*args, **_kwargs):
        return completed(args, stdout=json.dumps(next(responses)))

    with pytest.raises(DriverError, match="health is Degraded"):
        kubernetes.DRIVER.reconcile(reconciliation_context(tmp_path, desired_unit, runner=runner))
    with pytest.raises(DriverError, match="single-source"):
        kubernetes.DRIVER.reconcile(reconciliation_context(tmp_path, desired_unit, runner=runner))


def test_argo_verification_reports_status_mismatch_as_drift(tmp_path, monkeypatch):
    desired_unit = argo_unit()

    def runner(*args, **_kwargs):
        return completed(args, stdout=json.dumps(application(sync="OutOfSync")))

    assert (
        kubernetes.DRIVER.verify(verification_context(tmp_path, desired_unit, runner)).status
        is VerificationStatus.DRIFT
    )
