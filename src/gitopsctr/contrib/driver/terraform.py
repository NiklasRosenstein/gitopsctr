"""Apply and verify Terraform units."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TypedDict, cast

from gitopsctr.driver import (
    Driver,
    DriverContext,
    DriverError,
    DriverResult,
    JsonValue,
    VerificationCapability,
    VerificationResult,
    VerificationStatus,
)

from ._common import run, select_result_fields


class PlannedTerraform(TypedDict):
    sourceRevision: str


class TerraformPlanResult(TypedDict):
    planned: PlannedTerraform


class AppliedTerraform(TypedDict):
    sourceRevision: str
    path: str


class TerraformResult(TypedDict):
    applied: AppliedTerraform
    outputs: dict[str, JsonValue]


def terraform_runtime(
    context: DriverContext,
) -> tuple[Path, dict[str, str], str, list[str], list[object]]:
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
    output_names = cast(list[str], output_names)
    if not isinstance(checks, list):
        raise DriverError("terraform checks must be a list")

    terraform_root = context.source_root / context.source_path
    terraform_environment = os.environ | {
        f"TF_VAR_{name}": value if isinstance(value, str) else json.dumps(value) for name, value in variables.items()
    }
    return terraform_root, terraform_environment, backend_key, output_names, cast(list[object], checks)


def apply_terraform(context: DriverContext) -> TerraformPlanResult | TerraformResult:
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
            return run("terraform", *args, cwd=terraform_root, env=terraform_environment)
        try:
            result = run("terraform", *args, cwd=terraform_root, env=terraform_environment, capture=True)
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
            run("terraform", "output", "-json", cwd=terraform_root, env=terraform_environment, capture=True).stdout
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
        "applied": {"sourceRevision": context.source_revision, "path": context.source_path},
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

    run("terraform", "init", f"-backend-config=key={backend_key}", cwd=terraform_root, env=terraform_environment)
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


_SEMANTIC_RESULT = select_result_fields("applied", "outputs")


class TerraformDriver(Driver, VerificationCapability):
    version = 2

    def reconcile(self, context: DriverContext) -> TerraformPlanResult | TerraformResult:
        return apply_terraform(context)

    def semantic_result(self, result: object) -> DriverResult:
        return _SEMANTIC_RESULT(result)

    def verify(self, context: DriverContext) -> VerificationResult:
        return verify_terraform(context)


PLUGIN = TerraformDriver()
