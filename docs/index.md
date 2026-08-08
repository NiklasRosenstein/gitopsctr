# gitopsctr

`gitopsctr` is a local-first deployment reconciler. It materializes desired state from a source revision, records
deployment receipts in Git, promotes clean desired state between environments, and creates forward-only rollback
commits.

The command line is the complete operational interface. CI and the reusable GitHub Action invoke the same commands an
operator can run locally.

## Main operations

- `advance-desired` resolves source, observation, and promotion inputs into an immutable desired-state commit.
- `reconcile --plan` runs a unit driver's speculative plan without applying or writing a receipt.
- `reconcile` applies one desired unit and publishes its typed receipt.
- `converge` advances and reconciles a dependency closure until every unit is terminal.
- `list environments` and `list units` summarize deployment state for exploration.
- `status` shows all environments, one environment, or one unit within an environment.
- `show desired` and `show receipt` print resolved units and observation receipts in the project's format; receipt
  artifacts are opt-in with `--artifact` or `--artifacts`, and `--json`/`--yaml` override the format.
- `promote` and `rollback` publish direct changes or pull-request candidates according to `changeGate`.
- `verify` asks supported unit drivers to check external state without changing Git or external state.

Human-readable progress output uses semantic ANSI colors on terminals and in CI logs. It stays plain when redirected
to a file or captured for automation. Set `NO_COLOR=1` to disable styling or `FORCE_COLOR=1` to enable it explicitly;
machine-readable output remains uncolored.

## Install

```console
uv tool install gitopsctr
gitopsctr --help
```

For development, run `mise install`, `mise run sync`, and `mise run check`.

## Contracts

Every built-in unit kind publishes resource schemas for its authored unit,
materialized desired unit, and receipt. Start with the
[available unit drivers](drivers.md), the
[project configuration](project-configuration.md), or the
[JSON Schema catalog](schemas.md).
