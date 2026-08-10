# Changelog

## 0.2.2

- Add project-level `sourceRevisionPolicy.refreshWhen` configuration for refreshing unchanged unit source revisions.
- Default unchanged source revisions to `outside-candidate-history`, with `missing` available for GitOpsCTR 0.2.1
  compatibility.
- Apply refresh decisions consistently to `advance-desired` and `reconcile --plan`.
