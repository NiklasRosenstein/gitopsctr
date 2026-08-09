# Operations

The CLI is the operational interface used both locally and in CI. This page shows the normal workflows; use
`gitopsctr COMMAND --help` for every flag.

gitopsctr discovers the Git repository containing the current directory. Use `--repository PATH` or
`GITOPSCTR_REPOSITORY` when operating on another checkout. Human output is colored on terminals and remains plain when
redirected; `NO_COLOR=1` disables styling and `FORCE_COLOR=1` enables it explicitly. Machine-readable output is never
colored.

## Inspect and validate

Start with read-only discovery:

```console
gitopsctr list environments
gitopsctr list units --environment dev
gitopsctr status --environment dev
gitopsctr show desired --environment dev application
gitopsctr show receipt --environment dev application
gitopsctr dependencies --environment dev --unit application
gitopsctr validate
```

`show receipt --artifact NAME` prints one typed artifact and `--artifacts` prints all artifacts. Add `--json` or
`--yaml` to the `show` commands to override the Project's preferred output format.

## Resolve and reconcile

A source-tracked environment normally moves through these operations:

```console
gitopsctr advance-desired --environment dev --source-revision HEAD --dry
gitopsctr advance-desired --environment dev --source-revision HEAD
gitopsctr reconcile --environment dev --unit application --plan --source-revision HEAD
gitopsctr reconcile --environment dev --unit application
```

The dry advance previews controller-owned Git changes. A reconciliation plan lets the selected driver inspect its
prospective work without applying or publishing a receipt. Real reconciliation publishes a receipt only after the
driver succeeds.

For local orchestration, converge a unit and its dependencies until clean:

```console
gitopsctr converge --environment dev --source-revision HEAD --unit application --yes
```

Without `--unit`, `converge` targets the full environment. It advances desired state, reconciles ready units in
dependency order, and repeats until no work remains or progress is blocked.

## Promote and verify

Promotion requires a clean permitted source environment and resolves the target from the reviewed source desired
state:

```console
gitopsctr promote --from-environment dev --to-environment staging
gitopsctr verify --environment staging
```

The target Environment decides whether promotion is published directly or through a pull-request candidate. After a
gated candidate is merged, reconcile or converge the target without a source revision because its specification and
inputs are pinned by the merged promotion.

## Roll back forward

Choose an ancestral desired revision and publish it as a new forward commit:

```console
gitopsctr rollback \
  --environment prod \
  --to-desired-revision DESIRED_SHA \
  --reason "Incident mitigation"
```

Repeat `--unit` for a targeted rollback; omit it for the full desired tree. `--dry` previews the controller write, and
the Environment's change gate controls direct publication versus a reviewed candidate.

For CI orchestration, see the [GitHub Action](github-action.md).
