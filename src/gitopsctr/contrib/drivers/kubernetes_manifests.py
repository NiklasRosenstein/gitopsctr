"""Materialize and optionally deliver Kubernetes manifests."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

import yaml
from mashumaro.mixins.dict import DataClassDictMixin

from gitopsctr.contracts import (
    AuthoredSource,
    DesiredSource,
    MashumaroContract,
    MashumaroUnionContract,
    MaterializationDocument,
    ResolvedInputs,
    StrictModel,
    schema_url,
)
from gitopsctr.document import JsonObject, JsonObjectValue, JsonValue, ResolvedJsonObjectValue
from gitopsctr.driver import (
    DriverError,
    MaterializationCapability,
    MaterializationContext,
    MaterializationResult,
    PlanningCapability,
    PlanningContext,
    ReconciliationCapability,
    ReconciliationContext,
    ReconciliationOutput,
    ReconciliationResult,
    UnitDriver,
    UnitResolution,
    UnitResolutionContext,
    VerificationCapability,
    VerificationContext,
    VerificationResult,
    VerificationStatus,
    reference_fingerprints,
    unit_driver_api,
)
from gitopsctr.execution import CommandOutput, DriverExecution
from gitopsctr.templates import TemplateObject

from ._common import select_result_fields


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


type ArgoSyncStatus = Literal["Synced", "OutOfSync", "Unknown"]
type ArgoHealthStatus = Literal["Healthy", "Progressing", "Degraded", "Suspended", "Missing", "Unknown"]


@dataclass
class ArgoApplicationSpec(DataClassDictMixin):
    source: JsonObjectValue | None = None
    sources: list[JsonObjectValue] | None = None


@dataclass
class ArgoApplicationSync(DataClassDictMixin):
    revision: str
    status: str


@dataclass
class ArgoApplicationHealth(DataClassDictMixin):
    status: str


@dataclass
class ArgoApplicationStatusPayload(DataClassDictMixin):
    sync: ArgoApplicationSync
    health: ArgoApplicationHealth


@dataclass
class ArgoApplicationDocument(DataClassDictMixin):
    spec: ArgoApplicationSpec
    status: ArgoApplicationStatusPayload


@dataclass(frozen=True, kw_only=True)
class ArgoApplicationStatus:
    revision: str
    syncStatus: ArgoSyncStatus
    healthStatus: ArgoHealthStatus


@dataclass(frozen=True, kw_only=True)
class AuthoredHelmMaterialization(StrictModel):
    type: Literal["helm"]
    releaseName: str
    namespace: str
    values: TemplateObject
    allowSecrets: bool = False


@dataclass(frozen=True, kw_only=True)
class HelmMaterialization(StrictModel):
    type: Literal["helm"]
    releaseName: str
    namespace: str
    values: ResolvedJsonObjectValue
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
class KubernetesUnit(StrictModel):
    source: AuthoredSource
    materialize: AuthoredHelmMaterialization | PlainMaterialization
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
class KubernetesResolvedUnit(StrictModel):
    source: DesiredSource
    materialize: HelmMaterialization | PlainMaterialization
    delivery: DirectDelivery | ExternalDelivery
    inputs: ResolvedJsonObjectValue | None = None
    resolvedInputs: ResolvedInputs | None = None


@dataclass(frozen=True, kw_only=True)
class KubernetesDesiredUnit(KubernetesResolvedUnit):
    materialization: KubernetesMaterializationDescriptor


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


type KubernetesDriverResult = KubernetesResultModel | ArgoResultModel


def require_object(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DriverError(f"{description} must be an object")
    return cast(dict[str, object], value)


def require_string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise DriverError(f"{description} must be a non-empty string")
    return value


def _argo_observer_configuration(value: object) -> ArgoApiObserver | ArgoKubernetesObserver:
    observer = require_object(value, "kubernetes-manifests observer")
    if observer.get("type") != "argocd":
        raise DriverError("kubernetes-manifests supports only the argocd observer")
    access = observer.get("access")
    if access not in {"api", "kubernetes"}:
        raise DriverError("kubernetes-manifests argocd observer access must be 'api' or 'kubernetes'")
    common_fields = {
        "type",
        "access",
        "application",
        "applicationNamespace",
        "timeoutSeconds",
    }
    access_field = "argocdContext" if access == "api" else "kubeContext"
    if set(observer) - (common_fields | {access_field}):
        raise DriverError("kubernetes-manifests argocd observer has unsupported fields")
    application = require_string(observer.get("application"), "argocd observer application")
    application_namespace = require_string(observer.get("applicationNamespace"), "argocd observer applicationNamespace")
    timeout = observer.get("timeoutSeconds", 600)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise DriverError("argocd observer timeoutSeconds must be a positive integer")
    if access == "api":
        argocd_context = require_string(observer.get("argocdContext"), "argocd observer argocdContext")
        return ArgoApiObserver(
            type="argocd",
            access="api",
            application=application,
            applicationNamespace=application_namespace,
            argocdContext=argocd_context,
            timeoutSeconds=timeout,
        )
    kube_context = require_string(observer.get("kubeContext"), "argocd observer kubeContext")
    return ArgoKubernetesObserver(
        type="argocd",
        access="kubernetes",
        application=application,
        applicationNamespace=application_namespace,
        kubeContext=kube_context,
        timeoutSeconds=timeout,
    )


def delivery_configuration(
    unit: KubernetesUnit | KubernetesResolvedUnit | KubernetesDesiredUnit,
) -> DirectDelivery | ExternalDelivery:
    return unit.delivery


def materialization_configuration(
    unit: KubernetesUnit | KubernetesResolvedUnit | KubernetesDesiredUnit,
) -> AuthoredHelmMaterialization | HelmMaterialization | PlainMaterialization:
    return unit.materialize


@dataclass(frozen=True)
class ResolvedMaterialization:
    path: str
    digest: str
    inventory: list[ResourceIdentity]


def materialization_descriptor(unit: KubernetesDesiredUnit) -> ResolvedMaterialization:
    descriptor = unit.materialization
    return ResolvedMaterialization(
        descriptor.path,
        descriptor.digest,
        [cast(ResourceIdentity, item.to_dict()) for item in descriptor.metadata.inventory],
    )


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


def argo_observer(
    delivery: DirectDelivery | ExternalDelivery,
) -> ArgoApiObserver | ArgoKubernetesObserver | None:
    if isinstance(delivery, DirectDelivery):
        return None
    return delivery.observer


def read_argo_application(
    execution: DriverExecution,
    observer: ArgoApiObserver | ArgoKubernetesObserver,
) -> ArgoApplicationDocument:
    if isinstance(observer, ArgoApiObserver):
        result = execution.run(
            "argocd",
            "app",
            "get",
            observer.application,
            "--app-namespace",
            observer.applicationNamespace,
            "--argocd-context",
            observer.argocdContext,
            "--output",
            "json",
            output=CommandOutput.CAPTURE,
        )
    else:
        result = execution.run(
            *kubectl_prefix(observer.kubeContext),
            "--namespace",
            observer.applicationNamespace,
            "get",
            "application.argoproj.io",
            observer.application,
            "--output",
            "json",
            output=CommandOutput.CAPTURE,
        )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DriverError("Argo CD returned invalid Application JSON") from exc
    if not isinstance(document, dict):
        raise DriverError("Argo CD returned invalid Application JSON")
    try:
        return ArgoApplicationDocument.from_dict(document)
    except (KeyError, TypeError, ValueError) as exc:
        raise DriverError("Argo CD returned an invalid Application document") from exc


def argo_application_status(document: ArgoApplicationDocument) -> ArgoApplicationStatus:
    if document.spec.sources is not None or document.spec.source is None:
        raise DriverError("kubernetes-manifests requires a single-source Argo CD Application")
    sync_status = document.status.sync.status
    if sync_status not in {"Synced", "OutOfSync", "Unknown"}:
        raise DriverError(f"Argo CD returned an unknown sync status: {sync_status!r}")
    health_status = document.status.health.status
    if health_status not in {"Healthy", "Progressing", "Degraded", "Suspended", "Missing", "Unknown"}:
        raise DriverError(f"Argo CD returned an unknown health status: {health_status!r}")
    return ArgoApplicationStatus(
        revision=document.status.sync.revision,
        syncStatus=sync_status,
        healthStatus=health_status,
    )


class KubernetesManifestsDriver(
    UnitDriver[KubernetesUnit, KubernetesResolvedUnit, KubernetesDesiredUnit, KubernetesDriverResult],
    MaterializationCapability[KubernetesResolvedUnit, KubernetesDesiredUnit],
    PlanningCapability[KubernetesDesiredUnit],
    ReconciliationCapability[KubernetesDesiredUnit, KubernetesDriverResult],
    VerificationCapability[KubernetesDesiredUnit],
):
    api_version = "unit.gitopsctr.io/v1"
    kind = "KubernetesManifests"
    driver_name = "kubernetes-manifests"
    version = 1
    schema_base_uri = schema_url("drivers/kubernetes-manifests", version, "").removesuffix(".schema.json")
    unit_contract = MashumaroContract(
        KubernetesUnit,
        schema_url("drivers/kubernetes-manifests", version, "unit"),
    )
    resolved_unit_contract = MashumaroContract(
        KubernetesResolvedUnit,
        schema_url("drivers/kubernetes-manifests", version, "resolved-unit"),
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

    def authored_reconciliation_required(self, unit: KubernetesUnit) -> bool:
        delivery = unit.delivery
        return isinstance(delivery, DirectDelivery) or argo_observer(delivery) is not None

    _select_direct_result = staticmethod(select_result_fields("applied"))
    _select_argo_result = staticmethod(select_result_fields("observed"))

    def scaffold_unit_spec(self, name: str, source_path: str) -> JsonObject:
        return {
            "source": {"path": source_path},
            "materialize": {
                "type": "plain",
                "paths": ["manifests/**/*.yaml", "manifests/**/*.yml"],
                "allowSecrets": False,
            },
            "delivery": {"mode": "external"},
        }

    def resolve_unit(
        self, unit: KubernetesUnit, context: UnitResolutionContext
    ) -> UnitResolution[KubernetesResolvedUnit]:
        resolved = ()
        materialize: HelmMaterialization | PlainMaterialization
        if isinstance(unit.materialize, AuthoredHelmMaterialization):
            values = context.resolve_template(unit.materialize.values._serialize())
            if not isinstance(values.value, dict):
                raise DriverError("resolved Helm values must be an object")
            materialize = HelmMaterialization(
                type="helm",
                releaseName=unit.materialize.releaseName,
                namespace=unit.materialize.namespace,
                values=ResolvedJsonObjectValue(values.value),
                allowSecrets=unit.materialize.allowSecrets,
            )
            resolved = (values,)
        else:
            materialize = unit.materialize
        fingerprints = reference_fingerprints(*resolved)
        return UnitResolution(
            KubernetesResolvedUnit(
                source=context.source,
                materialize=materialize,
                delivery=unit.delivery,
                resolvedInputs=fingerprints,
            ),
            fingerprints,
        )

    def materialize(self, context: MaterializationContext[KubernetesResolvedUnit]) -> MaterializationResult:
        configuration = materialization_configuration(context.unit)
        delivery = delivery_configuration(context.unit)
        if isinstance(delivery, ExternalDelivery):
            argo_observer(delivery)
        renderer = configuration.type
        allow_secrets = configuration.allowSecrets
        source = context.source_root / context.source_path
        if not source.is_dir():
            raise DriverError(f"kubernetes-manifests source path is not a directory: {context.source_path}")

        metadata: JsonObject
        if renderer == "helm":
            assert isinstance(configuration, HelmMaterialization)
            release_name = configuration.releaseName
            namespace = configuration.namespace
            values = configuration.values
            version = context.execution.run("helm", "version", "--short", output=CommandOutput.CAPTURE).stdout.strip()
            if not version:
                raise DriverError("Helm did not report its version")
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as values_file:
                yaml.safe_dump(dict(values), values_file, sort_keys=True)
                values_file.flush()
                rendered = context.execution.run(
                    "helm",
                    "template",
                    release_name,
                    str(source),
                    "--namespace",
                    namespace,
                    "--include-crds",
                    "--values",
                    values_file.name,
                    output=CommandOutput.CAPTURE,
                ).stdout
            if not rendered.strip():
                raise DriverError("Helm rendered an empty manifest payload")
            (context.output_root / "manifest.yaml").write_text(rendered)
            metadata = {"renderer": "helm", "version": version}
        else:
            assert isinstance(configuration, PlainMaterialization)
            raw_patterns = configuration.paths or ["**/*.yaml", "**/*.yml"]
            copy_plain_manifests(source, context.output_root, raw_patterns)
            metadata = {"renderer": "plain"}

        inventory = manifest_inventory(context.output_root, allow_secrets)
        if not inventory:
            raise DriverError("kubernetes-manifests materialization produced no Kubernetes resources")
        metadata["inventory"] = cast(JsonValue, inventory)
        return MaterializationResult("application/vnd.gitopsctr.kubernetes-manifests.v1", metadata)

    def finalize_materialization(
        self,
        unit: KubernetesResolvedUnit,
        descriptor: MaterializationDocument,
    ) -> KubernetesDesiredUnit:
        metadata = KubernetesMaterializationMetadata.from_dict(descriptor.metadata)
        return KubernetesDesiredUnit(
            source=unit.source,
            materialize=unit.materialize,
            delivery=unit.delivery,
            inputs=unit.inputs,
            resolvedInputs=unit.resolvedInputs,
            materialization=KubernetesMaterializationDescriptor(
                path=descriptor.path,
                digest=descriptor.digest,
                mediaType=descriptor.mediaType,
                metadata=metadata,
            ),
        )

    def resolved_from_desired(self, unit: KubernetesDesiredUnit) -> KubernetesResolvedUnit:
        return KubernetesResolvedUnit(
            source=unit.source,
            materialize=unit.materialize,
            delivery=unit.delivery,
            inputs=unit.inputs,
            resolvedInputs=unit.resolvedInputs,
        )

    def reconciliation_required(self, unit: KubernetesDesiredUnit) -> bool:
        delivery = delivery_configuration(unit)
        return isinstance(delivery, DirectDelivery) or argo_observer(delivery) is not None

    def plan(self, context: PlanningContext[KubernetesDesiredUnit]) -> None:
        delivery = delivery_configuration(context.unit)
        observer = argo_observer(delivery)
        if observer is not None:
            self._observe_argo(context, observer, wait=True)
            return
        if not isinstance(delivery, DirectDelivery):
            raise DriverError("external Kubernetes delivery without an observer does not support planning")
        materialization_descriptor(context.unit)

    def reconcile(
        self,
        context: ReconciliationContext[KubernetesDesiredUnit],
    ) -> ReconciliationOutput[KubernetesDriverResult]:
        delivery = delivery_configuration(context.unit)
        observer = argo_observer(delivery)
        if observer is not None:
            return ReconciliationOutput(result=self._observe_argo(context, observer, wait=True))
        if not isinstance(delivery, DirectDelivery):
            raise DriverError("external Kubernetes delivery without an observer does not reconcile")

        materialization = materialization_descriptor(context.unit)
        digest = materialization.digest
        inventory = materialization.inventory
        context_name = delivery.kubeContext
        prune = delivery.prune
        waits = delivery.wait or []
        manager = field_manager(context.environment, context.unit_name)
        context.execution.run(
            *kubectl_prefix(context_name),
            "apply",
            "--server-side",
            f"--field-manager={manager}",
            "--filename",
            str(context.desired_root / materialization.path),
        )
        for item in waits:
            context.execution.run(
                *kubectl_prefix(context_name),
                "--namespace",
                item.namespace,
                "wait",
                f"--for=condition={item.condition}",
                f"--timeout={item.timeoutSeconds}s",
                item.resource,
            )
        if prune:
            self._prune_previous(context, context_name, inventory)
        return ReconciliationOutput(
            result=KubernetesResultModel(
                applied=AppliedManifestsModel(
                    manifestDigest=digest,
                    inventory=[ResourceIdentityModel(**item) for item in inventory],
                )
            )
        )

    @staticmethod
    def _prune_previous(
        context: ReconciliationContext[KubernetesDesiredUnit],
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
            context.execution.run(
                *kubectl_prefix(context_name),
                "delete",
                "--ignore-not-found",
                "--filename",
                "-",
                input_text=resource,
            )

    def verification_supported(self, unit: KubernetesDesiredUnit) -> bool:
        delivery = delivery_configuration(unit)
        return isinstance(delivery, DirectDelivery) or argo_observer(delivery) is not None

    def verify(self, context: VerificationContext[KubernetesDesiredUnit]) -> VerificationResult:
        delivery = delivery_configuration(context.unit)
        observer = argo_observer(delivery)
        if observer is not None:
            document = read_argo_application(context.execution, observer)
            application_status = argo_application_status(document)
            status = (
                VerificationStatus.CLEAN
                if application_status.revision == context.desired_revision
                and application_status.syncStatus == "Synced"
                and application_status.healthStatus == "Healthy"
                else VerificationStatus.DRIFT
            )
            return VerificationResult(status)
        if not isinstance(delivery, DirectDelivery):
            raise DriverError("external Kubernetes delivery without an observer cannot be verified")
        context_name = delivery.kubeContext
        materialization = materialization_descriptor(context.unit)
        result = context.execution.run(
            *kubectl_prefix(context_name),
            "diff",
            "--server-side",
            f"--field-manager={field_manager(context.environment, context.unit_name)}",
            "--filename",
            str(context.desired_root / materialization.path),
            output=CommandOutput.CAPTURE,
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
        context: (
            PlanningContext[KubernetesDesiredUnit]
            | ReconciliationContext[KubernetesDesiredUnit]
            | VerificationContext[KubernetesDesiredUnit]
        ),
        observer: ArgoApiObserver | ArgoKubernetesObserver,
        *,
        wait: bool,
    ) -> ArgoResultModel:
        deadline = time.monotonic() + observer.timeoutSeconds
        while True:
            document = read_argo_application(context.execution, observer)
            application_status = argo_application_status(document)
            ready = (
                application_status.revision == context.desired_revision
                and application_status.syncStatus == "Synced"
                and application_status.healthStatus == "Healthy"
            )
            if ready:
                return ArgoResultModel(
                    observed=ObservedApplicationModel(
                        application=observer.application,
                        desiredRevision=application_status.revision,
                        syncStatus="Synced",
                        healthStatus="Healthy",
                    )
                )
            if not wait:
                raise DriverError(
                    f"Argo CD Application is revision {application_status.revision!r}, "
                    f"{application_status.syncStatus}, {application_status.healthStatus}; "
                    f"expected {context.desired_revision}, Synced, Healthy"
                )
            if application_status.healthStatus in {"Degraded", "Missing"}:
                raise DriverError(f"Argo CD Application health is {application_status.healthStatus}")
            if time.monotonic() >= deadline:
                raise DriverError("timed out waiting for Argo CD Application to reach the desired revision")
            time.sleep(5)

    def semantic_result(self, result: object) -> ReconciliationResult:
        if isinstance(result, KubernetesResultModel):
            return self._select_direct_result(result)
        if isinstance(result, ArgoResultModel):
            return self._select_argo_result(result)
        raise DriverError("kubernetes-manifests result must contain exactly applied or observed")


DRIVER = KubernetesManifestsDriver()
API_KIND = unit_driver_api(DRIVER)
