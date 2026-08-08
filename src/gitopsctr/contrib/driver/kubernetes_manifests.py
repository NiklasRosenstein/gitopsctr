"""Materialize and optionally deliver Kubernetes manifests."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

import yaml

from gitopsctr.contracts import (
    AuthoredSource,
    DesiredSource,
    MashumaroContract,
    MashumaroUnionContract,
    SchemaDocument,
    StrictModel,
    schema_url,
)
from gitopsctr.document import JsonObject
from gitopsctr.driver import (
    DriverError,
    MaterializationCapability,
    MaterializationContext,
    MaterializationResult,
    PlanningCapability,
    PlanningContext,
    ReconciliationCapability,
    ReconciliationContext,
    ReconciliationResult,
    UnitPlugin,
    VerificationCapability,
    VerificationContext,
    VerificationResult,
    VerificationStatus,
)

from ._common import run, select_result_fields


class ResourceIdentity(TypedDict):
    apiVersion: str
    kind: str
    namespace: str
    name: str


class AppliedManifests(TypedDict):
    manifestDigest: str
    inventory: list[ResourceIdentity]


class KubernetesResult(TypedDict):
    applied: AppliedManifests


class ObservedApplication(TypedDict):
    application: str
    desiredRevision: str
    syncStatus: str
    healthStatus: str


class ArgoResult(TypedDict):
    observed: ObservedApplication


@dataclass(frozen=True, kw_only=True)
class HelmMaterialization(StrictModel):
    type: Literal["helm"]
    releaseName: str
    namespace: str
    values: dict[str, Any]
    allowSecrets: bool = False


@dataclass(frozen=True, kw_only=True)
class PlainMaterialization(StrictModel):
    type: Literal["plain"]
    paths: list[str] | None = None
    allowSecrets: bool = False


@dataclass(frozen=True, kw_only=True)
class ReadinessWait(StrictModel):
    resource: str
    namespace: str
    condition: str
    timeoutSeconds: int


@dataclass(frozen=True, kw_only=True)
class ArgoApiObserver(StrictModel):
    type: Literal["argocd"]
    access: Literal["api"]
    application: str
    applicationNamespace: str
    argocdContext: str
    timeoutSeconds: int = 600


@dataclass(frozen=True, kw_only=True)
class ArgoKubernetesObserver(StrictModel):
    type: Literal["argocd"]
    access: Literal["kubernetes"]
    application: str
    applicationNamespace: str
    kubeContext: str
    timeoutSeconds: int = 600


@dataclass(frozen=True, kw_only=True)
class DirectDelivery(StrictModel):
    mode: Literal["direct"]
    kubeContext: str
    prune: bool = False
    wait: list[ReadinessWait] | None = None


@dataclass(frozen=True, kw_only=True)
class ExternalDelivery(StrictModel):
    mode: Literal["external"]
    observer: ArgoApiObserver | ArgoKubernetesObserver | None = None


@dataclass(frozen=True, kw_only=True)
class KubernetesUnit(SchemaDocument):
    schema: Literal[1]
    name: str
    driver: Literal["kubernetes-manifests"]
    source: AuthoredSource
    materialize: HelmMaterialization | PlainMaterialization
    delivery: DirectDelivery | ExternalDelivery


@dataclass(frozen=True, kw_only=True)
class ResourceIdentityModel(StrictModel):
    apiVersion: str
    kind: str
    namespace: str
    name: str


@dataclass(frozen=True, kw_only=True)
class KubernetesMaterializationMetadata(StrictModel):
    renderer: Literal["helm", "plain"]
    inventory: list[ResourceIdentityModel]
    version: str | None = None
    releaseName: str | None = None
    namespace: str | None = None


@dataclass(frozen=True, kw_only=True)
class KubernetesMaterializationDescriptor(StrictModel):
    path: str
    digest: str
    mediaType: str
    metadata: KubernetesMaterializationMetadata


@dataclass(frozen=True, kw_only=True)
class KubernetesDesiredUnit(SchemaDocument):
    schema: Literal[1]
    name: str
    driver: Literal["kubernetes-manifests"]
    source: DesiredSource
    materialize: HelmMaterialization | PlainMaterialization
    delivery: DirectDelivery | ExternalDelivery
    materialization: KubernetesMaterializationDescriptor
    inputs: dict[str, Any] | None = None
    resolvedInputs: dict[str, dict[str, str]] | None = None


@dataclass(frozen=True, kw_only=True)
class AppliedManifestsModel(StrictModel):
    manifestDigest: str
    inventory: list[ResourceIdentityModel]


@dataclass(frozen=True, kw_only=True)
class KubernetesResultModel(StrictModel):
    applied: AppliedManifestsModel


@dataclass(frozen=True, kw_only=True)
class ObservedApplicationModel(StrictModel):
    application: str
    desiredRevision: str
    syncStatus: Literal["Synced"]
    healthStatus: Literal["Healthy"]


@dataclass(frozen=True, kw_only=True)
class ArgoResultModel(StrictModel):
    observed: ObservedApplicationModel


def require_object(value: object, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DriverError(f"{description} must be an object")
    return value


def require_string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise DriverError(f"{description} must be a non-empty string")
    return value


def delivery_configuration(unit: JsonObject) -> dict[str, Any]:
    delivery = require_object(unit.get("delivery"), "kubernetes-manifests delivery")
    mode = delivery.get("mode")
    if mode not in {"direct", "external"}:
        raise DriverError("kubernetes-manifests delivery.mode must be 'direct' or 'external'")
    if mode == "direct" and "observer" in delivery:
        raise DriverError("kubernetes-manifests direct delivery cannot configure an observer")
    if mode == "direct" and set(delivery) - {"mode", "kubeContext", "prune", "wait"}:
        raise DriverError("kubernetes-manifests direct delivery has unsupported fields")
    if mode == "direct":
        require_string(delivery.get("kubeContext"), "direct delivery kubeContext")
        if not isinstance(delivery.get("prune", False), bool):
            raise DriverError("direct delivery prune must be a boolean")
        waits = delivery.get("wait", [])
        if not isinstance(waits, list):
            raise DriverError("direct delivery wait must be a list")
        for item in waits:
            wait = require_object(item, "Kubernetes readiness wait")
            if set(wait) != {"resource", "namespace", "condition", "timeoutSeconds"}:
                raise DriverError("Kubernetes readiness wait has unsupported or missing fields")
            require_string(wait["resource"], "Kubernetes readiness wait resource")
            require_string(wait["namespace"], "Kubernetes readiness wait namespace")
            require_string(wait["condition"], "Kubernetes readiness wait condition")
            timeout = wait["timeoutSeconds"]
            if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
                raise DriverError("Kubernetes readiness wait timeoutSeconds must be a positive integer")
    if mode == "external" and set(delivery) - {"mode", "observer"}:
        raise DriverError("kubernetes-manifests external delivery has unsupported fields")
    return delivery


def materialization_configuration(unit: JsonObject) -> dict[str, Any]:
    configuration = require_object(unit.get("materialize"), "kubernetes-manifests materialize")
    renderer = configuration.get("type")
    if renderer not in {"helm", "plain"}:
        raise DriverError("kubernetes-manifests materialize.type must be 'helm' or 'plain'")
    return configuration


def materialization_descriptor(unit: JsonObject) -> tuple[str, list[ResourceIdentity]]:
    descriptor = require_object(unit.get("materialization"), "kubernetes-manifests materialization descriptor")
    digest = require_string(descriptor.get("digest"), "kubernetes-manifests materialization digest")
    metadata = require_object(descriptor.get("metadata"), "kubernetes-manifests materialization metadata")
    raw_inventory = metadata.get("inventory")
    if not isinstance(raw_inventory, list):
        raise DriverError("kubernetes-manifests materialization inventory must be a list")
    inventory: list[ResourceIdentity] = []
    for item in raw_inventory:
        if not isinstance(item, dict) or set(item) != {"apiVersion", "kind", "namespace", "name"}:
            raise DriverError("kubernetes-manifests materialization inventory is invalid")
        if not all(isinstance(item[field], str) for field in item):
            raise DriverError("kubernetes-manifests materialization inventory is invalid")
        inventory.append(cast(ResourceIdentity, item))
    return digest, inventory


def manifest_inventory(root: Path, allow_secrets: bool) -> list[ResourceIdentity]:
    inventory: list[ResourceIdentity] = []
    identities: set[tuple[str, str, str, str]] = set()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            documents = yaml.safe_load_all(path.read_text())
            for index, document in enumerate(documents, 1):
                if document is None:
                    continue
                if not isinstance(document, dict):
                    raise DriverError(f"{path.name} document {index} is not a Kubernetes object")
                api_version = require_string(document.get("apiVersion"), f"{path.name} document {index} apiVersion")
                kind = require_string(document.get("kind"), f"{path.name} document {index} kind")
                metadata = require_object(document.get("metadata"), f"{path.name} document {index} metadata")
                name = require_string(metadata.get("name"), f"{path.name} document {index} metadata.name")
                namespace_value = metadata.get("namespace", "")
                if not isinstance(namespace_value, str):
                    raise DriverError(f"{path.name} document {index} metadata.namespace must be a string")
                if api_version == "v1" and kind == "Secret" and not allow_secrets:
                    raise DriverError("kubernetes-manifests refuses core Secret resources unless allowSecrets is true")
                identity = (api_version, kind, namespace_value, name)
                if identity in identities:
                    raise DriverError(f"duplicate Kubernetes resource in materialized payload: {kind}/{name}")
                identities.add(identity)
                inventory.append(
                    {
                        "apiVersion": api_version,
                        "kind": kind,
                        "namespace": namespace_value,
                        "name": name,
                    }
                )
        except yaml.YAMLError as exc:
            raise DriverError(f"could not parse materialized Kubernetes manifest {path.name}: {exc}") from exc
    return sorted(inventory, key=lambda item: (item["apiVersion"], item["kind"], item["namespace"], item["name"]))


def copy_plain_manifests(source: Path, output: Path, patterns: list[str]) -> None:
    copied: set[str] = set()
    for pattern in patterns:
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise DriverError(f"plain manifest glob escapes its source path: {pattern!r}")
        for path in sorted(source.glob(pattern)):
            if path.is_symlink():
                raise DriverError(f"plain manifest input is a symbolic link: {path.relative_to(source)}")
            if not path.is_file():
                continue
            try:
                relative = path.relative_to(source).as_posix()
            except ValueError as exc:
                raise DriverError(f"plain manifest input escapes its source path: {path}") from exc
            if relative in copied:
                continue
            copied.add(relative)
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def field_manager(environment: str, unit_name: object) -> str:
    manager = f"gitopsctr-{environment}-{unit_name}"
    if len(manager) <= 128:
        return manager
    import hashlib

    suffix = hashlib.sha256(manager.encode()).hexdigest()[:16]
    return f"{manager[:111]}-{suffix}"


def kubectl_prefix(context_name: str) -> list[str]:
    return ["kubectl", "--context", context_name]


def argo_observer(delivery: dict[str, Any]) -> dict[str, Any] | None:
    observer = delivery.get("observer")
    if observer is None:
        return None
    observer = require_object(observer, "kubernetes-manifests observer")
    if observer.get("type") != "argocd":
        raise DriverError("kubernetes-manifests supports only the argocd observer")
    if observer.get("access") not in {"api", "kubernetes"}:
        raise DriverError("kubernetes-manifests argocd observer access must be 'api' or 'kubernetes'")
    common_fields = {
        "type",
        "access",
        "application",
        "applicationNamespace",
        "timeoutSeconds",
    }
    access_field = "argocdContext" if observer["access"] == "api" else "kubeContext"
    if set(observer) - (common_fields | {access_field}):
        raise DriverError("kubernetes-manifests argocd observer has unsupported fields")
    require_string(observer.get("application"), "argocd observer application")
    require_string(observer.get("applicationNamespace"), "argocd observer applicationNamespace")
    timeout = observer.get("timeoutSeconds", 600)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise DriverError("argocd observer timeoutSeconds must be a positive integer")
    if observer["access"] == "api":
        require_string(observer.get("argocdContext"), "argocd observer argocdContext")
    else:
        require_string(observer.get("kubeContext"), "argocd observer kubeContext")
    return observer


def read_argo_application(observer: dict[str, Any]) -> dict[str, Any]:
    application = cast(str, observer["application"])
    namespace = cast(str, observer["applicationNamespace"])
    if observer["access"] == "api":
        result = run(
            "argocd",
            "app",
            "get",
            application,
            "--app-namespace",
            namespace,
            "--argocd-context",
            cast(str, observer["argocdContext"]),
            "--output",
            "json",
            capture=True,
        )
    else:
        result = run(
            *kubectl_prefix(cast(str, observer["kubeContext"])),
            "--namespace",
            namespace,
            "get",
            "application.argoproj.io",
            application,
            "--output",
            "json",
            capture=True,
        )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DriverError("Argo CD returned invalid Application JSON") from exc
    if not isinstance(document, dict):
        raise DriverError("Argo CD returned invalid Application JSON")
    return document


def argo_application_status(document: dict[str, Any]) -> tuple[str, str, str]:
    specification = require_object(document.get("spec"), "Argo CD Application spec")
    if specification.get("sources") is not None or not isinstance(specification.get("source"), dict):
        raise DriverError("kubernetes-manifests requires a single-source Argo CD Application")
    status = require_object(document.get("status"), "Argo CD Application status")
    sync = require_object(status.get("sync"), "Argo CD Application sync status")
    health = require_object(status.get("health"), "Argo CD Application health status")
    revision = require_string(sync.get("revision"), "Argo CD Application synced revision")
    sync_status = require_string(sync.get("status"), "Argo CD Application sync status")
    health_status = require_string(health.get("status"), "Argo CD Application health status")
    return revision, sync_status, health_status


class KubernetesManifestsPlugin(
    UnitPlugin,
    MaterializationCapability,
    PlanningCapability,
    ReconciliationCapability,
    VerificationCapability,
):
    version = 1
    schema_base_uri = schema_url("drivers/kubernetes-manifests", version, "").removesuffix(".schema.json")
    unit_contract = MashumaroContract(
        KubernetesUnit,
        schema_url("drivers/kubernetes-manifests", version, "unit"),
    )
    desired_unit_contract = MashumaroContract(
        KubernetesDesiredUnit,
        schema_url("drivers/kubernetes-manifests", version, "desired-unit"),
    )
    result_contract = MashumaroUnionContract(
        (KubernetesResultModel, ArgoResultModel),
        schema_url("drivers/kubernetes-manifests", version, "result"),
        "kubernetes-manifests result v1",
    )
    _select_direct_result = staticmethod(select_result_fields("applied"))
    _select_argo_result = staticmethod(select_result_fields("observed"))

    def materialize(self, context: MaterializationContext) -> MaterializationResult:
        configuration = materialization_configuration(context.unit)
        delivery = delivery_configuration(context.unit)
        if delivery["mode"] == "external":
            argo_observer(delivery)
        renderer = cast(str, configuration["type"])
        allow_secrets = configuration.get("allowSecrets", False)
        if not isinstance(allow_secrets, bool):
            raise DriverError("kubernetes-manifests allowSecrets must be a boolean")
        source = context.source_root / context.source_path
        if not source.is_dir():
            raise DriverError(f"kubernetes-manifests source path is not a directory: {context.source_path}")

        metadata: JsonObject
        if renderer == "helm":
            allowed = {"type", "releaseName", "namespace", "values", "allowSecrets"}
            if set(configuration) - allowed:
                raise DriverError("kubernetes-manifests Helm materialization has unsupported fields")
            release_name = require_string(configuration.get("releaseName"), "Helm releaseName")
            namespace = require_string(configuration.get("namespace"), "Helm namespace")
            values = require_object(configuration.get("values", {}), "Helm values")
            version = run("helm", "version", "--short", capture=True).stdout.strip()
            if not version:
                raise DriverError("Helm did not report its version")
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as values_file:
                yaml.safe_dump(values, values_file, sort_keys=True)
                values_file.flush()
                rendered = run(
                    "helm",
                    "template",
                    release_name,
                    str(source),
                    "--namespace",
                    namespace,
                    "--include-crds",
                    "--values",
                    values_file.name,
                    capture=True,
                ).stdout
            if not rendered.strip():
                raise DriverError("Helm rendered an empty manifest payload")
            (context.output_root / "manifest.yaml").write_text(rendered)
            metadata = {"renderer": "helm", "version": version}
        else:
            allowed = {"type", "paths", "allowSecrets"}
            if set(configuration) - allowed:
                raise DriverError("kubernetes-manifests plain materialization has unsupported fields")
            raw_patterns = configuration.get("paths", ["**/*.yaml", "**/*.yml"])
            if (
                not isinstance(raw_patterns, list)
                or not raw_patterns
                or not all(isinstance(pattern, str) and pattern for pattern in raw_patterns)
            ):
                raise DriverError("kubernetes-manifests plain paths must be a non-empty list of globs")
            copy_plain_manifests(source, context.output_root, cast(list[str], raw_patterns))
            metadata = {"renderer": "plain"}

        inventory = manifest_inventory(context.output_root, allow_secrets)
        if not inventory:
            raise DriverError("kubernetes-manifests materialization produced no Kubernetes resources")
        metadata["inventory"] = cast(Any, inventory)
        return MaterializationResult("application/vnd.gitopsctr.kubernetes-manifests.v1", metadata)

    def reconciliation_required(self, unit: JsonObject) -> bool:
        delivery = delivery_configuration(unit)
        return delivery["mode"] == "direct" or argo_observer(delivery) is not None

    def plan(self, context: PlanningContext) -> None:
        delivery = delivery_configuration(context.unit)
        observer = argo_observer(delivery)
        if observer is not None:
            self._observe_argo(context, observer, wait=True)
            return
        if delivery["mode"] != "direct":
            raise DriverError("external Kubernetes delivery without an observer does not support planning")
        materialization_descriptor(context.unit)

    def reconcile(self, context: ReconciliationContext) -> KubernetesResult | ArgoResult:
        delivery = delivery_configuration(context.unit)
        observer = argo_observer(delivery)
        if observer is not None:
            return self._observe_argo(context, observer, wait=True)
        if delivery["mode"] != "direct":
            raise DriverError("external Kubernetes delivery without an observer does not reconcile")

        digest, inventory = materialization_descriptor(context.unit)
        context_name = require_string(delivery.get("kubeContext"), "direct delivery kubeContext")
        prune = cast(bool, delivery.get("prune", False))
        waits = cast(list[object], delivery.get("wait", []))
        manager = field_manager(context.environment, context.unit.get("name"))
        run(
            *kubectl_prefix(context_name),
            "apply",
            "--server-side",
            f"--field-manager={manager}",
            "--filename",
            str(context.desired_root / "manifests" / cast(str, context.unit["name"])),
        )
        for item in waits:
            wait = require_object(item, "Kubernetes readiness wait")
            resource = require_string(wait["resource"], "Kubernetes readiness wait resource")
            namespace = require_string(wait["namespace"], "Kubernetes readiness wait namespace")
            condition = require_string(wait["condition"], "Kubernetes readiness wait condition")
            timeout = wait["timeoutSeconds"]
            assert isinstance(timeout, int) and not isinstance(timeout, bool)
            run(
                *kubectl_prefix(context_name),
                "--namespace",
                namespace,
                "wait",
                f"--for=condition={condition}",
                f"--timeout={timeout}s",
                resource,
            )
        if prune:
            self._prune_previous(context, context_name, inventory)
        return {"applied": {"manifestDigest": digest, "inventory": inventory}}

    @staticmethod
    def _prune_previous(
        context: ReconciliationContext,
        context_name: str,
        inventory: list[ResourceIdentity],
    ) -> None:
        receipt = context.previous_receipt
        previous_applied = receipt.get("applied") if isinstance(receipt, dict) else None
        previous_inventory = previous_applied.get("inventory") if isinstance(previous_applied, dict) else None
        if previous_inventory is None:
            return
        if not isinstance(previous_inventory, list):
            raise DriverError("previous Kubernetes receipt has an invalid inventory")
        current = {(item["apiVersion"], item["kind"], item["namespace"], item["name"]) for item in inventory}
        for raw_item in previous_inventory:
            if not isinstance(raw_item, dict):
                raise DriverError("previous Kubernetes receipt has an invalid inventory")
            try:
                identity = tuple(raw_item[field] for field in ("apiVersion", "kind", "namespace", "name"))
            except KeyError as exc:
                raise DriverError("previous Kubernetes receipt has an invalid inventory") from exc
            if not all(isinstance(value, str) for value in identity):
                raise DriverError("previous Kubernetes receipt has an invalid inventory")
            if identity in current:
                continue
            api_version, kind, namespace, name = cast(tuple[str, str, str, str], identity)
            metadata = {"name": name}
            if namespace:
                metadata["namespace"] = namespace
            resource = yaml.safe_dump(
                {"apiVersion": api_version, "kind": kind, "metadata": metadata},
                sort_keys=True,
            )
            run(
                *kubectl_prefix(context_name),
                "delete",
                "--ignore-not-found",
                "--filename",
                "-",
                input_text=resource,
            )

    def verification_supported(self, unit: JsonObject) -> bool:
        delivery = delivery_configuration(unit)
        return delivery["mode"] == "direct" or argo_observer(delivery) is not None

    def verify(self, context: VerificationContext) -> VerificationResult:
        delivery = delivery_configuration(context.unit)
        observer = argo_observer(delivery)
        if observer is not None:
            document = read_argo_application(observer)
            revision, sync_status, health_status = argo_application_status(document)
            status = (
                VerificationStatus.CLEAN
                if revision == context.desired_revision and sync_status == "Synced" and health_status == "Healthy"
                else VerificationStatus.DRIFT
            )
            return VerificationResult(status)
        if delivery["mode"] != "direct":
            raise DriverError("external Kubernetes delivery without an observer cannot be verified")
        context_name = require_string(delivery.get("kubeContext"), "direct delivery kubeContext")
        result = run(
            *kubectl_prefix(context_name),
            "diff",
            "--server-side",
            f"--field-manager={field_manager(context.environment, context.unit.get('name'))}",
            "--filename",
            str(context.desired_root / "manifests" / cast(str, context.unit["name"])),
            capture=True,
            check=False,
        )
        if result.returncode == 0:
            return VerificationResult(VerificationStatus.CLEAN)
        if result.returncode == 1:
            return VerificationResult(VerificationStatus.DRIFT)
        output = "".join(part for part in (result.stdout, result.stderr) if part).strip()
        raise DriverError(output or f"kubectl diff failed with exit code {result.returncode}")

    @staticmethod
    def _observe_argo(
        context: PlanningContext | ReconciliationContext | VerificationContext,
        observer: dict[str, Any],
        *,
        wait: bool,
    ) -> ArgoResult:
        deadline = time.monotonic() + cast(int, observer.get("timeoutSeconds", 600))
        while True:
            document = read_argo_application(observer)
            revision, sync_status, health_status = argo_application_status(document)
            ready = revision == context.desired_revision and sync_status == "Synced" and health_status == "Healthy"
            if ready:
                return {
                    "observed": {
                        "application": cast(str, observer["application"]),
                        "desiredRevision": revision,
                        "syncStatus": sync_status,
                        "healthStatus": health_status,
                    }
                }
            if not wait:
                raise DriverError(
                    f"Argo CD Application is revision {revision!r}, {sync_status}, {health_status}; "
                    f"expected {context.desired_revision}, Synced, Healthy"
                )
            if health_status in {"Degraded", "Missing"}:
                raise DriverError(f"Argo CD Application health is {health_status}")
            if time.monotonic() >= deadline:
                raise DriverError("timed out waiting for Argo CD Application to reach the desired revision")
            time.sleep(5)

    def semantic_result(self, result: object) -> ReconciliationResult:
        if isinstance(result, Mapping) and set(result) == {"applied"}:
            return self._select_direct_result(result)
        if isinstance(result, Mapping) and set(result) == {"observed"}:
            return self._select_argo_result(result)
        raise DriverError("kubernetes-manifests result must contain exactly applied or observed")


PLUGIN = KubernetesManifestsPlugin()
