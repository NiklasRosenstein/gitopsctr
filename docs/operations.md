# Operations

The CLI is the operational interface for local use and CI. This page shows
common workflows. Use `gitopsctr COMMAND --help` for all flags.

gitopsctr finds the Git repository that contains the current directory. Use
`--repository PATH` or `GITOPSCTR_REPOSITORY` for another checkout. Terminal
output uses color. Redirected output is plain. `NO_COLOR=1` disables color and
`FORCE_COLOR=1` enables it. Machine-readable output is always plain.

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

When you select a source revision, `advance-desired`, `reconcile`, and
`converge` use its committed snapshot. If the working tree has changes,
gitopsctr warns that it will exclude them. Commit the changes and select that
commit if they must affect the operation.

Dry advance shows the controller-owned Git changes. A reconciliation plan lets
the driver inspect its work without applying changes or publishing a receipt.
Normal reconciliation publishes a receipt only after the driver succeeds.

For local orchestration, converge a unit and its dependencies until clean:

```console
gitopsctr converge --environment dev --source-revision HEAD --unit application --yes
```

Without `--unit`, `converge` targets the full environment. It advances desired
state, reconciles ready units in dependency order, and repeats until all units
are clean or progress is blocked.

## Promote and verify

Promotion requires a clean permitted source environment. It combines the target specification with explicitly
selected values or artifacts from the reviewed source state:

```console
gitopsctr promote \
  --from-environment dev \
  --to-environment staging \
  --specification-revision SOURCE_SHA
gitopsctr verify --environment staging
```

The Promotion resource pins three independently selected revisions: the source desired revision, its matching source
observed revision, and `--specification-revision`, which contains the target Environment and authored resources.
`--source-desired-revision` defaults to the source desired-ref head; `--specification-revision` defaults to `HEAD`.
Pass the latter explicitly when `HEAD` might have advanced beyond the source revision reviewed in the source
environment.

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

## CI-driven preview cleanup

Forge identity is provenance only. `gitopsctr` does not decide whether a pull
request or merge request is eligible, and normal desired-state operations do not
call GitHub or GitLab.

Trusted PR CI may create, update, delete, and finalize a preview Stack with the
normal CLI primitives. A scheduled CI job may enumerate preview refs or
resources by lineage, consult the forge for missed events, and request cleanup.
Cleanup still uses UID-/revision-fenced Stack finalization. The deployment-owned
scheduled job is responsible for lineage enumeration and forge API calls; this
repository does not provide a forge-aware recovery command or watcher.

## Verify GitHub merge policy

After configuring branch protection or a merge queue, run the read-only verifier:

```console
python tools/verify_github_policy.py \
  --repository OWNER/REPOSITORY \
  --branch main
```

It prints versioned JSON and returns non-zero if the branch is unprotected, the
GitHub API fails, or the policy is malformed. Deployment repositories that use
gated candidates may add `--required-check` to verify a specific required status
context. This repository does not require the candidate-freshness check because
its CI does not publish deployed desired state. The verifier does not inspect or
change GitHub rulesets or merge-queue settings.
