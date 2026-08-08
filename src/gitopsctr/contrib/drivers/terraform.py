"""Apply and verify Terraform units."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from gitopsctr.contracts import (
    AuthoredSource,
    DesiredSource,
    MashumaroContract,
    MaterializationDocument,
    SchemaDocument,
    StrictModel,
    schema_url,
)
from gitopsctr.document import JsonValue
from gitopsctr.driver import (
    DriverError,
    PlanningCapability,
    PlanningContext,
    ReconciliationCapability,
    ReconciliationContext,
    ReconciliationResult,
    UnitDriver,
    VerificationCapability,
    VerificationContext,
    VerificationResult,
    VerificationStatus,
)
from gitopsctr.execution import CommandOutput, CommandResult

from ._common import select_result_fields


class AppliedTerraform(TypedDict):
    sourceRevision: str
    path: str


class TerraformResult(TypedDict):
    applied: AppliedTerraform
    outputs: dict[str, JsonValue]


@dataclass(frozen=True, kw_only=True)
class TerraformHttpCheck(StrictModel):
    type: Literal["http"]
    urlOutput: str
    path: str = ""


@dataclass(frozen=True, kw_only=True)
class TerraformConfiguration(StrictModel):
    backend: dict[str, str | int | float | bool] | None = None
    variables: dict[str, Any] | None = None
    observeOutputs: list[str] | None = None
    checks: list[TerraformHttpCheck] | None = None


@dataclass(frozen=True, kw_only=True)
class TerraformUnit(SchemaDocument):
    name: str
    driver: Literal["terraform"]
    source: AuthoredSource
    terraform: TerraformConfiguration | None = None
    inputs: dict[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class TerraformDesiredUnit(SchemaDocument):
    name: str
    driver: Literal["terraform"]
    source: DesiredSource
    terraform: TerraformConfiguration | None = None
    inputs: dict[str, Any] | None = None
    resolvedInputs: dict[str, dict[str, str]] | None = None
    materialization: MaterializationDocument | None = None


@dataclass(frozen=True, kw_only=True)
class AppliedTerraformModel(StrictModel):
    sourceRevision: str
    path: str | None = None


@dataclass(frozen=True, kw_only=True)
class TerraformResultModel(StrictModel):
    applied: AppliedTerraformModel
    outputs: dict[str, Any]


def terraform_runtime(
    context: PlanningContext | ReconciliationContext | VerificationContext,
) -> tuple[Path, dict[str, str], list[str], list[str], list[object]]:
    configuration = context.unit.get("terraform")
    if not isinstance(configuration, dict):
        raise DriverError("terraform driver requires a terraform configuration")
    backend = configuration.get("backend")
    variables = configuration.get("variables")
    output_names = configuration.get("observeOutputs")
    checks = configuration.get("checks", [])
    if not isinstance(backend, dict) or not isinstance(variables, dict):
        raise DriverError("terraform driver requires backend and variables objects")
    invalid_backend_fields = [
        name
        for name, value in backend.items()
        if not isinstance(name, str) or not name or not isinstance(value, (str, int, float, bool))
    ]
    if invalid_backend_fields:
        raise DriverError("terraform backend values must be strings, numbers, or booleans")
    backend_args = [
        f"-backend-config={name}={value if isinstance(value, str) else json.dumps(value)}"
        for name, value in backend.items()
    ]
    if not isinstance(output_names, list) or not all(isinstance(name, str) for name in output_names):
        raise DriverError("terraform observeOutputs must be a list of names")
    output_names = cast(list[str], output_names)
    if not isinstance(checks, list):
        raise DriverError("terraform checks must be a list")

    terraform_root = context.source_root / context.source_path
    terraform_environment = os.environ | {
        f"TF_VAR_{name}": value if isinstance(value, str) else json.dumps(value) for name, value in variables.items()
    }
    return terraform_root, terraform_environment, backend_args, output_names, cast(list[object], checks)


class TerraformDriver(UnitDriver, PlanningCapability, ReconciliationCapability, VerificationCapability):
    api_version = "unit.gitopsctr.io/v1"
    kind = "Terraform"
    driver_name = "terraform"
    version = 2
    schema_base_uri = schema_url("drivers/terraform", version, "").removesuffix(".schema.json")
    unit_contract = MashumaroContract(TerraformUnit, schema_url("drivers/terraform", version, "unit"))
    desired_unit_contract = MashumaroContract(
        TerraformDesiredUnit,
        schema_url("drivers/terraform", version, "desired-unit"),
    )
    result_contract = MashumaroContract(TerraformResultModel, schema_url("drivers/terraform", version, "result"))
    _select_semantic_result = staticmethod(select_result_fields("applied", "outputs"))

    @staticmethod
    def _prepare_plan_artifacts(
        context: PlanningContext | ReconciliationContext | VerificationContext,
        plan_name: str,
        report_name: str,
        local_plan_name: str,
    ) -> tuple[Path, Path | None]:
        if context.report is None:
            return context.source_root / local_plan_name, None
        context.report.mkdir(parents=True, exist_ok=True)
        plan = context.report / plan_name
        report = context.report / report_name
        for previous in (plan, report):
            if previous.exists():
                previous.unlink()
        return plan, report

    def plan(self, context: PlanningContext) -> None:
        terraform_root, terraform_environment, backend_args, _, _ = terraform_runtime(context)
        plan, report_text = self._prepare_plan_artifacts(
            context,
            "plan.tfplan",
            "plan.txt",
            ".reconcile.tfplan",
        )

        def terraform(*args: str, reported: bool = False) -> CommandResult:
            output = CommandOutput.STREAM if report_text is None else CommandOutput.TEE
            try:
                result = context.execution.run(
                    "terraform",
                    *args,
                    cwd=terraform_root,
                    env=terraform_environment,
                    output=output,
                )
            except subprocess.CalledProcessError as exc:
                failure = "".join(part for part in (exc.stdout, exc.stderr) if part)
                if report_text is not None:
                    report_text.write_text(failure or f"terraform {' '.join(args)} failed\n")
                raise
            if reported:
                assert report_text is not None
                report_text.write_text(result.stdout + result.stderr)
            return result

        terraform("init", *backend_args)
        terraform(
            "plan",
            f"-out={plan}",
            "-refresh=false",
            "-lock=false",
            "-input=false",
            "-no-color",
        )
        if report_text is not None:
            terraform("show", "-no-color", str(plan), reported=True)

    def reconcile(self, context: ReconciliationContext) -> TerraformResult:
        terraform_root, terraform_environment, backend_args, output_names, checks = terraform_runtime(context)
        plan, report_text = self._prepare_plan_artifacts(
            context,
            "plan.tfplan",
            "plan.txt",
            ".reconcile.tfplan",
        )

        def terraform(
            *args: str,
            reported: bool = False,
            emit: bool = True,
        ) -> CommandResult:
            output = CommandOutput.STREAM
            if report_text is not None:
                output = CommandOutput.TEE if emit else CommandOutput.CAPTURE
            try:
                result = context.execution.run(
                    "terraform",
                    *args,
                    cwd=terraform_root,
                    env=terraform_environment,
                    output=output,
                )
            except subprocess.CalledProcessError as exc:
                failure = "".join(part for part in (exc.stdout, exc.stderr) if part)
                if report_text is not None:
                    report_text.write_text(failure or f"terraform {' '.join(args)} failed\n")
                raise
            if reported:
                assert report_text is not None
                report_text.write_text(result.stdout + result.stderr)
            return result

        terraform("init", *backend_args)
        terraform("plan", f"-out={plan}", emit=report_text is None)
        if report_text is not None:
            terraform("show", "-no-color", str(plan), reported=True)
        terraform("apply", "-auto-approve", str(plan))
        try:
            raw_outputs = json.loads(
                context.execution.run(
                    "terraform",
                    "output",
                    "-json",
                    cwd=terraform_root,
                    env=terraform_environment,
                    output=CommandOutput.CAPTURE,
                ).stdout
            )
            outputs = cast(dict[str, JsonValue], {name: raw_outputs[name]["value"] for name in output_names})
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise DriverError(f"Terraform did not return the expected outputs: {exc}") from exc

        for check in checks:
            if not isinstance(check, dict) or check.get("type") != "http":
                raise DriverError("terraform currently supports only HTTP checks")
            output_name = check.get("urlOutput")
            path = check.get("path", "")
            if output_name not in outputs or not isinstance(path, str):
                raise DriverError("terraform HTTP check has invalid urlOutput or path")
            context.execution.run(
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
            "applied": {"sourceRevision": context.source_revision, "path": context.source_path},
            "outputs": outputs,
        }

    def verify(self, context: VerificationContext) -> VerificationResult:
        terraform_root, terraform_environment, backend_args, _, _ = terraform_runtime(context)
        plan, report_text = self._prepare_plan_artifacts(
            context,
            "verify.tfplan",
            "verify.txt",
            ".verify.tfplan",
        )

        context.execution.run("terraform", "init", *backend_args, cwd=terraform_root, env=terraform_environment)
        result = context.execution.run(
            "terraform",
            "plan",
            "-detailed-exitcode",
            "-input=false",
            "-no-color",
            f"-out={plan}",
            cwd=terraform_root,
            env=terraform_environment,
            output=CommandOutput.TEE,
            check=False,
        )
        output = "".join(part for part in (result.stdout, result.stderr) if part)
        if report_text is not None:
            report_text.write_text(output)

        if result.returncode == 0:
            return VerificationResult(VerificationStatus.CLEAN)
        if result.returncode == 2:
            return VerificationResult(VerificationStatus.DRIFT)
        raise DriverError(output.strip() or f"Terraform verification failed with exit code {result.returncode}")

    def semantic_result(self, result: object) -> ReconciliationResult:
        return self._select_semantic_result(result)


DRIVER = TerraformDriver()
