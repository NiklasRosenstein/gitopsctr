set -euo pipefail

require_input() {
  local name="$1"
  local value="$2"
  if [[ -z "${value}" ]]; then
    echo "${name} is required for operation ${OPERATION}" >&2
    exit 2
  fi
}

require_boolean() {
  local name="$1"
  local value="$2"
  if [[ "${value}" != "true" && "${value}" != "false" ]]; then
    echo "${name} must be true or false" >&2
    exit 2
  fi
}

require_boolean advance "${ADVANCE}"
require_boolean dry "${DRY}"
require_boolean reapply "${REAPPLY}"

working_directory="$(cd "${WORKING_DIRECTORY}" && pwd)"
args=(--repository "${working_directory}")

write_prepare_outputs() {
  local active="$1"
  local advance_after_reconcile="$2"
  local desired_changed="$3"
  local desired_revision="$4"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    {
      echo "active=${active}"
      echo "advance_after_reconcile=${advance_after_reconcile}"
      echo "desired_changed=${desired_changed}"
      echo "desired_revision=${desired_revision}"
    } >> "${GITHUB_OUTPUT}"
  fi
}

case "${OPERATION}" in
  prepare)
    require_input environment "${ENVIRONMENT}"
    if [[ -n "${SOURCE_REVISION}" && -n "${DESIRED_REVISION}" ]]; then
      echo "source-revision and desired-revision are mutually exclusive for operation prepare" >&2
      exit 2
    fi
    if [[ -z "${SOURCE_REVISION}" && -n "${REQUIRE_SOURCE_REF}" ]]; then
      echo "require-source-ref requires source-revision for operation prepare" >&2
      exit 2
    fi

    desired_changed=false
    advance_after_reconcile=true
    if [[ -n "${SOURCE_REVISION}" ]]; then
      args+=(advance-desired --environment "${ENVIRONMENT}")
      [[ -n "${DESIRED_REF}" ]] && args+=(--desired-ref "${DESIRED_REF}")
      [[ -n "${OBSERVED_REF}" ]] && args+=(--observed-ref "${OBSERVED_REF}")
      args+=(--source-revision "${SOURCE_REVISION}")
      [[ -n "${REQUIRE_SOURCE_REF}" ]] && args+=(--require-source-ref "${REQUIRE_SOURCE_REF}")
      [[ "${DRY}" == "true" ]] && args+=(--dry)
      prepare_outputs="$(mktemp)"
      trap 'rm -f "${prepare_outputs}"' EXIT
      desired_revision=$(GITHUB_OUTPUT="${prepare_outputs}" gitopsctr "${args[@]}")
      desired_changed=$(sed -n 's/^desired_changed=//p' "${prepare_outputs}" | tail -n 1)
      desired_changed="${desired_changed:-false}"
    else
      desired_ref="${DESIRED_REF:-deploy/${ENVIRONMENT}}"
      args+=(resolve-desired --desired-ref "${desired_ref}")
      if [[ -n "${DESIRED_REVISION}" ]]; then
        args+=(--desired-revision "${DESIRED_REVISION}")
        advance_after_reconcile=false
      fi
      desired_revision=$(gitopsctr "${args[@]}")
    fi

    if [[ -z "${desired_revision}" ]]; then
      write_prepare_outputs false false false ""
      if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
        echo "This run is superseded by a newer source revision." >> "${GITHUB_STEP_SUMMARY}"
      fi
    else
      write_prepare_outputs true "${advance_after_reconcile}" "${desired_changed}" "${desired_revision}"
    fi
    exit 0
    ;;
  reconcile)
    require_input environment "${ENVIRONMENT}"
    require_input unit "${UNIT}"
    args+=(reconcile --environment "${ENVIRONMENT}" --unit "${UNIT}")
    [[ -n "${DESIRED_REF}" ]] && args+=(--desired-ref "${DESIRED_REF}")
    [[ -n "${OBSERVED_REF}" ]] && args+=(--observed-ref "${OBSERVED_REF}")
    [[ -n "${DESIRED_REVISION}" ]] && args+=(--desired-revision "${DESIRED_REVISION}")
    [[ -n "${SOURCE_REVISION}" ]] && args+=(--source-revision "${SOURCE_REVISION}")
    [[ -n "${REQUIRE_SOURCE_REF}" ]] && args+=(--require-source-ref "${REQUIRE_SOURCE_REF}")
    [[ -n "${REPORT}" ]] && args+=(--report "${REPORT}")
    [[ "${ADVANCE}" == "true" ]] && args+=(--advance)
    [[ "${DRY}" == "true" ]] && args+=(--dry)
    [[ "${REAPPLY}" == "true" ]] && args+=(--reapply)
    ;;
  advance)
    require_input environment "${ENVIRONMENT}"
    args+=(advance-desired --environment "${ENVIRONMENT}")
    [[ -n "${DESIRED_REF}" ]] && args+=(--desired-ref "${DESIRED_REF}")
    [[ -n "${OBSERVED_REF}" ]] && args+=(--observed-ref "${OBSERVED_REF}")
    [[ -n "${SOURCE_REVISION}" ]] && args+=(--source-revision "${SOURCE_REVISION}")
    [[ -n "${REQUIRE_SOURCE_REF}" ]] && args+=(--require-source-ref "${REQUIRE_SOURCE_REF}")
    [[ "${DRY}" == "true" ]] && args+=(--dry)
    ;;
  promote)
    require_input from-environment "${FROM_ENVIRONMENT}"
    require_input to-environment "${TO_ENVIRONMENT}"
    specification_revision="${SPECIFICATION_REVISION:-${WORKFLOW_REVISION}}"
    require_input specification-revision "${specification_revision}"
    args+=(
      promote
      --from-environment "${FROM_ENVIRONMENT}"
      --to-environment "${TO_ENVIRONMENT}"
      --specification-revision "${specification_revision}"
    )
    [[ -n "${SOURCE_REVISION}" ]] && args+=(--source-desired-revision "${SOURCE_REVISION}")
    [[ -n "${CANDIDATE_REF}" ]] && args+=(--candidate-ref "${CANDIDATE_REF}")
    ;;
  *)
    echo "operation must be prepare, reconcile, advance, or promote" >&2
    exit 2
    ;;
esac

gitopsctr "${args[@]}"
