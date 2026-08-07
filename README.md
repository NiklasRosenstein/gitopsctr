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

- `advance-desired`: materialize the next desired-state commit.
- `reconcile`: run one unit's driver and publish its receipt.
- `converge`: reconcile a dependency closure locally.
- `promote`: promote a clean environment through its configured change gate.
- `rollback`: publish a forward desired-state commit from historical state.
- `verify`: ask supported drivers to check external state without writing receipts.

## Driver plugins

Drivers are discovered from the `gitopsctr.drivers` Python entry-point group. An entry point must load
a `gitopsctr.driver.DriverPlugin`. The drivers distributed in this package use the same public
registry and live under `gitopsctr.contrib.driver`.

## GitHub Action

The repository's root composite action wraps `reconcile`, `advance-desired`, and `promote`. It can
install the CLI from PyPI, from the checked-out action revision, or from an explicit Git repository
and revision. Caller workflows retain responsibility for credentials, deployment tools, permissions,
concurrency, and follow-up scheduling.

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
