# Operations

The CLI is the operational interface for local use and CI. This page shows
common workflows. Use `gitopsctr COMMAND --help` for all flags.

gitopsctr finds the Git repository that contains the current directory. Use
`--repository PATH` or `GITOPSCTR_REPOSITORY` for another checkout. Terminal
output uses color. Redirected output is plain. `NO_COLOR=1` disables color and
`FORCE_COLOR=1` enables it. Machine-readable output is always plain.

## Inspect and validate

`get` is the resource-oriented, read-only inspection command. An Environment is analogous to a Kubernetes namespace:
environment-scoped resources require `--environment NAME`, while `-A` or `--all-environments` queries every authored
environment.

```console
gitopsctr get environments
gitopsctr get environment dev
gitopsctr get units --environment dev
gitopsctr get units -A
gitopsctr get unit application--deploy --environment dev
gitopsctr get stacks --environment dev
gitopsctr get stack application --environment dev
gitopsctr get stacktemplates --environment dev
gitopsctr get stacktemplate application --environment dev
gitopsctr get promotions --environment staging
gitopsctr get promotion dev --environment staging
gitopsctr get receipts --environment dev
gitopsctr get receipt application--image --environment dev
gitopsctr status --environment dev
gitopsctr dependencies --environment dev --unit application
gitopsctr validate
```

Singular and plural selectors are equivalent apart from whether a name is supplied. A named lookup with `-A` returns
every matching resource and includes its Environment, which is useful when names are reused across namespaces:

```console
gitopsctr get unit application--deploy -A
```

`--environment` and `-A/--all-environments` are mutually exclusive. Project-scoped Environment queries need neither.
A collection with no matches succeeds with an empty table; a named lookup with no matches fails and identifies the
resource and Environment.

### Tables and raw resources

`-o table` is the default. Tables are operational views rather than stored API documents. In particular, the Units
table joins desired Units with their separately stored Receipts to show `OBSERVATION` (`CURRENT`, `STALE`, `MISSING`,
or `N/A`) and `RECONCILIATION` (`CLEAN`, `READY`, `WAIT`, or `MATERIALIZED`). The Receipts table reports `CURRENT`,
`STALE`, or `ORPHAN` relative to the selected desired snapshot. These relationships are derived at read time; a
Receipt is not embedded into Unit `status`.

The built-in views use these columns, with `ENVIRONMENT` added for `-A`:

| Resource | Default columns |
| --- | --- |
| Environments | `NAME`, `DESIRED`, `OBSERVED`, reconciliation counts |
| Units | `NAME`, `KIND`, `DESIRED`, `OBSERVATION`, `RECONCILIATION`, `REASON` |
| Stacks | `NAME`, `TEMPLATE`, `PARTITION`, `UNITS`, `STATE` |
| StackTemplates | `NAME`, `PARAMETERS`, `UNITS` |
| Promotions | `NAME`, `SOURCE`, pinned desired, observed, and specification revisions |
| Receipts | `NAME`, subject `KIND`, `OBSERVATION`, `ARTIFACTS` |

For Units, `DESIRED` is the short Git blob identity of that exact persisted Unit document—the same identity used by a
Receipt's freshness binding. It is intentionally per-resource rather than repeating the desired snapshot commit on
every row.

Use `-o yaml` or `-o json` for machine-readable output:

```console
gitopsctr get unit application--deploy --environment dev -o yaml
gitopsctr get receipts -A -o json
```

A single result is the exact persisted resource document. Multi-result output is a schema-versioned inspection
envelope: every item contains its Environment, plane, ref, revision, and path provenance alongside the exact document.
The envelope does not synthesize a joined API resource.

Desired resource queries accept `--desired-ref` and `--desired-revision`; Receipt queries accept `--observed-ref` and
`--observed-revision`. Explicit ref or revision overrides cannot be combined with `-A`, because each Environment may
resolve different deployment refs.

Receipt artifacts remain explicitly subordinate to their producing Receipt. `--artifact NAME` prints one typed
Artifact resource and `--artifacts` prints every artifact described by the Receipt:

```console
gitopsctr get receipt application--image --environment dev --artifact containers
gitopsctr get receipt application--image --environment dev --artifacts
```

There is no standalone Artifact selector in this release because Artifact identity is producer-qualified. `status`
remains the higher-level diagnostic view and includes authored resources that have not yet resolved into persisted
desired Units; `get units` lists persisted desired Units only.

## Apply and reconcile

`apply` is the sole desired-state constructor. It takes explicit authored or canonical desired resources, resolves
authored inputs, and atomically publishes the resulting desired snapshot:

```console
gitopsctr apply --environment dev \
  --partition application \
  --file deployment/environments/dev/stacks/application.yaml \
  --source-revision HEAD
gitopsctr reconcile --environment dev --unit application --plan
gitopsctr reconcile --environment dev --unit application
```

The optional partition identifies an authoritative apply set. Reapplying partition `application` means the supplied
roots are its complete membership: members omitted from that application begin deletion. Different partitions and
unpartitioned roots are untouched. Without `--partition`, apply updates only the named inputs; an existing root keeps
its partition, while a new root is unpartitioned. Owned resources inherit selection through `ownerReferences`.

`--dry` previews the controller-owned Git changes. A no-op application creates no commit. An Environment change gate
decides whether a changed candidate is published to the desired ref or offered for review. A reconciliation plan lets
the driver inspect its work without applying changes or publishing a receipt; normal reconciliation publishes a
receipt only after the driver succeeds.

`--source-revision` selects the committed source snapshot used to resolve paths and pins. Working-tree changes are
excluded, so commit them and select that commit when they must affect the application.

For local orchestration, converge a unit and its dependencies until clean:

```console
gitopsctr converge --environment dev --unit application --yes
```

Without `--unit` or `--partition`, `converge` targets every persisted desired Unit. `--partition application` is
selection shorthand for the Units rooted in that partition, including owned descendants. Supplying `--file` makes
converge retain those exact inputs for the invocation and alternate apply with dependency-ordered reconciliation:

```console
gitopsctr converge --environment dev \
  --partition application \
  --file deployment/environments/dev/stacks/application.yaml \
  --source-revision HEAD \
  --yes
```

Without `--file`, converge only reconciles current desired state; it cannot reconstruct authored input. If an
observation unlocks another dynamic reference, invoke apply again with the explicit input or use `converge --file`.

## Promote and verify

Promotion requires a clean permitted source environment. It combines the target specification with explicitly
selected values or artifacts from the reviewed source state:

```console
gitopsctr promote \
  --from-environment dev \
  --to-environment staging \
  --file deployment/environments/staging/stacks/application.yaml \
  --partition application \
  --specification-revision SOURCE_SHA
gitopsctr verify --environment staging
```

Promotion applies the explicit target resources passed with `--file`; `--partition` gives that target apply set the
same omission-based pruning semantics as ordinary apply. The Promotion resource pins three independently selected
revisions: the source desired revision, its matching source observed revision, and `--specification-revision`, which
contains the target Environment, project configuration, and template sources.
`--source-desired-revision` defaults to the source desired-ref head; `--specification-revision` defaults to `HEAD`.
Pass the latter explicitly when `HEAD` might have advanced beyond the source revision reviewed in the source
environment.

Within the target specification, each Stack still chooses its own template mode. `template: application` reads the
target template at `--specification-revision`; `fromGit` resolves the target-authored Git request; and
`template.source.fromPromotion` follows the source Stack's already-recorded template commit and digest before applying
the target Stack's parameters. Promoted field values and artifact imports are separate selectors, so a target-owned
Stack can use either without promoting its template.

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
