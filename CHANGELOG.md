# Changelog

## 0.2.2

- Add project-level `sourceRevisionPolicy` configuration for detecting unavailable retained source revisions and
  selecting separate advance and plan actions.
- Default unavailable detection to `outside-candidate-history`, with `missing` available for GitOpsCTR 0.2.1
  compatibility; advancement refreshes by default while planning errors by default.
- Allow `reconcile --plan` to use an explicitly refreshed source only in its ephemeral dry candidate.
