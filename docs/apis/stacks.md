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

### Reusing an expanded source projection

The following is a different operation:

```yaml
spec:
  template:
    name: application
    source:
      fromPromotion:
        stack: application
```

It reconstructs a template from the source desired Stack's `resolvedProjection`. The source Stack's parameters have
already been substituted, so the reconstructed template declares no parameters. The target must use that projection
as-is and cannot supply a new set of parameter values.

Use this form when the exact expanded Unit specifications are the promoted object. Use a local or Git-backed template
plus `artifactImports` when the target needs its own parameters while consuming exact artifacts proven by the source
environment.

!!! note "Similar names, different scopes"

    - A field-level [`fromPromotion`](../references.md#promotion-selectors) expression reads public `spec` data from a
      source desired Unit.
    - `artifactImports[].fromPromotion` imports and validates an artifact using source desired and observed evidence.
    - `template.source.fromPromotion` reuses a source Stack's already-expanded template projection.

## Desired-state records

Desired StackTemplate and Stack documents are controller-owned projections. Their metadata records lifecycle
authority and stable identity. A desired Stack additionally records its requested and resolved template source,
expanded Unit projection, and resolved promoted-artifact lineage. Generated Units carry a UID-fenced owner reference
to the Stack so updates and child-first deletion cannot cross Stack incarnations.

Do not author or edit desired Stack resources manually. The complete structural contracts are the StackTemplate
[authored](../schemas/apis/gitopsctr.io/v1/StackTemplate/authored.schema.json) and
[desired](../schemas/apis/gitopsctr.io/v1/StackTemplate/desired.schema.json) schemas, and the Stack
[authored](../schemas/apis/gitopsctr.io/v1/Stack/authored.schema.json) and
[desired](../schemas/apis/gitopsctr.io/v1/Stack/desired.schema.json) schemas.
