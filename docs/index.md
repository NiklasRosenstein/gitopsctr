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
- `promote` and `rollback` publish direct changes or pull-request candidates according to `changeGate`.
- `verify` asks supported unit drivers to check external state without changing Git or external state.

## Install

```console
uv tool install gitopsctr
gitopsctr --help
```

For development, run `mise install`, `mise run sync`, and `mise run check`.

## Contracts

Every built-in unit driver publishes schemas for its authored unit, materialized desired unit, applied result, and composed
receipt. Start with the [available unit drivers](drivers.md), the [project configuration](project-configuration.md), or the
[JSON Schema catalog](schemas.md).
