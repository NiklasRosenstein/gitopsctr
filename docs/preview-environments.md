# Preview environments

A preview is an ordinary Environment whose resources are constructed from explicit apply input. A Stack is a useful
root because it expands one parameterized StackTemplate into owned Units that can be reconciled and finalized as a
group.

## Apply a preview

Author a Stack document for the preview instance:

```yaml
apiVersion: gitopsctr.io/v1
kind: Stack
metadata:
  name: pr-123
spec:
  template: preview
  parameters:
    namespace: preview-123
    expiresAt: 1735689600
  units: [image, deploy]
```

Apply it at a trusted source revision:

```console
gitopsctr apply \
  --environment preview \
  --file deployment/stack-templates/application.yaml \
  --file deployment/previews/pr-123.yaml \
  --source-revision "$GITHUB_SHA"
```

This unpartitioned application updates only `pr-123`; absence from any later apply has no meaning. Reapplying the same
document at a new source revision preserves the root and child identities while resolving the new template and source
pins. A no-op application creates no desired commit.

Use a partition when one CI invocation owns a complete set of roots:

```console
gitopsctr apply \
  --environment preview \
  --partition pull-request-123 \
  --file deployment/stack-templates/application.yaml \
  --file deployment/previews/pr-123/
```

Omitting a previous member from that same partition begins its deletion. A root cannot be silently moved from another
partition. Stack-generated Units use `ownerReferences`; they inherit selection and teardown from their Stack rather
than carrying a partition label themselves.

## Converge

Pass the same explicit input to apply and reconcile repeatedly until the preview is clean:

```console
gitopsctr converge \
  --environment preview \
  --file deployment/stack-templates/application.yaml \
  --file deployment/previews/pr-123.yaml \
  --source-revision "$GITHUB_SHA" \
  --yes
```

`converge` without `--file` reconciles persisted desired Units and can re-project the durable StackTemplate/Stack
inputs when new evidence arrives. It does not reconstruct unrelated authored configuration. `--partition NAME`
selects every Unit rooted in that partition and is shorthand for passing those Unit names through `--unit`; with
neither selector, converge targets the whole Environment.

## Inspect and delete

Use `get` as the primary inspection utility:

```console
gitopsctr get stack pr-123 --environment preview
gitopsctr get units --environment preview
gitopsctr get receipts --environment preview
gitopsctr status --environment preview
```

When an unpartitioned preview is no longer eligible, request UID-fenced deletion explicitly:

```console
gitopsctr delete stack \
  --environment preview \
  --name pr-123 \
  --uid "$STACK_UID"
```

Deletion is recorded on the retained desired resource. Reconcile and finalize owned Units in reverse dependency order,
then finalize the Stack with its UID and deletion-generation fences. Partitioned previews can instead be omitted from
the next authoritative application of their partition.

## CI and forge boundary

Forge identity is provenance only. Trusted CI decides whether a pull request is eligible and supplies explicit input
to `apply` or `converge`; gitopsctr does not inspect forge state or persist authored input for later reconstruction. A
scheduled deployment job may enumerate previews, consult the forge for missed events, and invoke the same apply or
UID-fenced deletion operations.

For Argo CD, use a trusted ApplicationSet or Application that watches the preview Environment's desired ref and the
Stack-owned Unit's materialized path. The Kubernetes demo exercises this boundary with:

```console
mise run demo-k8s acceptance --preview --delivery argocd
```

The demo applies the preview Stack without a partition, publishes its materialized manifest to
`gitopsctr/desired/preview`, and lets Argo CD perform external delivery while gitopsctr observes it.
