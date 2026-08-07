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

## License

MIT
