# Stacks and StackTemplates

`gitopsctr.io/v1` `StackTemplate` is an inline, parameterized collection of Unit templates. A directly applied
StackTemplate is an independent desired root. A `Stack` selects a StackTemplate in the same environment and is
projected into UID-fenced generated Units.

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

Applying a StackTemplate without a Stack keeps the template as an unreferenced desired root. Applying changed content
preserves that root's UID, changes its semantic `contentDigest`, and atomically reprojects every referring Stack in the
complete desired candidate.

## Repository-backed Unit sources

Inline Unit templates may contain repository-backed Unit paths. Such a template must be applied with an exact
`--source-revision <commit>`. Desired state records that revision in `spec.sourceContext`; a later Stack apply can
project from the stored revision without another source revision. Source-less inline projections do not need a source
context.

External Git, promotion, and Stack-owned template source modes are not part of this contract. Documents using
`fromResource`, `fromGit`, or a template `fromPromotion` source are rejected until a later acquisition slice exists.

## Desired-state records

Desired StackTemplates retain their full inline `parameters` and `unitTemplates`, a semantic `contentDigest`, one
canonical direct-input `acquisition` record, and optional exact `sourceContext`.

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
