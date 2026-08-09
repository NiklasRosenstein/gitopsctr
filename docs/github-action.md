# GitHub Action

The repository's composite Action wraps `prepare`, `reconcile`, `advance`, `promote`, and `rollback`. The caller must
check out the deployment repository, supply deployment credentials and external tools, and configure concurrency.
`prepare` is Action-only orchestration: it advances from a supplied source revision or resolves an existing desired
revision; it is not an additional CLI command or persisted resource.

## Prepare and reconcile

Prepare selects one exact desired revision before reconciliation jobs fan out:

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

Important outputs are `active`, `desired-revision`, `desired-changed`, and `advance-after-reconcile`. A superseded
source revision returns `active=false`. Supplying an exact desired revision fixes the run; otherwise later receipts may
unlock another desired advance.

Reconcile one selected unit with the prepared revision:

```yaml
- uses: NiklasRosenstein/gitopsctr@<commit-or-ref>
  with:
    operation: reconcile
    package-source: action
    environment: dev
    unit: application
    desired-revision: ${{ steps.prepare.outputs.desired-revision }}
```

## Promote

```yaml
- uses: NiklasRosenstein/gitopsctr@<commit-or-ref>
  with:
    operation: promote
    from-environment: dev
    to-environment: staging
    specification-revision: ${{ github.sha }}
```

Direct and gated changes expose `change-revision`, `change-status`, `change-url`, `candidate-ref`, and `target-ref`.
Gated changes require `contents: write` and `pull-requests: write`; receipt publication requires `contents: write`.

## Roll back

```yaml
- uses: NiklasRosenstein/gitopsctr@<commit-or-ref>
  with:
    operation: rollback
    environment: prod
    rollback-revision: <historical-desired-sha>
    units: aws-application,frontend
    reason: Incident mitigation
```

An empty `units` input rolls back the full tree. The same change outputs describe direct publication or a gated
candidate.

## Package source

`package-source: pypi` installs the latest release or `package-version`; `action` installs the package bundled with the
selected Action revision; `git` requires `package-repository` and `package-revision`. Use `action` when testing an
unreleased Action change so the wrapper and CLI remain aligned.

See [`action.yml`](https://github.com/NiklasRosenstein/gitopsctr/blob/main/action.yml) for the complete input and output
contract.
