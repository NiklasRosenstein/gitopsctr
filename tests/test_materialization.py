from pathlib import Path

import pytest

from gitopsctr import cli
from gitopsctr.document import DocumentContract, JsonObject
from gitopsctr.driver import MaterializationCapability, MaterializationContext, MaterializationResult, UnitDriver
from tests.conftest import write_test_document


class RenderOnlyContract(DocumentContract):
    def validate(self, document: object) -> JsonObject:
        assert isinstance(document, dict)
        return document

    def json_schema(self) -> JsonObject:
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}


class RenderOnlyPlugin(UnitDriver, MaterializationCapability):
    driver_name = "render-only"
    kind = "RenderOnly"
    version = 1
    unit_contract = RenderOnlyContract()
    desired_unit_contract = RenderOnlyContract()
    result_contract = RenderOnlyContract()

    def __init__(self) -> None:
        self.calls = 0

    def materialize(self, context: MaterializationContext) -> MaterializationResult:
        self.calls += 1
        output = context.output_root / "rendered.yaml"
        output.write_text(f"environment: {context.environment}\nrevision: {context.source_revision}\n")
        return MaterializationResult(
            media_type="application/yaml",
            metadata={"renderer": "test"},
        )


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
    first_unit = cli.load_unit(first / "units/rendered.json", "rendered")

    assert plugin.calls == 1
    assert (first / "materialized/rendered/rendered.yaml").read_text() == (
        "environment: dev\nrevision: " + "a" * 40 + "\n"
    )
    assert first_unit["materialization"] == {
        "path": "materialized/rendered",
        "digest": cli.materialization_tree_digest(first / "materialized/rendered"),
        "mediaType": "application/yaml",
        "metadata": {"renderer": "test"},
    }
    assert cli.reconciliation_statuses(["rendered"], first, tmp_path / "first-observed") == [
        ("rendered", "MATERIALIZED", "desired payload is published for external delivery")
    ]

    second = materialize_candidate(tmp_path, source, first, "second")

    assert plugin.calls == 1
    assert cli.directory_files(second / "materialized/rendered") == cli.directory_files(first / "materialized/rendered")
    assert cli.load_unit(second / "units/rendered.json", "rendered")["materialization"] == first_unit["materialization"]


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
