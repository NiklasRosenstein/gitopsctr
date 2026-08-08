# gitopsctr

`gitopsctr` is a local-first deployment reconciler. It materializes desired state from a source
revision, records deployment receipts in Git, promotes clean desired state between environments,
and creates forward-only rollback commits.

The command line is the complete operational interface. CI workflows and the reusable GitHub Action
invoke the same commands that an operator can run locally.

## Development

Requirements are managed with [mise](https://mise.jdx.dev/) and [uv](https://docs.astral.sh/uv/).

```console
mise install
mise run sync
mise run check
```

The project supports Python 3.12 and newer. Ruff formats and lints the code with a 120-character line
length; ty checks the package.

## CLI

Run `gitopsctr --help` for the complete command list. The CLI discovers the Git repository containing
the current directory. Use `--repository PATH` or `GITOPSCTR_REPOSITORY` to select one explicitly.

Important commands include:

- `create project|environment|unit`: scaffold schema-valid authored resources.
- `validate`: validate files, environments, or the complete authored Project.
- `advance-desired`: materialize the next desired-state commit.
- `reconcile --plan`: run a speculative unit-driver plan without applying or publishing a receipt.
- `reconcile`: apply one unit and publish its receipt.
- `converge`: reconcile a dependency closure locally.
- `promote`: promote a clean environment through its configured change gate.
- `rollback`: publish a forward desired-state commit from historical state.
- `verify`: ask supported drivers to check external state without writing receipts.

Human-readable progress output uses semantic ANSI colors on terminals and in CI logs. It stays plain when redirected
to a file or captured for automation. Set `NO_COLOR=1` to disable styling or `FORCE_COLOR=1` to enable it explicitly.
Machine-readable stdout remains uncolored.

## Demo

`mise run demo` creates an isolated local Git remote, builds and publishes a real OCI image to a local
registry, and deploys it as a Docker container through the Terraform driver. See [`demo/docker`](demo/docker/)
for prerequisites, the reconciliation flow, and cleanup instructions.

## Unit drivers

Unit drivers are discovered from the `gitopsctr.drivers` Python entry-point group. An entry point must load an instance
of `gitopsctr.driver.UnitDriver` implementing at least `MaterializationCapability` or `ReconciliationCapability`.
Verification is an independent optional capability. The built-in drivers use the public registry and live under
`gitopsctr.contrib.drivers`, one module per driver.

The `kubernetes-manifests` unit driver renders Helm or plain YAML into the desired Git tree. It supports direct apply,
materialization-only external delivery, and read-only Argo CD observation. See the
[available unit drivers](docs/drivers.md) for all built-ins and the
[Kubernetes unit driver](docs/drivers/kubernetes-manifests.md) page for delivery modes and rollback behavior.

## JSON Schemas

Built-in unit drivers publish Draft 2020-12 schemas for authored units, desired units, raw results, and composed receipts at
[`https://niklasrosenstein.github.io/gitopsctr/schemas/`](https://niklasrosenstein.github.io/gitopsctr/schemas/).
Core environment, promotion, materialization, desired-unit, and receipt-envelope schemas are published alongside them.

Use `gitopsctr schemas show DRIVER KIND` for one schema and `gitopsctr schemas export DIRECTORY` to generate the complete
catalog. Committed YAML specifications should use a pinned `# yaml-language-server: $schema=...` directive; JSON
specifications should use the same pinned URL in `$schema`. gitopsctr treats both forms as untrusted editor hints and
never fetches them.

## GitHub Action

The repository's root composite action wraps reconciliation preparation, `reconcile`, `advance-desired`, `promote`,
and `rollback`. `operation: prepare` is action-only orchestration terminology: it selects an exact desired revision
by calling `advance-desired` for a supplied source revision or `resolve-desired` otherwise. It does not add a
CLI command or persisted controller state. The action can
install the CLI from PyPI, from the checked-out action revision, or from an explicit Git repository
and revision. Caller workflows retain responsibility for credentials, deployment tools, permissions,
concurrency, and follow-up scheduling.

Prepare one exact desired revision before fan-out reconciliation jobs:

```yaml
- id: prepare
  uses: NiklasRosenstein/gitopsctr@<commit-or-ref>
  with:
    operation: prepare
    package-source: action
    environment: dev
    source-revision: ${{ github.sha }}
    require-source-ref: main
```

The outputs are `active`, `desired-revision`, `desired-changed`, and `advance-after-reconcile`. Supplying an
exact `desired-revision` makes the run fixed (`advance-after-reconcile=false`); without one, later receipts may
continue materializing desired state. A source revision superseded through `require-source-ref` returns
`active=false`.

Publish a full-tree or targeted forward rollback through the same change-gate behavior as the CLI:

```yaml
- id: rollback
  uses: NiklasRosenstein/gitopsctr@<commit-or-ref>
  with:
    operation: rollback
    package-source: action
    environment: prod
    rollback-revision: <historical-desired-sha>
    units: aws-application,frontend
    reason: Incident mitigation
```

An empty `units` input rolls back the full tree. The action exposes the standard `change-revision`,
`change-status`, `change-url`, `candidate-ref`, and `target-ref` outputs for direct publication or a gated pull
request.

Install the package bundled with the exact action revision while testing an unreleased change:

```yaml
- uses: NiklasRosenstein/gitopsctr@<commit-or-ref>
  with:
    operation: reconcile
    package-source: action
    environment: dev
    unit: application
```

Install the latest PyPI release (the default):

```yaml
- uses: NiklasRosenstein/gitopsctr@v1
  with:
    operation: advance
    environment: dev
    source-revision: ${{ github.sha }}
```

Install from a separate Git revision:

```yaml
- uses: NiklasRosenstein/gitopsctr@v1
  with:
    operation: promote
    package-source: git
    package-repository: NiklasRosenstein/gitopsctr
    package-revision: <commit-or-ref>
    from-environment: dev
    to-environment: staging
```

The caller must check out its deployment repository before invoking the action. For gated changes,
grant `contents: write` and `pull-requests: write`; for reconciliation receipts, grant
`contents: write`. Pass any cloud credentials and required external tools in the caller workflow.

## Releases

CI runs the mocked suite on Python 3.12, 3.13, and 3.14. Tags matching `v*` are accepted only when
the tag equals `v` plus the package version. After verification and an isolated package build, the
release workflow publishes through PyPI Trusted Publishing using the protected `pypi` environment.

## License

MIT
