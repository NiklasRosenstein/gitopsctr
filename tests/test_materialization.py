from dataclasses import dataclass
from pathlib import Path

import pytest

from gitopsctr import cli
from gitopsctr.contracts import (
    AuthoredSource,
    DesiredSource,
    EmptyResultModel,
    MashumaroContract,
    MaterializationDocument,
    ResolvedInputs,
    StrictModel,
)
from gitopsctr.driver import (
    MaterializationCapability,
    MaterializationContext,
    MaterializationResult,
    UnitDriver,
    UnitResolution,
    UnitResolutionContext,
)
from gitopsctr.errors import ReferenceUnavailable
from tests.conftest import write_test_document


@dataclass(frozen=True, kw_only=True)
class RenderOnlyUnit(StrictModel):
    source: AuthoredSource


@dataclass(frozen=True, kw_only=True)
class RenderOnlyResolvedUnit(StrictModel):
    source: DesiredSource
    resolvedInputs: ResolvedInputs | None = None


@dataclass(frozen=True, kw_only=True)
class RenderOnlyDesiredUnit(RenderOnlyResolvedUnit):
    materialization: MaterializationDocument


class RenderOnlyPlugin(
    UnitDriver[RenderOnlyUnit, RenderOnlyResolvedUnit, RenderOnlyDesiredUnit, EmptyResultModel],
    MaterializationCapability[RenderOnlyResolvedUnit, RenderOnlyDesiredUnit],
):
    driver_name = "render-only"
    kind = "RenderOnly"
    version = 1
    unit_contract = MashumaroContract(RenderOnlyUnit, "urn:gitopsctr:test:render-only:authored")
    resolved_unit_contract = MashumaroContract(RenderOnlyResolvedUnit, "urn:gitopsctr:test:render-only:resolved")
    desired_unit_contract = MashumaroContract(RenderOnlyDesiredUnit, "urn:gitopsctr:test:render-only:desired")
    result_contract = MashumaroContract(EmptyResultModel, "urn:gitopsctr:test:render-only:result")

    def __init__(self) -> None:
        self.calls = 0

    def resolve_unit(
        self, unit: RenderOnlyUnit, context: UnitResolutionContext
    ) -> UnitResolution[RenderOnlyResolvedUnit]:
        return UnitResolution(RenderOnlyResolvedUnit(source=context.source))

    def materialize(self, context: MaterializationContext[RenderOnlyResolvedUnit]) -> MaterializationResult:
        self.calls += 1
        output = context.output_root / "rendered.yaml"
        output.write_text(f"environment: {context.environment}\nrevision: {context.source_revision}\n")
        return MaterializationResult(
            media_type="application/yaml",
            metadata={"renderer": "test"},
        )

    def finalize_materialization(
        self, unit: RenderOnlyResolvedUnit, descriptor: MaterializationDocument
    ) -> RenderOnlyDesiredUnit:
        return RenderOnlyDesiredUnit(
            source=unit.source,
            resolvedInputs=unit.resolvedInputs,
            materialization=descriptor,
        )

    def resolved_from_desired(self, unit: RenderOnlyDesiredUnit) -> RenderOnlyResolvedUnit:
        return RenderOnlyResolvedUnit(source=unit.source, resolvedInputs=unit.resolvedInputs)


def write_json(path: Path, value: object) -> None:
    write_test_document(path, value)


def install_render_only(monkeypatch: pytest.MonkeyPatch) -> RenderOnlyPlugin:
    plugin = RenderOnlyPlugin()
    monkeypatch.setitem(cli.UNIT_DRIVERS, "render-only", plugin)
    monkeypatch.setitem(cli.MATERIALIZATION_DRIVERS, "render-only", plugin)
    monkeypatch.setitem(cli.DRIVER_VERSIONS, "render-only", plugin.version)
    monkeypatch.setitem(cli.DRIVER_GVKS, "render-only", "unit.gitopsctr.io/v1/RenderOnly")
    monkeypatch.setitem(cli.DRIVER_NAMES_BY_GVK, "unit.gitopsctr.io/v1/RenderOnly", "render-only")
    return plugin


def source_tree(root: Path, *, policy: str | None = None, consumer: bool = False) -> None:
    environment: dict[str, object] = {"schema": 1, "name": "dev"}
    if policy is not None:
        environment["promotionPolicy"] = {"minimumEvidence": policy}
    write_json(root / "deployment/environments/dev/environment.json", environment)
    write_json(
        root / "deployment/environments/dev/units/rendered.json",
        {
            "schema": 1,
            "name": "rendered",
            "driver": "render-only",
            "source": {"path": ".", "inputs": ["input.txt"]},
        },
    )
    (root / "input.txt").write_text("input")
    if consumer:
        write_json(
            root / "deployment/environments/dev/units/consumer.json",
            {
                "schema": 1,
                "name": "consumer",
                "driver": "terraform",
                "source": {"path": "."},
                "inputs": {
                    "rendered": {
                        "fromReceipt": {"unit": "rendered", "pointer": "/outputs/value"},
                    }
                },
            },
        )


def materialize_candidate(
    tmp_path: Path,
    source: Path,
    current: Path,
    name: str,
) -> Path:
    candidate = tmp_path / name
    observed = tmp_path / f"{name}-observed"
    current.mkdir(parents=True, exist_ok=True)
    observed.mkdir()
    cli.build_desired_candidate(
        "dev",
        source,
        "a" * 40,
        current,
        observed,
        None,
        candidate,
        verbose=False,
    )
    return candidate


def test_advancement_materializes_and_reuses_an_unchanged_payload(tmp_path, monkeypatch):
    plugin = install_render_only(monkeypatch)
    source = tmp_path / "source"
    source_tree(source)

    first = materialize_candidate(tmp_path, source, tmp_path / "empty", "first")
    first_unit = cli.load_desired_unit(first / "units/rendered.json", "rendered")

    assert plugin.calls == 1
    assert (first / "materialized/rendered/rendered.yaml").read_text() == (
        "environment: dev\nrevision: " + "a" * 40 + "\n"
    )
    assert first_unit.spec.materialization == MaterializationDocument(
        path="materialized/rendered",
        digest=cli.materialization_tree_digest(first / "materialized/rendered"),
        mediaType="application/yaml",
        metadata={"renderer": "test"},
    )
    assert cli.reconciliation_statuses(["rendered"], first, tmp_path / "first-observed") == [
        ("rendered", "MATERIALIZED", "desired payload is published for external delivery")
    ]

    second = materialize_candidate(tmp_path, source, first, "second")

    assert plugin.calls == 1
    assert cli.directory_files(second / "materialized/rendered") == cli.directory_files(first / "materialized/rendered")
    assert (
        cli.load_desired_unit(second / "units/rendered.json", "rendered").spec.materialization
        == first_unit.spec.materialization
    )


def test_revision_refresh_carries_valid_materialization_when_dependency_is_stale(tmp_path, monkeypatch):
    plugin = install_render_only(monkeypatch)
    source = tmp_path / "source"
    source_tree(source)
    first = materialize_candidate(tmp_path, source, tmp_path / "empty", "first-refresh")
    first_unit = cli.load_desired_unit(first / "units/rendered.json", "rendered")
    assert first_unit.spec.source.inputHash is not None

    monkeypatch.setattr(
        cli,
        "resolved_unit_source",
        lambda *_args: cli.ResolvedUnitSourceResult(
            source=DesiredSource(
                path=".",
                inputs=["input.txt"],
                revision="b" * 40,
                inputHash=first_unit.spec.source.inputHash,
                driverVersion=plugin.version,
            ),
            disposition=cli.SourceResolutionDisposition.REVISION_REFRESHED,
            refresh_reason="retained source aaaaaaaaaaaa is unavailable; use bbbbbbbbbbbb",
        ),
    )
    monkeypatch.setattr(
        plugin,
        "resolve_unit",
        lambda _unit, _context: (_ for _ in ()).throw(ReferenceUnavailable("receipt is stale: upstream")),
    )

    candidate = tmp_path / "second-refresh"
    observed = tmp_path / "second-refresh-observed"
    observed.mkdir()
    result = cli.build_desired_candidate("dev", source, "b" * 40, first, observed, None, candidate, verbose=False)

    carried = cli.load_desired_unit(candidate / "units/rendered.json", "rendered")
    assert result.blocked == {"rendered": "receipt is stale: upstream"}
    assert carried.spec.source.revision == "b" * 40
    assert carried.spec.materialization == first_unit.spec.materialization
    assert cli.directory_files(candidate / "materialized/rendered") == cli.directory_files(
        first / "materialized/rendered"
    )
    assert plugin.calls == 1


def test_materialized_payload_tampering_fails_before_status_or_promotion(tmp_path, monkeypatch):
    install_render_only(monkeypatch)
    source = tmp_path / "source"
    source_tree(source)
    desired = materialize_candidate(tmp_path, source, tmp_path / "empty", "desired")
    (desired / "materialized/rendered/rendered.yaml").write_text("tampered: true\n")

    with pytest.raises(cli.OperationError, match="does not match its digest"):
        cli.reconciliation_statuses(["rendered"], desired, tmp_path / "desired-observed")
    with pytest.raises(cli.OperationError, match="does not match its digest"):
        cli.require_clean_source(desired, tmp_path / "desired-observed", "materialized")


def test_materialized_promotion_evidence_is_explicit_and_needs_no_observed_ref(tmp_path, monkeypatch):
    install_render_only(monkeypatch)
    source = tmp_path / "source"
    source_tree(source, policy="materialized")
    desired = materialize_candidate(tmp_path, source, tmp_path / "empty", "desired")
    observed = tmp_path / "desired-observed"

    with pytest.raises(cli.OperationError, match="not fully reconciled"):
        cli.require_clean_source(desired, observed)

    cli.require_clean_source(desired, observed, cli.minimum_promotion_evidence(source, "dev"))
    assert cli.find_clean_observed_snapshot("observed/dev", desired, ["rendered"], tmp_path / "history") is None


def test_observations_cannot_depend_on_materialization_only_units(tmp_path, monkeypatch):
    install_render_only(monkeypatch)
    source_tree(tmp_path, consumer=True)

    with pytest.raises(cli.OperationError, match="cannot observe materialization-only unit 'rendered'"):
        cli.load_environment_specifications(tmp_path, "dev")
