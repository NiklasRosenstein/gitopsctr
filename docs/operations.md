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
gitopsctr get all --environment dev
gitopsctr get all -A
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

`get all` is the namespace overview, analogous to `kubectl get all`. It queries every registry-defined,
environment-scoped inspection family and prints one table section per family that has results. This includes Units,
Stacks, StackTemplates, Promotions, and Receipts; project-scoped Environments and Receipt-owned Artifacts keep their
dedicated selectors. With `-o yaml` or `-o json`, the aggregate is always one provenance-bearing `ResourceList`, even
when it contains zero or one resource.

`--environment` and `-A/--all-environments` are mutually exclusive. Project-scoped Environment queries need neither.
A collection with no matches succeeds with an empty table; a named lookup with no matches fails and identifies the
resource and Environment.

### Tables and raw resources

`-o table` is the default. Tables are operational views rather than stored API documents. `-o wide` adds identity
fences, complete digests, acquisition lineage, and projection details where a resource family defines an expanded
view. The Units table joins desired Units with their separately stored Receipts to show `OBSERVATION` (`CURRENT`,
`STALE`, `MISSING`, or `N/A`) and `RECONCILIATION` (`CLEAN`, `READY`, `WAIT`, or `MATERIALIZED`). The Receipts table
reports `CURRENT`, `STALE`, or `ORPHAN` relative to the selected desired snapshot. These relationships are derived at
read time; a Receipt is not embedded into Unit `status`.

The built-in views use these columns, with `ENVIRONMENT` added for `-A`:

| Resource | Default columns |
| --- | --- |
| Environments | `NAME`, `DESIRED`, `OBSERVED`, reconciliation counts |
| Units | `NAME`, `KIND`, `DESIRED`, `OBSERVATION`, `RECONCILIATION`, `REASON` |
| Stacks | `NAME`, `TEMPLATE`, short `TEMPLATE-DIGEST`, `PARTITION`, active/structural `UNITS`, `OBSERVATION`, `STATE` |
| StackTemplates | `NAME`, short `CONTENT-DIGEST`, short `SOURCE`, `PARAMETERS`, `UNITS`, `PARTITION`, `REFERENCES`, `STATE` |
| Promotions | `NAME`, `SOURCE`, pinned desired, observed, and specification revisions |
| Receipts | `NAME`, subject `KIND`, `OBSERVATION`, `ARTIFACTS` |

The default Stack view keeps `TEMPLATE` and a short `TEMPLATE-DIGEST` for quick identification; `UNITS` is the
active/structural projection count. In `-o wide`, Stack `TEMPLATE`, `TEMPLATE-UID`, and `TEMPLATE-DIGEST` are the
name/UID/content fences for its selected desired StackTemplate. `STRUCTURAL` shows the intended projection identity,
context, generated Unit kinds, and topology;
`ACTIVE` shows the concrete activated projection and its source projection. `TOPOLOGY` shows each logical Unit and its
dependencies. `OBSERVATION` is derived from UID-fenced child Units and their separate receipts, so it reports child
observation states rather than embedded Stack status. `STATE` reports the Stack's deletion state.

The default StackTemplate view shortens its content digest and source revision. In `-o wide`, `ACQUISITION` reports
`input`, `git`, or `promotion` distinctly. It includes the document digest and,
for Git and promotion, the requested selector plus the resolved exact revision/lineage. Repository values are shown
without credentials. `SOURCE` reports retained repository and exact revision context, and `REFERENCES` lists Stacks
whose name/UID/content-digest binding selects that template. `PARTITION` follows the resource's authoritative apply
membership; `PARAMETERS` and `UNITS` are counts. These relationship and child observation facts are evaluated against
the selected desired and observed snapshots at read time.

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
  --file deployment/stack-templates/application.yaml \
  --file deployment/environments/dev/stacks/application.yaml \
  --source-revision HEAD
gitopsctr reconcile --environment dev --unit application --plan
gitopsctr reconcile --environment dev --unit application
```

The optional partition identifies an authoritative apply set. Reapplying partition `application` means the supplied
roots are its complete membership: members omitted from that application begin deletion. Different partitions and
unpartitioned roots are untouched. Without `--partition`, apply updates only the named inputs; an existing root keeps
its partition, while a new root is unpartitioned. Owned resources inherit selection through `ownerReferences`.

Deletion is a two-phase lifecycle. `delete` and partition omission publish UID-/digest-fenced deletion intent only;
they do not run external teardown. Reconcile a deleting Unit or run `converge` to let the controller process deleting
resources child/dependent-first, perform idempotent teardown, record observed evidence, and publish the cleanup
commit automatically. A resource whose driver cannot prove teardown remains in the desired tree and is shown as
`RECONCILIATION: WAIT` rather than being removed.

Persisted desired state supplies teardown inputs and Stack projection context. The controller's live Project and
Environment configuration remains the trust anchor that identifies the accepted desired ref; desired or candidate
documents never authorize their own cleanup effects.

`--dry` previews the controller-owned Git changes. A no-op application creates no commit. An Environment change gate
decides whether a changed candidate is published to the desired ref or offered for review. A reconciliation plan lets
the driver inspect its work without applying changes or publishing a receipt; normal reconciliation publishes a
receipt only after the driver succeeds.

Without `--source-revision`, apply reads its explicit documents and source-less configuration from the current
worktree. It does not silently substitute `HEAD` or create a hidden commit. If an inline StackTemplate or authored
Unit uses repository-backed `spec.source`, apply fails and asks for `--source-revision <commit>`; the selected
revision is persisted as the desired template's `sourceContext`.

`--source-revision` selects the exact committed snapshot used for repository-backed paths and pins. In that mode,
working-tree changes are excluded and apply reports that exclusion. Every `-f/--file` spelling is first resolved
relative to the caller's current working directory; revision-backed apply then maps that path into the selected
repository snapshot and rejects stdin or paths outside the repository. Without a source revision, live filesystem
input is read as spelled and no hidden `HEAD` commit is created. Commit the intended content and select that commit
explicitly when reproducibility is required.

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
  --file deployment/stack-templates/application.yaml \
  --file deployment/environments/dev/stacks/application.yaml \
  --source-revision HEAD \
  --yes
```

Without `--file`, converge reconciles current desired state and can re-project persisted StackTemplate/Stack
inputs when new observation evidence unlocks a dynamic reference. No re-application of the original source files is
required; provide `--file` only when intentionally changing authored input. It also progresses deleting resources until
the environment is clean, waiting, or failed. A change-gated candidate is not live desired state: it cannot start
reconciliation, teardown, or cleanup until it is approved and reaches the environment's desired ref.

## Promote and verify

Promotion requires a clean permitted source environment. It combines the target specification with explicitly
selected values or artifacts from the reviewed source state:

```console
gitopsctr promote \
  --from-environment dev \
  --to-environment staging \
  --file deployment/stack-templates/application.yaml \
  --file deployment/environments/staging/stacks/application.yaml \
  --partition application \
  --specification-revision SOURCE_SHA
gitopsctr verify --environment staging
```

Promotion applies the explicit target resources passed with `--file`; `--partition` gives that target apply set the
same omission-based pruning semantics as ordinary apply. A Stack-only promotion may reuse a retained target
StackTemplate only when no authoritative partition selects it. When an authoritative partition selects that template,
the target StackTemplate must be supplied explicitly with its `inline`, `fromGit`, or `fromPromotion` mode. The
Promotion resource pins three independently selected revisions:
the source desired revision, its matching source observed
revision, and `--specification-revision`, which authenticates the target Project/Environment configuration and the
exact bytes of the explicit target input files.
`--source-desired-revision` defaults to the source desired-ref head; `--specification-revision` defaults to `HEAD`.
Pass the latter explicitly when `HEAD` might have advanced beyond the source revision reviewed in the source
environment.

Promotion is a resolution context, not a source-tree copy. A target StackTemplate is reused only from target desired
state when no authoritative partition selects the retained template, or supplied explicitly by `--file` input in its
`inline`, `fromGit`, or `fromPromotion` mode. A target
StackTemplate owned by an authoritative partition must be supplied explicitly so omission-based pruning remains
deterministic. `apply` rejects `fromPromotion`; it requires the explicit `promote` transaction and its pinned source
desired revision. `fromGit` is resolved from its requested ref and can be used where a StackTemplate input is accepted.
Field-level `fromPromotion` values and `artifactImports[].fromPromotion` are resolved against the pinned source desired
and observed revisions, with receipt, producer, artifact, and digest validation before publication.

Repository-backed Unit paths inherit the exact source context retained by the desired StackTemplate unless
`source.revision` selects an exact 40-hex commit. A StackTemplate parameter may provide that revision, allowing
independent Stacks to use different commits from the same repository and acquired-ref history. Direct authored Units
do not accept this field; they continue to use the operation's `--source-revision`. The effective revision
is resolved before projection, contributes to the structural and desired identity, and is retained under the
Stack/template incarnation. The driver `inputHash` reflects the selected bytes and deliberately excludes the revision
value itself, so byte-identical commits have the same input hash while remaining distinct projections. Updating the
template context reprojects inheritors; changing one Stack's override does not change another Stack.

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

Trusted PR CI may create, update, and delete a preview Stack with the normal CLI
primitives, then run `converge` against the live desired ref. Convergence performs
UID-/revision-fenced teardown and automatic child/dependent-first cleanup. A
scheduled CI job may enumerate preview refs or resources by lineage, consult the
forge for missed events, and request deletion intent; the deployment-owned job
remains responsible for lineage enumeration and forge API calls. This repository
does not provide a forge-aware recovery command or watcher.

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
