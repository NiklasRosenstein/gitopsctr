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
    ResolvedInputs,
    StrictModel,
    schema_url,
)
from gitopsctr.document import JsonObject, JsonValue, ResolvedJsonObjectValue
from gitopsctr.driver import (
    DriverError,
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
from gitopsctr.execution import CommandOutput, CommandResult
from gitopsctr.templates import TemplateObject

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
class TerraformAuthoredConfiguration(StrictModel):
    backend: dict[str, str | int | float | bool] | None = None
    variables: TemplateObject | None = None
    observeOutputs: list[str] | None = None
    checks: list[TerraformHttpCheck] | None = None


@dataclass(frozen=True, kw_only=True)
class TerraformConfiguration(StrictModel):
    backend: dict[str, str | int | float | bool] | None = None
    variables: ResolvedJsonObjectValue | None = None
    observeOutputs: list[str] | None = None
    checks: list[TerraformHttpCheck] | None = None


@dataclass(frozen=True, kw_only=True)
class TerraformUnit(StrictModel):
    source: AuthoredSource
    terraform: TerraformAuthoredConfiguration | None = None
    inputs: TemplateObject | None = None


@dataclass(frozen=True, kw_only=True)
class TerraformDesiredUnit(StrictModel):
    source: DesiredSource
    terraform: TerraformConfiguration | None = None
    inputs: ResolvedJsonObjectValue | None = None
    resolvedInputs: ResolvedInputs | None = None


@dataclass(frozen=True, kw_only=True)
class AppliedTerraformModel(StrictModel):
    sourceRevision: str
    path: str | None = None


@dataclass(frozen=True, kw_only=True)
class TerraformResultModel(StrictModel):
    applied: AppliedTerraformModel
    outputs: dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class TerraformRuntime:
    """Validated values used to invoke Terraform for a unit.

    Checks remain unparsed until reconciliation, where they are validated against
    the observed Terraform outputs they reference.
    """

    working_directory: Path
    environment: dict[str, str]
    init_args: list[str]
    observed_output_names: list[str]
    checks: list[TerraformHttpCheck]


def terraform_runtime(
    context: (
        PlanningContext[TerraformDesiredUnit]
        | ReconciliationContext[TerraformDesiredUnit]
        | VerificationContext[TerraformDesiredUnit]
    ),
) -> TerraformRuntime:
    configuration = context.unit.terraform
    if configuration is None:
        raise DriverError("terraform driver requires a terraform configuration")
    backend = configuration.backend
    variables = configuration.variables
    output_names = configuration.observeOutputs
    checks = configuration.checks or []
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
    if output_names is None:
        raise DriverError("terraform observeOutputs must be a list of names")

    terraform_root = context.source_root / context.source_path
    terraform_environment = os.environ | {
        f"TF_VAR_{name}": value if isinstance(value, str) else json.dumps(value) for name, value in variables.items()
    }
    return TerraformRuntime(
        working_directory=terraform_root,
        environment=terraform_environment,
        init_args=backend_args,
        observed_output_names=output_names,
        checks=checks,
    )


class TerraformDriver(
    UnitDriver[TerraformUnit, TerraformDesiredUnit, TerraformDesiredUnit, TerraformResultModel],
    PlanningCapability[TerraformDesiredUnit],
    ReconciliationCapability[TerraformDesiredUnit, TerraformResultModel],
    VerificationCapability[TerraformDesiredUnit],
):
    api_version = "unit.gitopsctr.io/v1"
    kind = "Terraform"
    driver_name = "terraform"
    version = 2
    schema_base_uri = schema_url("drivers/terraform", version, "").removesuffix(".schema.json")
    unit_contract = MashumaroContract(TerraformUnit, schema_url("drivers/terraform", version, "unit"))
    resolved_unit_contract = MashumaroContract(
        TerraformDesiredUnit,
        schema_url("drivers/terraform", version, "resolved-unit"),
    )
    desired_unit_contract = MashumaroContract(
        TerraformDesiredUnit,
        schema_url("drivers/terraform", version, "desired-unit"),
    )
    result_contract = MashumaroContract(TerraformResultModel, schema_url("drivers/terraform", version, "result"))
    _select_semantic_result = staticmethod(select_result_fields("applied", "outputs"))

    def scaffold_unit_spec(self, name: str, source_path: str) -> JsonObject:
        return {
            "source": {"path": source_path},
            "terraform": {"backend": {}, "variables": {}, "observeOutputs": [], "checks": []},
        }

    def resolve_unit(self, unit: TerraformUnit, context: UnitResolutionContext) -> UnitResolution[TerraformDesiredUnit]:
        resolutions = []
        configuration: TerraformConfiguration | None = None
        if unit.terraform is not None:
            variables = None
            if unit.terraform.variables is not None:
                variable_resolution = context.resolve_template(unit.terraform.variables._serialize())
                if not isinstance(variable_resolution.value, dict):
                    raise DriverError("resolved Terraform variables must be an object")
                variables = ResolvedJsonObjectValue(variable_resolution.value)
                resolutions.append(variable_resolution)
            configuration = TerraformConfiguration(
                backend=unit.terraform.backend,
                variables=variables,
                observeOutputs=unit.terraform.observeOutputs,
                checks=unit.terraform.checks,
            )
        inputs = None
        if unit.inputs is not None:
            input_resolution = context.resolve_template(unit.inputs._serialize())
            if not isinstance(input_resolution.value, dict):
                raise DriverError("resolved Terraform inputs must be an object")
            inputs = ResolvedJsonObjectValue(input_resolution.value)
            resolutions.append(input_resolution)
        fingerprints = reference_fingerprints(*resolutions)
        return UnitResolution(
            TerraformDesiredUnit(
                source=context.source,
                terraform=configuration,
                inputs=inputs,
                resolvedInputs=fingerprints,
            ),
            fingerprints,
        )

    @staticmethod
    def _prepare_plan_artifacts(
        context: (
            PlanningContext[TerraformDesiredUnit]
            | ReconciliationContext[TerraformDesiredUnit]
            | VerificationContext[TerraformDesiredUnit]
        ),
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

    def plan(self, context: PlanningContext[TerraformDesiredUnit]) -> None:
        runtime = terraform_runtime(context)
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
                    cwd=runtime.working_directory,
                    env=runtime.environment,
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

        terraform("init", *runtime.init_args)
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

    def reconcile(
        self,
        context: ReconciliationContext[TerraformDesiredUnit],
    ) -> ReconciliationOutput[TerraformResultModel]:
        runtime = terraform_runtime(context)
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
                    cwd=runtime.working_directory,
                    env=runtime.environment,
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

        terraform("init", *runtime.init_args)
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
                    cwd=runtime.working_directory,
                    env=runtime.environment,
                    output=CommandOutput.CAPTURE,
                ).stdout
            )
            outputs = cast(
                dict[str, JsonValue],
                {name: raw_outputs[name]["value"] for name in runtime.observed_output_names},
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise DriverError(f"Terraform did not return the expected outputs: {exc}") from exc

        for check in runtime.checks:
            if check.type != "http":
                raise DriverError("terraform currently supports only HTTP checks")
            output_name = check.urlOutput
            path = check.path
            if output_name not in outputs:
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

        return ReconciliationOutput(
            result=TerraformResultModel(
                applied=AppliedTerraformModel(
                    sourceRevision=context.source_revision,
                    path=context.source_path,
                ),
                outputs=dict(outputs),
            )
        )

    def verify(self, context: VerificationContext[TerraformDesiredUnit]) -> VerificationResult:
        runtime = terraform_runtime(context)
        plan, report_text = self._prepare_plan_artifacts(
            context,
            "verify.tfplan",
            "verify.txt",
            ".verify.tfplan",
        )

        context.execution.run(
            "terraform",
            "init",
            *runtime.init_args,
            cwd=runtime.working_directory,
            env=runtime.environment,
        )
        result = context.execution.run(
            "terraform",
            "plan",
            "-detailed-exitcode",
            "-input=false",
            "-no-color",
            f"-out={plan}",
            cwd=runtime.working_directory,
            env=runtime.environment,
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
API_KIND = unit_driver_api(DRIVER)
