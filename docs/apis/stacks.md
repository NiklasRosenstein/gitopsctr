# Stacks and StackTemplates

`gitopsctr.io/v1` `StackTemplate` defines a parameterized collection of Unit templates. A `Stack` selects one
StackTemplate, supplies its parameters, optionally selects a subset of its Units, and may import artifacts from a
promoted source Stack. Projection gives each generated Unit a Stack-scoped name and records the resolved template in
immutable desired state.

## Basic authoring

Project-level StackTemplates live below `Project.spec.stackTemplatesPath`, which defaults to
`deployment/stack-templates`:

```yaml
apiVersion: gitopsctr.io/v1
kind: StackTemplate
metadata:
  name: application
spec:
  parameters:
    - name: workload-name
      type: string
    - name: message
      type: string
  unitTemplates:
    image:
      apiVersion: unit.gitopsctr.io/v1
      kind: OciImages
      spec:
        source: {path: .}
    deploy:
      apiVersion: unit.gitopsctr.io/v1
      kind: KubernetesManifests
      spec:
        source: {path: charts/application, inputs: ["**/*"]}
        materialize:
          type: helm
          releaseName:
            fromParameter: {name: workload-name}
          namespace: default
          values:
            message:
              fromParameter: {name: message}
            image:
              fromArtifact:
                unit: image
                name: containers
                apiVersion: artifact.gitopsctr.io/v1
                kind: ContainerImages
                pointer: /images/application/uri
```

Source-authored Stacks live below `<environmentsPath>/<environment>/stacks`:

```yaml
apiVersion: gitopsctr.io/v1
kind: Stack
metadata:
  name: application
spec:
  template: application
  parameters:
    workload-name: application-dev
    message: rendered in dev
  units: [image, deploy]
```

This generates `application--image` and `application--deploy`. References and `dependsOn` edges that use logical names
inside the template are scoped to those generated names.

`spec.units` is optional. When present, it selects logical Unit templates and must include the dependencies of every
selected Unit. `spec.parameters` must supply exactly the declared parameters.

## Template sources

The short form `template: application` selects the project-level StackTemplate available in the operation's pinned
specification tree. It is equivalent to selecting the named desired `StackTemplate` resource:

```yaml
template:
  name: application
  source:
    fromResource: {}
```

A Stack may instead request a StackTemplate from an explicitly pinned Git source:

```yaml
template:
  name: application
  source:
    fromGit:
      remote: https://github.com/example/deployment-templates.git
      commit: 0123456789abcdef0123456789abcdef01234567
```

Desired Stack resources record both the requested source and the resolved commit, resource path, and content digest.
They also store the expanded projection, so reconciliation does not rebuild a Stack from a mutable source branch.

## Promotion and template selection

Environment promotion and Stack template selection are separate decisions. A target Environment becomes
promotion-tracked through `Environment.spec.promotion.allowedSources`; `gitopsctr promote` then pins the source
desired revision, source observed revision, and target specification revision in a
[`Promotion`](promotion.md) resource.

The target Stack chooses its template source explicitly:

| Template source | Selection during promotion |
| --- | --- |
| `template: application` or `fromResource` | Load `application` from the Promotion's pinned target `specificationRevision`. |
| `fromGit` | Resolve the target-authored Git request independently and record the selected commit and digest. |
| `fromPromotion` | Read the selected source Stack from the pinned source desired revision and load its exact recorded template commit, path, and digest. |

A moving `fromGit.ref` is pinned when target desired state is resolved. Use an explicit commit when repeated attempts
must select the same source even if a ref moves. `fromPromotion` never resolves the source Stack's recorded ref again
and never substitutes `HEAD`; if its recorded repository, commit, path, or digest is unavailable, promotion fails.

The common pattern is to expand the target's parameterized template from the pinned specification revision and import
an exact artifact produced by the source Stack:

```yaml
apiVersion: gitopsctr.io/v1
kind: Stack
metadata:
  name: application
spec:
  template: application
  parameters:
    workload-name: application-staging
    message: promoted from dev to staging
  units: [deploy]
  artifactImports:
    - unit: image
      name: containers
      apiVersion: artifact.gitopsctr.io/v1
      kind: ContainerImages
      fromPromotion:
        stack: application
```

This has two independent effects:

1. `template: application` loads and expands the target StackTemplate from `Promotion.spec.specificationRevision`.
2. `artifactImports[].fromPromotion` finds the source Stack's `image` Unit in the pinned source desired revision,
   validates `containers` against its receipt in the pinned source observed revision, and makes that immutable artifact
   available to the selected `deploy` Unit.

The target can therefore use staging-specific parameters without rebuilding the image.

### Reusing a source Stack's template pin

The following is a different operation:

```yaml
spec:
  template:
    name: application
    source:
      fromPromotion:
        stack: application
  parameters:
    workload-name: application-staging
    message: promoted from dev to staging
  units: [deploy]
```

The controller loads `application` from the exact source pin recorded in the promoted source Stack's
`resolvedSource`, verifies the raw document digest and template identity, and expands the original parameterized
StackTemplate with the target parameters. In this example, the template code is exactly what dev pinned, while the
expanded workload name and message are staging-specific.

This source choice is independent of promoted artifacts. A Stack can combine `template.source.fromPromotion` with
`artifactImports[].fromPromotion`, selecting template lineage and artifact lineage separately. It may instead use a
target-owned `fromResource` or `fromGit` template and consume promoted fields or artifacts. A target-only companion
Stack may participate in the same promotion transaction without consuming any source value at all.

!!! note "Similar names, different scopes"

    - A field-level [`fromPromotion`](../references.md#promotion-selectors) expression reads public `spec` data from a
      source desired Unit.
    - `artifactImports[].fromPromotion` imports and validates an artifact using source desired and observed evidence.
    - `template.source.fromPromotion` reuses a source Stack's exact StackTemplate source pin, then applies target
      parameters and Unit selection.

## Desired-state records

Desired StackTemplate and Stack documents are controller-owned projections. Their metadata records lifecycle
authority and stable identity. A desired Stack additionally records its requested and resolved template source,
expanded Unit projection, and resolved promoted-artifact lineage. Generated Units carry a UID-fenced owner reference
to the Stack so updates and child-first deletion cannot cross Stack incarnations.

`resolvedProjection` is the immutable expanded record used for reconciliation, dependency and graph validation, and
teardown. Template selection never reconstructs a parameterized StackTemplate from another Stack's projection.

Do not author or edit desired Stack resources manually. The complete structural contracts are the StackTemplate
[authored](../schemas/apis/gitopsctr.io/v1/StackTemplate/authored.schema.json) and
[desired](../schemas/apis/gitopsctr.io/v1/StackTemplate/desired.schema.json) schemas, and the Stack
[authored](../schemas/apis/gitopsctr.io/v1/Stack/authored.schema.json) and
[desired](../schemas/apis/gitopsctr.io/v1/Stack/desired.schema.json) schemas.
