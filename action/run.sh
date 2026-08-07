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

case "${OPERATION}" in
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
    echo "operation must be reconcile, advance, or promote" >&2
    exit 2
    ;;
esac

gitopsctr "${args[@]}"
