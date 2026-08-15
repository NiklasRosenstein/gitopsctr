# Stacks and StackTemplates

`gitopsctr.io/v1` `StackTemplate` is a parameterized collection of Unit templates. Authored input can provide that
content inline, acquire it from a repository at `spec.source.fromGit`, or (in an explicit promotion transaction) select
the source StackTemplate with `spec.source.fromPromotion`. Desired state always stores the resolved inline content. An
explicitly applied StackTemplate is an independent desired root. A `Stack` selects a desired StackTemplate in the same
environment and is projected into UID-fenced generated Units.

## Authoring and applying

Apply the template and any Stacks that use it explicitly:

```console
gitopsctr apply --environment dev -f template.yaml -f stack.yaml
```

The authored documents are:

```yaml
apiVersion: gitopsctr.io/v1
kind: StackTemplate
metadata:
  name: application
spec:
  parameters:
    - name: workload-name
      type: string
  unitTemplates:
    deploy:
      apiVersion: unit.gitopsctr.io/v1
      kind: KubernetesManifests
      spec:
        source: {path: charts/application, inputs: ["**/*"]}
        materialize:
          type: plain
```

```yaml
apiVersion: gitopsctr.io/v1
kind: Stack
metadata:
  name: application
spec:
  template: application
  parameters:
    workload-name: application-dev
```

`spec.units` is optional. When present, it selects logical Unit templates and must include every dependency of each
selected Unit. `spec.parameters` must supply exactly the declared parameters.

### Acquisition modes

Inline content is the ordinary mode and is accepted by `apply` and `promote`:

```yaml
apiVersion: gitopsctr.io/v1
kind: StackTemplate
metadata:
  name: application
spec:
  parameters: []
  unitTemplates:
    deploy:
      apiVersion: unit.gitopsctr.io/v1
      kind: KubernetesManifests
      spec:
        source: {path: charts/application, inputs: ["**/*"]}
```

A repository-backed selector is resolved before the desired StackTemplate is written:

```yaml
apiVersion: gitopsctr.io/v1
kind: StackTemplate
metadata:
  name: application
spec:
  source:
    fromGit:
      repository: https://github.com/example/deployments.git
      revision: main
      path: deployment/stack-templates/application.yaml
      # Optional SHA-256 of the selected serialized document bytes.
      documentDigest: sha256:<raw-document-digest>
```

`fromGit` accepts a branch, tag, or other Git ref and resolves it to one exact commit. The desired acquisition record
retains the requested repository/ref/path and the resolved credential-free repository, exact commit, and path. Repository
credentials are transport configuration, not persisted resource data.

Promotion can select the already resolved source StackTemplate through the source Stack pinned by the promotion:

```yaml
apiVersion: gitopsctr.io/v1
kind: StackTemplate
metadata:
  name: application
spec:
  source:
    fromPromotion:
      stack: application
```

`fromPromotion` is legal only in an explicit `promote` transaction. `apply` rejects it because apply has no pinned source
desired revision from which to resolve the Stack and its template. Promotion records the requested source Stack and the
resolved source environment, desired ref, exact desired revision, Stack UID, template UID, and template content digest.
The promoted template's inline content and its retained `sourceContext` are then copied into target desired state.

Applying a StackTemplate without a Stack keeps the template as an unreferenced desired root. Applying changed content
preserves that root's UID, changes its semantic `contentDigest`, and atomically reprojects every referring Stack in the
complete desired candidate.

## Repository-backed Unit sources

Inline Unit templates may contain repository-backed Unit paths. Such a template must be applied with an exact
`--source-revision <commit>`. Desired state records that revision in `spec.sourceContext`; a later Stack apply can
project from the stored revision without another source revision. Source-less inline projections do not need a source
context.

An explicit promotion may also reuse a retained target StackTemplate when its target input is Stack-only and no
authoritative partition selects that template. When an authoritative partition selects the template, supply the target
StackTemplate explicitly. It is never implicitly acquired from source promotion state or Git. This partition-sensitive
rule keeps omission-based pruning deterministic: a partition that owns the template must state its complete desired
membership.

## Desired-state records

Desired StackTemplates retain their full parameterized `parameters` and `unitTemplates`, a semantic `contentDigest`,
and an immutable `acquisition` record. `acquisition.documentDigest` is the SHA-256 digest of the serialized selected
StackTemplate document bytes; it is a raw-document integrity check and is distinct from `contentDigest`, which is the
semantic digest of the resolved inline template content. `requestedSource` preserves the authored mode and selectors;
`resolvedSource` preserves the exact Git commit or promotion lineage used to produce the inline content. Inline input has
`fromInput` in both source records.

When repository-backed Unit paths occur inside the resolved inline content, `sourceContext` retains the credential-free
repository identity and exact commit needed for later Stack-only projection. Inline templates use repository `.` plus
the exact `--source-revision`; `fromGit` uses the imported repository and commit; `fromPromotion` carries forward the
source template's context. Later `converge` or Stack-only apply can use this retained context without rereading the
original checkout.

Desired Stacks contain a mandatory:

```yaml
templateRef:
  name: application
  uid: <StackTemplate UID>
  contentDigest: sha256:<semantic-template-digest>
```

They also contain one mandatory `structuralProjection`. Its identity is fenced by the Stack UID, selected
StackTemplate UID and content digest, and its projection digest is derived from the canonical Unit GVKs, resolved specs,
and required `dependsOn` lists. The persisted topology is authoritative and is checked against the selected
StackTemplate before publication.

When Units are active, `activeProjection` records both the structural projection digest it was activated from and the
`projectionContextDigest` used to resolve those Units. A blocked structural transition retains the prior active Unit
set and context as one immutable lineage. Reconciliation of a retained active Unit therefore uses its active context;
the structural context is used only after the active projection catches up. Both referenced context records must remain
available in the desired snapshot.

Generated Units use names such as `application--deploy` and carry an owner reference fenced by the exact Stack
`apiVersion`, kind, name, and UID. Missing, stale, cyclic, or unknown dependencies and unsupported unresolved dynamic
artifact/receipt/promotion evidence fail before desired publication.

Artifact imports with `artifactImports[].fromPromotion` are a separate Unit artifact-lineage feature; they do not select
or acquire a StackTemplate.

### Projection source propagation

Repository-backed Unit paths inherit the desired StackTemplate's exact source context. Applying that StackTemplate
requires `--source-revision <commit>`; a later Stack-only apply reads the retained context and does not need the source
checkout. The current authored Unit source contract has no independent per-Stack revision selector. Updating the
inline template's source context advances its referring Stacks together.

Inspect desired representations with:

```console
gitopsctr get stacks --environment dev
gitopsctr get stack application --environment dev -o yaml
gitopsctr get stacktemplates --environment dev
gitopsctr get stacktemplate application --environment dev -o yaml
```

The complete public contracts are the StackTemplate
[authored](../schemas/apis/gitopsctr.io/v1/StackTemplate/authored.schema.json) and
[desired](../schemas/apis/gitopsctr.io/v1/StackTemplate/desired.schema.json) schemas, and the Stack
[authored](../schemas/apis/gitopsctr.io/v1/Stack/authored.schema.json) and
[desired](../schemas/apis/gitopsctr.io/v1/Stack/desired.schema.json) schemas.
