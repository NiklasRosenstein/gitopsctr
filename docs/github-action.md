# GitHub Action

The repository's composite Action wraps `apply`, `converge`, `reconcile`, `promote`, and `rollback`. It also provides
`prepare` as a read-only helper that selects an existing desired revision before reconciliation jobs fan out. The
caller must check out the deployment repository, supply deployment credentials and external tools, and configure
concurrency.

## Apply explicit input

Apply one authoritative partition from checked-out files:

```yaml
- id: apply
  uses: NiklasRosenstein/gitopsctr@<commit-or-ref>
  with:
    operation: apply
    package-source: action
    environment: dev
    partition: application
    files: |
      deployment/environments/dev/stacks/application.yaml
    source-revision: ${{ github.sha }}
```

`files` is a newline-separated list of paths. The Action passes every path directly to `gitopsctr apply -f`; it never
reconstructs implicit source input. Apply and gated publication expose `change-revision`, `change-status`,
`change-url`, `candidate-ref`, and `target-ref`.

## Converge

Converge with the same explicit input for the duration of the Action step:

```yaml
- uses: NiklasRosenstein/gitopsctr@<commit-or-ref>
  with:
    operation: converge
    package-source: action
    environment: dev
    partition: application
    files: deployment/environments/dev/stacks/application.yaml
    source-revision: ${{ github.sha }}
```

Omit `files` to reconcile current desired state. Omit both `unit` and `partition` to converge every desired Unit;
`partition` selects all Units in that apply partition, while `unit` selects one explicit Unit.

## Prepare and reconcile

`prepare` resolves an existing desired-ref head or exact desired revision. It never applies authored source:

```yaml
- id: prepare
  uses: NiklasRosenstein/gitopsctr@<commit-or-ref>
  with:
    operation: prepare
    package-source: action
    environment: dev

- uses: NiklasRosenstein/gitopsctr@<commit-or-ref>
  with:
    operation: reconcile
    package-source: action
    environment: dev
    unit: application
    desired-revision: ${{ steps.prepare.outputs.desired-revision }}
```

The prepare outputs are `active` and `desired-revision`. `active=false` means the selected desired state does not
exist. Reconcile publishes a Receipt only after its driver succeeds.

## Promote

Promotion requires explicit target input in addition to its pinned source context:

```yaml
- uses: NiklasRosenstein/gitopsctr@<commit-or-ref>
  with:
    operation: promote
    from-environment: dev
    to-environment: staging
    files: deployment/environments/staging/stacks/application.yaml
    partition: application
    specification-revision: ${{ github.sha }}
```

The same change outputs describe direct publication or a gated candidate. Gated changes require `contents: write` and
`pull-requests: write`; Receipt publication requires `contents: write`.

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

An empty `units` input rolls back the full tree.

## Package source

`package-source: pypi` installs the latest release or `package-version`; `action` installs the package bundled with the
selected Action revision; `git` requires `package-repository` and `package-revision`. Use `action` when testing an
unreleased Action change so the wrapper and CLI remain aligned.

See [`action.yml`](https://github.com/NiklasRosenstein/gitopsctr/blob/main/action.yml) for the complete input and output
contract.
