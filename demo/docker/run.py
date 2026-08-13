"""Run the isolated local Docker and Terraform demonstration."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

import yaml

from demo.utils import DemoRepository, RefHeads, docker_platform, remove_docker_images, require_commands, run
from gitopsctr import controller
from gitopsctr.state import GitStateStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).parent / "repository"
STATE_ROOT = PROJECT_ROOT / ".docker-demo-state"
WORKTREE = STATE_ROOT / "repository"
REMOTE = STATE_ROOT / "origin.git"
TERRAFORM_STATE = STATE_ROOT / "terraform.tfstate"
STACK_TERRAFORM_STATE = STATE_ROOT / "stack-terraform.tfstate"
REGISTRY_NAME = "gitopsctr-demo-registry"
APP_NAME = "gitopsctr-demo-app"
STACK_APP_NAME = "gitopsctr-demo-stack-app"
REPOSITORY = DemoRepository(TEMPLATE, STATE_ROOT, WORKTREE, REMOTE, "gitopsctr Docker demo")

PREVIEW_STATE_ROOT = PROJECT_ROOT / ".docker-preview-acceptance-state"
PREVIEW_WORKTREE = PREVIEW_STATE_ROOT / "repository"
PREVIEW_REMOTE = PREVIEW_STATE_ROOT / "origin.git"
PREVIEW_REGISTRY_NAME = "gitopsctr-preview-acceptance-registry"
PREVIEW_IMAGE_REPOSITORY = "gitopsctr-preview/app"
PREVIEW_REPOSITORY = DemoRepository(
    TEMPLATE,
    PREVIEW_STATE_ROOT,
    PREVIEW_WORKTREE,
    PREVIEW_REMOTE,
    "gitopsctr Docker preview acceptance",
)
PREVIEW_APP_NAMES = {
    "dev": "gitopsctr-preview-dev",
    "staging": "gitopsctr-preview-staging",
    "preview": "gitopsctr-preview-direct",
}
PREVIEW_PORTS = {
    "dev": 18180,
    "staging": 18181,
    "preview": 18182,
}
PREVIEW_UNIT_NAMES = {
    "dev": ("application--image", "application--deploy"),
    "staging": ("application--deploy",),
    "preview": ("preview--image", "preview--deploy"),
}
PREVIEW_TERRAFORM_STATES = {
    environment: PREVIEW_STATE_ROOT / f"terraform-{environment}.tfstate"
    for environment in ("dev", "staging", "preview")
}
PREVIEW_IMAGE_REFERENCES: set[str] = set()


def configure_template(registry: str, app_port: int) -> None:
    replacements = {
        '"__APP_PORT__"': str(app_port),
        "__APP_PORT__": str(app_port),
        "__DOCKER_PLATFORM__": docker_platform(),
        "__REGISTRY__": registry,
        "__TERRAFORM_STATE__": TERRAFORM_STATE.as_posix(),
    }
    for path in WORKTREE.rglob("*"):
        if not path.is_file() or path.suffix in {".pyc", ".pyo"}:
            continue
        content = path.read_text()
        for old, new in replacements.items():
            content = content.replace(old, new)
        path.write_text(content)


def prepare_repository(registry: str, app_port: int) -> None:
    REPOSITORY.prepare(lambda: configure_template(registry, app_port))


def configure_preview_repository(registry: str) -> None:
    for path in (
        PREVIEW_WORKTREE / "deployment/environments/dev/units/demo-image.yaml",
        PREVIEW_WORKTREE / "deployment/environments/dev/units/demo-service.yaml",
    ):
        path.unlink()
    replacements = {
        "__DOCKER_PLATFORM__": docker_platform(),
        "__REGISTRY__": registry,
        "__APP_VERSION__": "R1",
    }
    for path in PREVIEW_WORKTREE.rglob("*"):
        if not path.is_file() or path.suffix in {".pyc", ".pyo"}:
            continue
        content = path.read_text()
        for old, new in replacements.items():
            content = content.replace(old, new)
        path.write_text(content)
    write_preview_stacks(registry)


def write_preview_stacks(registry: str) -> None:
    environments = PREVIEW_WORKTREE / "deployment/environments"

    def parameters(environment: str) -> dict[str, object]:
        return {
            "container-name": PREVIEW_APP_NAMES[environment],
            "host-port": PREVIEW_PORTS[environment],
            "terraform-state": PREVIEW_TERRAFORM_STATES[environment].as_posix(),
            "image-repository": f"{registry}/gitopsctr-preview/{environment}",
        }

    dev_stack = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Stack",
        "metadata": {"name": "application"},
        "spec": {
            "template": "application",
            "parameters": parameters("dev"),
            "units": ["image", "deploy"],
        },
    }
    staging_stack = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Stack",
        "metadata": {"name": "application"},
        "spec": {
            "template": "application",
            "parameters": parameters("staging"),
            "units": ["deploy"],
            "artifactImports": [
                {
                    "unit": "image",
                    "name": "containers",
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "ContainerImages",
                    "fromPromotion": {"stack": "application"},
                }
            ],
        },
    }
    for environment, stack in (("dev", dev_stack), ("staging", staging_stack)):
        stack_path = environments / environment / "stacks/application.yaml"
        stack_path.parent.mkdir(parents=True, exist_ok=True)
        stack_path.write_text(yaml.safe_dump(stack, sort_keys=False))


def prepare_preview_repository(registry: str) -> None:
    PREVIEW_REPOSITORY.prepare(lambda: configure_preview_repository(registry))


def wait_for_registry(port: int) -> None:
    url = f"http://127.0.0.1:{port}/v2/"
    for _ in range(30):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    raise RuntimeError(f"local registry did not become ready at {url}")


def ensure_registry(port: int) -> None:
    existing = run("docker", "container", "inspect", REGISTRY_NAME, check=False, capture=True)
    if existing.returncode == 0:
        run("docker", "start", REGISTRY_NAME)
    else:
        run(
            "docker",
            "run",
            "--detach",
            "--name",
            REGISTRY_NAME,
            "--publish",
            f"127.0.0.1:{port}:5000",
            "registry:2",
        )
    wait_for_registry(port)


def ensure_named_registry(name: str, port: int) -> None:
    existing = run("docker", "container", "inspect", name, check=False, capture=True)
    if existing.returncode == 0:
        run("docker", "start", name)
    else:
        run(
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--publish",
            f"127.0.0.1:{port}:5000",
            "registry:2",
        )
    wait_for_registry(port)


def verify_application(port: int) -> str:
    url = f"http://127.0.0.1:{port}/"
    for _ in range(30):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return response.read().decode().strip()
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    raise RuntimeError(f"demo application did not become ready at {url}")


def verify_versioned_application(port: int, expected: str) -> None:
    message = verify_application(port)
    if message != expected:
        raise RuntimeError(f"application at port {port} returned {message!r}, expected {expected!r}")


def _run_controller_at(worktree: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = run(
        sys.executable,
        "-m",
        "gitopsctr",
        "--repository",
        str(worktree),
        *args,
        cwd=worktree,
        capture=True,
        check=False,
    )
    output = result.stdout + result.stderr
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    if check and result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result


def _preview_ref_heads(environment: str) -> RefHeads:
    store = GitStateStore(PREVIEW_WORKTREE)
    desired = store.fetch(f"gitopsctr/desired/{environment}").revision
    observed = store.fetch(f"gitopsctr/observed/{environment}").revision
    if desired is None or observed is None:
        raise RuntimeError(f"preview acceptance has incomplete {environment} refs")
    return RefHeads(desired, observed)


def _print_preview_ref_histories() -> None:
    """Print the final desired and observed histories for the preview acceptance."""

    store = GitStateStore(PREVIEW_WORKTREE)
    print("\nPreview acceptance ref history:")
    for category in ("desired", "observed"):
        for environment in ("dev", "staging", "preview"):
            ref = f"gitopsctr/{category}/{environment}"
            snapshot = store.fetch(ref)
            if snapshot.revision is None:
                raise RuntimeError(f"preview acceptance has no final ref {ref}")
            history = store.git(
                "log",
                "--oneline",
                "--reverse",
                f"refs/remotes/origin/{ref}",
            ).stdout.splitlines()
            print(f"{ref} ({len(history)} advancements):")
            for line in history:
                print(f"  {line}")
    lease_store = controller.load_project_config(PREVIEW_WORKTREE).effect_lease_store
    if lease_store is not None:
        lease_ref = lease_store.ref.replace("{environment}", "preview").removeprefix("refs/heads/")
        snapshot = store.fetch(lease_ref)
        if snapshot.revision is not None:
            history = store.git(
                "log",
                "--oneline",
                "--reverse",
                f"refs/remotes/origin/{lease_ref}",
            ).stdout.splitlines()
            print(f"{lease_ref} ({len(history)} advancements):")
            for line in history:
                print(f"  {line}")


def _preview_desired_tree(environment: str, label: str) -> Path:
    store = GitStateStore(PREVIEW_WORKTREE)
    revision = store.fetch(f"gitopsctr/desired/{environment}").revision
    if revision is None:
        raise RuntimeError(f"preview acceptance has no desired revision for {environment}")
    output = PREVIEW_STATE_ROOT / f"desired-{environment}-{label}"
    if output.exists():
        shutil.rmtree(output)
    store.materialize(revision, output)
    return output


def _desired_metadata(desired: Path, directory: str, name: str) -> Mapping[str, object]:
    paths = controller.document_candidates(desired / directory, name)
    if len(paths) != 1:
        raise RuntimeError(f"expected one desired {directory[:-1]} resource named {name}")
    document = controller.RESOURCE_CATALOG.load_document(paths[0])
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise RuntimeError(f"desired {directory[:-1]} resource {name} has no metadata")
    return metadata


def _deletion_generation(metadata: Mapping[str, object], resource_name: str) -> int:
    deletion = metadata.get("deletion")
    if not isinstance(deletion, dict) or not isinstance(deletion.get("generation"), int):
        raise RuntimeError(f"desired resource {resource_name} has no deletion metadata")
    return deletion["generation"]


def _owned_unit_records(desired: Path, owner_name: str, owner_uid: str) -> list[tuple[str, str, int]]:
    records: list[tuple[str, str, int]] = []
    units_directory = desired / "units"
    for path in sorted(units_directory.iterdir()):
        if not path.is_file():
            continue
        document = controller.RESOURCE_CATALOG.load_document(path)
        metadata = document.get("metadata")
        if not isinstance(metadata, dict):
            continue
        references = metadata.get("ownerReferences")
        if not isinstance(references, list):
            continue
        owned = any(
            isinstance(reference, dict)
            and reference.get("apiVersion") == "gitopsctr.io/v1"
            and reference.get("kind") == "Stack"
            and reference.get("name") == owner_name
            and reference.get("uid") == owner_uid
            for reference in references
        )
        if not owned:
            continue
        name = metadata.get("name")
        uid = metadata.get("uid")
        if not isinstance(name, str) or not isinstance(uid, str):
            raise RuntimeError(f"owned desired Unit in {path} has no name or UID")
        records.append((name, uid, _deletion_generation(metadata, name)))
    return records


def _reverse_demo_unit_order(records: list[tuple[str, str, int]]) -> list[tuple[str, str, int]]:
    return sorted(records, key=lambda record: (not record[0].endswith("--deploy"), record[0]))


def _preview_artifact_uri(environment: str, unit_name: str, label: str) -> str:
    store = GitStateStore(PREVIEW_WORKTREE)
    revision = store.fetch(f"gitopsctr/observed/{environment}").revision
    if revision is None:
        raise RuntimeError(f"preview acceptance has no observed revision for {environment}")
    output = PREVIEW_STATE_ROOT / f"observed-{environment}-{label}"
    if output.exists():
        shutil.rmtree(output)
    store.materialize(revision, output)
    paths = controller.document_candidates(output / "artifacts" / unit_name, "containers")
    if len(paths) != 1:
        raise RuntimeError(f"expected one observed containers artifact for {unit_name}")
    document = yaml.safe_load(paths[0].read_text())
    try:
        uri = document["images"]["application"]["uri"]
        digest = uri.rsplit("@", 1)[1]
    except (AttributeError, KeyError, TypeError, IndexError) as exc:
        raise RuntimeError(f"malformed observed containers artifact for {unit_name}") from exc
    if not isinstance(uri, str) or not digest.startswith("sha256:"):
        raise RuntimeError(f"observed artifact for {unit_name} has no immutable image URI")
    PREVIEW_IMAGE_REFERENCES.add(uri)
    return uri


def _assert_container_identity(container_name: str, image_uri: str) -> None:
    image = run("docker", "image", "inspect", image_uri, "--format", "{{.Id}}", capture=True).stdout.strip()
    if not image:
        raise RuntimeError(f"Docker has no image for observed artifact {image_uri}")
    repo_digests = json.loads(
        run("docker", "image", "inspect", image_uri, "--format", "{{json .RepoDigests}}", capture=True).stdout
    )
    if image_uri not in repo_digests:
        raise RuntimeError(f"Docker image does not carry the observed repo digest {image_uri}")
    inspected = json.loads(
        run("docker", "container", "inspect", container_name, "--format", "{{json .}}", capture=True).stdout
    )
    if inspected["Config"]["Image"] not in {image_uri, image}:
        raise RuntimeError(f"container {container_name} uses {inspected['Config']['Image']!r}, expected {image_uri!r}")
    if inspected["Image"] != image:
        raise RuntimeError(f"container {container_name} image ID differs from {image_uri}")


def _assert_deployed_version(environment: str, expected: str, label: str) -> str:
    unit_name = PREVIEW_UNIT_NAMES[environment][0]
    if environment == "staging":
        image_uri = _preview_desired_image_uri(environment, label)
    else:
        image_uri = _preview_artifact_uri(environment, unit_name, label)
    _assert_container_identity(PREVIEW_APP_NAMES[environment], image_uri)
    verify_versioned_application(PREVIEW_PORTS[environment], f"gitopsctr preview {expected}")
    return image_uri


def _preview_desired_image_uri(environment: str, label: str) -> str:
    desired = _preview_desired_tree(environment, label)
    unit_name = PREVIEW_UNIT_NAMES[environment][-1]
    path = controller.unit_document_path(desired, unit_name)
    document = yaml.safe_load(path.read_text())
    try:
        image_uri = document["spec"]["terraform"]["variables"]["image"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"desired {environment} deploy Unit has no resolved image") from exc
    if not isinstance(image_uri, str) or "@sha256:" not in image_uri:
        raise RuntimeError(f"desired {environment} deploy Unit does not select an immutable image")
    PREVIEW_IMAGE_REFERENCES.add(image_uri)
    return image_uri


def _preview_converge(environment: str, source_revision: str | None = None) -> None:
    args = ["converge", "--environment", environment, "--yes"]
    if source_revision is not None:
        args.extend(("--source-revision", source_revision))
    for _ in range(4):
        result = _run_controller_at(PREVIEW_WORKTREE, *args, check=False)
        if result.returncode == 0:
            return
        output = result.stdout + result.stderr
        if "convergence stalled with no ready unit" not in output:
            raise subprocess.CalledProcessError(
                result.returncode, result.args, output=result.stdout, stderr=result.stderr
            )
    raise RuntimeError(f"preview {environment} did not converge after four passes")


def _preview_source_revision() -> str:
    return run("git", "rev-parse", "HEAD", cwd=PREVIEW_WORKTREE, capture=True).stdout.strip()


def _initialize_preview_desired(source_revision: str) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        initial = Path(temporary_directory) / "desired"
        controller.project_stack_resources(PREVIEW_WORKTREE, "preview", source_revision, initial, PREVIEW_WORKTREE)
        GitStateStore(PREVIEW_WORKTREE).publish(
            "gitopsctr/desired/preview",
            initial,
            None,
            "Initialize direct preview desired state",
        )


def _advance_preview_version() -> str:
    path = PREVIEW_WORKTREE / "acceptance/app.py"
    content = path.read_text()
    if 'VERSION = "R1"' not in content:
        raise RuntimeError("preview acceptance source is not at R1")
    path.write_text(content.replace('VERSION = "R1"', 'VERSION = "R2"'))
    run("git", "add", "acceptance/app.py", cwd=PREVIEW_WORKTREE)
    run("git", "commit", "-m", "Publish preview application R2", cwd=PREVIEW_WORKTREE)
    run("git", "push", "origin", "main", cwd=PREVIEW_WORKTREE)
    return _preview_source_revision()


def _update_direct_preview(source_revision: str, request_id: str, registry: str) -> None:
    desired_revision = GitStateStore(PREVIEW_WORKTREE).fetch("gitopsctr/desired/preview").revision
    if desired_revision is None:
        raise RuntimeError("direct preview has no desired head before update")
    desired = _preview_desired_tree("preview", "before-update")
    stack_paths = controller.document_candidates(desired / "stacks", "preview")
    if len(stack_paths) != 1:
        raise RuntimeError("direct preview Stack is missing before update")
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(stack_paths[0]),
        profile="desired",
        expected_name="preview",
    )
    if stack.metadata.uid is None:
        raise RuntimeError("direct preview Stack has no UID before update")
    parameters = json.dumps(
        {
            "container-name": PREVIEW_APP_NAMES["preview"],
            "host-port": PREVIEW_PORTS["preview"],
            "terraform-state": PREVIEW_TERRAFORM_STATES["preview"].as_posix(),
            "image-repository": f"{registry}/gitopsctr-preview/preview",
        },
        separators=(",", ":"),
    )
    result = _run_controller_at(
        PREVIEW_WORKTREE,
        "update-direct-stack",
        "--environment",
        "preview",
        "--stack",
        "preview",
        "--uid",
        stack.metadata.uid,
        "--desired-revision",
        desired_revision,
        "--template",
        "application",
        "--source-revision",
        source_revision,
        "--parameters",
        parameters,
        "--request-id",
        request_id,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            "core command mismatch: expected the agreed explicit update-direct-stack interface "
            "(environment, stack, template, source-revision, parameters, request-id); "
            f"current CLI returned: {detail}"
        )


def _delete_direct_preview() -> None:
    desired = _preview_desired_tree("preview", "before-delete")
    stack_path = controller.document_candidates(desired / "stacks", "preview")
    if len(stack_path) != 1:
        raise RuntimeError("direct preview Stack is missing before deletion")
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(stack_path[0]),
        profile="desired",
        expected_name="preview",
    )
    if stack.metadata.uid is None:
        raise RuntimeError("direct preview Stack has no UID")
    _run_controller_at(
        PREVIEW_WORKTREE,
        "delete",
        "stack",
        "--in=state",
        "--environment",
        "preview",
        "--name",
        "preview",
        "--uid",
        stack.metadata.uid,
    )
    desired = _preview_desired_tree("preview", "delete-request")
    stack_metadata = _desired_metadata(desired, "stacks", "preview")
    stack_deletion_generation = _deletion_generation(stack_metadata, "preview")
    stack_uid = stack_metadata.get("uid")
    if stack_uid != stack.metadata.uid:
        raise RuntimeError("direct preview deletion changed the Stack UID")
    owned_units = _reverse_demo_unit_order(_owned_unit_records(desired, "preview", stack.metadata.uid))
    if not owned_units:
        raise RuntimeError("direct preview deletion did not retain owned Units")
    for unit_name, unit_uid, deletion_generation in owned_units:
        _run_controller_at(
            PREVIEW_WORKTREE,
            "finalize",
            "unit",
            "--environment",
            "preview",
            "--name",
            unit_name,
            "--uid",
            unit_uid,
            "--deletion-generation",
            str(deletion_generation),
        )
        desired = _preview_desired_tree("preview", f"finalize-{unit_name}")
    _run_controller_at(
        PREVIEW_WORKTREE,
        "finalize",
        "stack",
        "--environment",
        "preview",
        "--name",
        "preview",
        "--uid",
        stack.metadata.uid,
        "--deletion-generation",
        str(stack_deletion_generation),
    )
    if run("docker", "container", "inspect", PREVIEW_APP_NAMES["preview"], check=False, capture=True).returncode == 0:
        raise RuntimeError("direct preview finalization left its Docker container running")


def clean_preview_acceptance(registry: str) -> None:
    if shutil.which("docker") is not None:
        for name in (*PREVIEW_APP_NAMES.values(), PREVIEW_REGISTRY_NAME):
            existing = run("docker", "container", "inspect", name, check=False, capture=True)
            if existing.returncode == 0:
                run("docker", "container", "rm", "--force", name)
        for image in sorted(PREVIEW_IMAGE_REFERENCES):
            run("docker", "image", "rm", "--force", image, check=False)
        remove_docker_images(f"{registry}/{PREVIEW_IMAGE_REPOSITORY}:*")
    PREVIEW_IMAGE_REFERENCES.clear()
    PREVIEW_REPOSITORY.clean()
    print("Preview acceptance resources and state removed.")


def preview_acceptance(registry_port: int) -> None:
    registry = f"localhost:{registry_port}"
    clean_preview_acceptance(registry)
    try:
        require_commands(
            "docker",
            "git",
            "terraform",
            "curl",
            installation_hint="run 'mise install' and ensure Docker is installed",
        )
        prepare_preview_repository(registry)
        ensure_named_registry(PREVIEW_REGISTRY_NAME, registry_port)

        r1 = _preview_source_revision()
        _preview_converge("dev", r1)
        dev_r1 = _assert_deployed_version("dev", "R1", "r1-dev")
        _run_controller_at(
            PREVIEW_WORKTREE,
            "promote",
            "--from-environment",
            "dev",
            "--to-environment",
            "staging",
        )
        _preview_converge("staging")
        staging_r1 = _assert_deployed_version("staging", "R1", "r1-staging")
        if staging_r1 != dev_r1:
            raise RuntimeError("staging did not import the exact dev R1 image artifact")

        _initialize_preview_desired(r1)
        preview_parameters = json.dumps(
            {
                "container-name": PREVIEW_APP_NAMES["preview"],
                "host-port": PREVIEW_PORTS["preview"],
                "terraform-state": PREVIEW_TERRAFORM_STATES["preview"].as_posix(),
                "image-repository": f"{registry}/gitopsctr-preview/preview",
            },
            separators=(",", ":"),
        )
        _run_controller_at(
            PREVIEW_WORKTREE,
            "instantiate-stack",
            "--environment",
            "preview",
            "--stack",
            "preview",
            "--template",
            "application",
            "--source-revision",
            r1,
            "--parameters",
            preview_parameters,
            "--request-id",
            "demo:gitopsctr-preview#1",
        )
        _preview_converge("preview", r1)
        _update_direct_preview(r1, "demo:gitopsctr-preview#resolve-r1", registry)
        _preview_converge("preview", r1)
        preview_r1 = _assert_deployed_version("preview", "R1", "r1-preview")
        if preview_r1 == dev_r1:
            print("Preview R1 produced the same content digest as dev R1; producer lineage remains separate.")

        staging_heads = _preview_ref_heads("staging")
        preview_heads = _preview_ref_heads("preview")
        r2 = _advance_preview_version()
        _preview_converge("dev", r2)
        dev_r2 = _assert_deployed_version("dev", "R2", "r2-dev")
        if dev_r2 == dev_r1:
            raise RuntimeError("R2 did not publish a new image digest")

        _preview_converge("staging")
        if _preview_ref_heads("staging") != staging_heads:
            raise RuntimeError("staging advanced during the dev-only R2 change")
        if _assert_deployed_version("staging", "R1", "stable-staging") != staging_r1:
            raise RuntimeError("staging changed during the dev-only R2 change")

        if _preview_ref_heads("preview") != preview_heads:
            raise RuntimeError("direct preview advanced before update-direct-stack")
        if _assert_deployed_version("preview", "R1", "stable-preview") != preview_r1:
            raise RuntimeError("direct preview changed during the dev-only R2 change")

        _update_direct_preview(r2, "demo:gitopsctr-preview#update-r2", registry)
        _preview_converge("preview", r2)
        _update_direct_preview(r2, "demo:gitopsctr-preview#resolve-r2", registry)
        _preview_converge("preview", r2)
        preview_r2 = _assert_deployed_version("preview", "R2", "r2-preview")
        if preview_r2 == preview_r1:
            raise RuntimeError("direct preview R2 did not produce a new image digest")

        _delete_direct_preview()
        _print_preview_ref_histories()
        print("Preview acceptance passed: R1 promotion, R2 advancement, direct update, and cleanup verified.")
    finally:
        clean_preview_acceptance(registry)


def remove_demo_images(registry: str) -> None:
    remove_docker_images("demo-image:*", f"{registry}/gitopsctr-demo/app*")


def clean(registry: str) -> None:
    if shutil.which("docker") is not None:
        for name in (APP_NAME, STACK_APP_NAME, REGISTRY_NAME):
            existing = run("docker", "container", "inspect", name, check=False, capture=True)
            if existing.returncode == 0:
                run("docker", "container", "rm", "--force", name)
        remove_demo_images(registry)
    REPOSITORY.clean()
    print("Demo resources and state removed.")


def deployment_heads() -> RefHeads:
    return REPOSITORY.heads()


def converge(
    registry_port: int,
    app_port: int,
    *,
    expect_clean: bool = False,
    verify_ports: tuple[int, ...] = (),
) -> None:
    registry = f"localhost:{registry_port}"
    require_commands(
        "docker",
        "git",
        "terraform",
        "curl",
        installation_hint="run 'mise install' and ensure Docker is installed",
    )
    prepare_repository(registry, app_port)
    ensure_registry(registry_port)
    result = run(
        sys.executable,
        "-m",
        "gitopsctr",
        "--repository",
        str(WORKTREE),
        "converge",
        "--environment",
        "dev",
        "--source-revision",
        "HEAD",
        "--yes",
        cwd=WORKTREE,
        capture=expect_clean,
    )
    if expect_clean:
        output = result.stdout + result.stderr
        print(output, end="" if output.endswith("\n") else "\n")
        if "no drivers ran; 0 ref movements" not in output:
            raise RuntimeError("second convergence was not clean")
    message = verify_application(app_port)
    print(f"\nDemo is running at http://127.0.0.1:{app_port}/")
    print(f"Response: {message}")
    for port in verify_ports:
        stack_message = verify_application(port)
        print(f"Stack demo is running at http://127.0.0.1:{port}/")
        print(f"Stack response: {stack_message}")
    print("Run 'mise run demo' again to observe a clean convergence.")
    print("Run 'mise run demo-clean' to remove all demo effects.")


def _commit_source(message: str) -> str:
    run("git", "add", "deployment/stack-templates", "deployment/environments/dev/stacks", cwd=WORKTREE)
    run("git", "commit", "-m", message, cwd=WORKTREE)
    run("git", "push", "origin", "main", cwd=WORKTREE)
    return run("git", "rev-parse", "HEAD", cwd=WORKTREE, capture=True).stdout.strip()


def add_stack_source(stack_port: int) -> None:
    environment = WORKTREE / "deployment/environments/dev"
    (WORKTREE / "deployment/stack-templates").mkdir(parents=True, exist_ok=True)
    (environment / "stacks").mkdir(parents=True, exist_ok=True)
    template = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "StackTemplate",
        "metadata": {"name": "preview"},
        "spec": {
            "parameters": [
                {"name": "container-name", "type": "string"},
                {"name": "host-port", "type": "integer"},
                {"name": "terraform-state", "type": "string"},
                {"name": "image", "type": "object"},
            ],
            "resources": [
                {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "name": "demo-service",
                    "spec": {
                        "source": {"path": "infrastructure", "inputs": ["*.tf"]},
                        "terraform": {
                            "backend": {"path": {"fromParameter": {"name": "terraform-state"}}},
                            "variables": {
                                "container_name": {"fromParameter": {"name": "container-name"}},
                                "host_port": {"fromParameter": {"name": "host-port"}},
                                "image": {"fromParameter": {"name": "image"}},
                            },
                            "observeOutputs": ["container_id", "url"],
                            "checks": [{"type": "http", "urlOutput": "url", "path": "/"}],
                        },
                    },
                }
            ],
        },
    }
    stack = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Stack",
        "metadata": {"name": "preview"},
        "spec": {
            "template": "preview",
            "parameters": {
                "container-name": STACK_APP_NAME,
                "host-port": stack_port,
                "terraform-state": STACK_TERRAFORM_STATE.as_posix(),
                "image": {
                    "fromArtifact": {
                        "unit": "demo-image",
                        "name": "containers",
                        "apiVersion": "artifact.gitopsctr.io/v1",
                        "kind": "ContainerImages",
                        "pointer": "/images/application/uri",
                    }
                },
            },
        },
    }
    (WORKTREE / "deployment/stack-templates/preview.yaml").write_text(yaml.safe_dump(template, sort_keys=False))
    (environment / "stacks/preview.yaml").write_text(yaml.safe_dump(stack, sort_keys=False))
    _commit_source("Add Docker Stack preview")


def remove_stack_source() -> str:
    environment = WORKTREE / "deployment/environments/dev"
    for path in (environment / "stacks").glob("preview.*"):
        path.unlink()
    for path in (WORKTREE / "deployment/stack-templates").glob("preview.*"):
        path.unlink()
    return _commit_source("Remove Docker Stack preview")


def _run_controller(*args: str) -> None:
    result = run(
        sys.executable,
        "-m",
        "gitopsctr",
        "--repository",
        str(WORKTREE),
        *args,
        cwd=WORKTREE,
        capture=True,
    )
    output = result.stdout + result.stderr
    if output:
        print(output, end="" if output.endswith("\n") else "\n")


def _desired_tree() -> Path:
    store = GitStateStore(WORKTREE)
    revision = store.fetch("gitopsctr/desired/dev").revision
    if revision is None:
        raise RuntimeError("desired state was not published")
    output = STATE_ROOT / "desired-inspection"
    if output.exists():
        shutil.rmtree(output)
    store.materialize(revision, output)
    return output


def stack_acceptance(registry_port: int, app_port: int) -> RefHeads:
    stack_port = app_port + 1
    add_stack_source(stack_port)
    try:
        converge(registry_port, app_port, verify_ports=(stack_port,))
        first_heads = deployment_heads()
        converge(registry_port, app_port, expect_clean=True, verify_ports=(stack_port,))
        if deployment_heads() != first_heads:
            raise RuntimeError("clean Stack convergence moved desired or observed refs")

        remove_stack_source()
        _run_controller("advance-desired", "--environment", "dev", "--source-revision", "HEAD")
        desired = _desired_tree()
        stack_metadata = _desired_metadata(desired, "stacks", "preview")
        stack_uid = stack_metadata.get("uid")
        if not isinstance(stack_uid, str):
            raise RuntimeError("Stack removal did not retain the Stack UID")
        stack_deletion_generation = _deletion_generation(stack_metadata, "preview")
        owned_units = _reverse_demo_unit_order(_owned_unit_records(desired, "preview", stack_uid))
        if not owned_units:
            raise RuntimeError("Stack removal did not retain its owned Units")
        for unit_name, unit_uid, deletion_generation in owned_units:
            _run_controller(
                "finalize",
                "unit",
                "--environment",
                "dev",
                "--name",
                unit_name,
                "--uid",
                unit_uid,
                "--deletion-generation",
                str(deletion_generation),
            )
            desired = _desired_tree()
        _run_controller(
            "finalize",
            "stack",
            "--environment",
            "dev",
            "--name",
            "preview",
            "--uid",
            stack_uid,
            "--deletion-generation",
            str(stack_deletion_generation),
        )
        absent = run("docker", "container", "inspect", STACK_APP_NAME, check=False, capture=True)
        if absent.returncode == 0:
            raise RuntimeError("Stack finalization left its Docker container running")
        if verify_application(app_port) == "":
            raise RuntimeError("direct demo application became unavailable during Stack cleanup")
        final_heads = deployment_heads()
        print("Acceptance passed: Stack-driven Docker/Terraform cleanup removed the Stack application.")
        return final_heads
    finally:
        clean(f"localhost:{registry_port}")


def acceptance(registry_port: int, app_port: int) -> None:
    registry = f"localhost:{registry_port}"
    clean(registry)
    try:
        converge(registry_port, app_port)
        first_heads = deployment_heads()
        converge(registry_port, app_port, expect_clean=True)
        second_heads = deployment_heads()
        if second_heads != first_heads:
            raise RuntimeError("clean convergence moved desired or observed refs")
        final_heads = stack_acceptance(registry_port, app_port)
        print(
            "Acceptance passed: "
            f"gitopsctr/desired/dev={final_heads.desired[:12]} "
            f"gitopsctr/observed/dev={final_heads.observed[:12]}"
        )
    finally:
        clean(registry)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=("run", "reset", "clean", "acceptance", "preview-acceptance"),
        nargs="?",
        default="run",
    )
    args = parser.parse_args()
    registry_port = int(os.environ.get("GITOPSCTR_DEMO_REGISTRY_PORT", "5000"))
    app_port = int(os.environ.get("GITOPSCTR_DEMO_APP_PORT", "18080"))
    preview_registry_port = int(os.environ.get("GITOPSCTR_PREVIEW_REGISTRY_PORT", "5003"))
    registry = f"localhost:{registry_port}"
    try:
        if args.operation == "clean":
            clean(registry)
        elif args.operation == "acceptance":
            acceptance(registry_port, app_port)
        elif args.operation == "preview-acceptance":
            preview_acceptance(preview_registry_port)
        elif args.operation == "reset":
            clean(registry)
            converge(registry_port, app_port)
        else:
            converge(registry_port, app_port)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"demo failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
