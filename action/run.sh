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

require_boolean dry "${DRY}"
require_boolean plan "${PLAN}"
require_boolean reapply "${REAPPLY}"

if [[ "${PLAN}" == "true" && "${OPERATION}" != "reconcile" ]]; then
  echo "plan is only valid for operation reconcile" >&2
  exit 2
fi
if [[ "${DRY}" == "true" && "${OPERATION}" != "apply" && "${OPERATION}" != "rollback" ]]; then
  echo "dry is only valid for operations apply and rollback" >&2
  exit 2
fi
if [[ "${PLAN}" == "true" && "${DRY}" == "true" ]]; then
  echo "plan and dry are mutually exclusive" >&2
  exit 2
fi
if [[ -n "${FILES}" && "${OPERATION}" != "apply" && "${OPERATION}" != "converge" && "${OPERATION}" != "promote" ]]; then
  echo "files is only valid for operations apply, converge, and promote" >&2
  exit 2
fi
if [[ -n "${PARTITION}" && "${OPERATION}" != "apply" && "${OPERATION}" != "converge" && "${OPERATION}" != "promote" ]]; then
  echo "partition is only valid for operations apply, converge, and promote" >&2
  exit 2
fi

working_directory="$(cd "${WORKING_DIRECTORY}" && pwd)"
cd "${working_directory}"
args=(--repository "${working_directory}")

write_prepare_outputs() {
  local active="$1"
  local desired_revision="$2"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    {
      echo "active=${active}"
      echo "desired_revision=${desired_revision}"
    } >> "${GITHUB_OUTPUT}"
  fi
}

append_files() {
  local count=0
  local raw
  while IFS= read -r raw || [[ -n "${raw}" ]]; do
    raw="${raw#"${raw%%[![:space:]]*}"}"
    raw="${raw%"${raw##*[![:space:]]}"}"
    [[ -z "${raw}" ]] && continue
    args+=(--file "${raw}")
    count=$((count + 1))
  done <<< "${FILES}"
  FILE_COUNT="${count}"
}

case "${OPERATION}" in
  prepare)
    require_input environment "${ENVIRONMENT}"
    if [[ -n "${SOURCE_REVISION}" || -n "${REQUIRE_SOURCE_REF}" ]]; then
      echo "prepare selects existing desired state and does not accept source input" >&2
      exit 2
    fi
    desired_ref="${DESIRED_REF:-gitopsctr/desired/${ENVIRONMENT}}"
    args+=(resolve-desired --desired-ref "${desired_ref}")
    [[ -n "${DESIRED_REVISION}" ]] && args+=(--desired-revision "${DESIRED_REVISION}")
    desired_revision=$(gitopsctr "${args[@]}")

    if [[ -z "${desired_revision}" ]]; then
      write_prepare_outputs false ""
    else
      write_prepare_outputs true "${desired_revision}"
    fi
    exit 0
    ;;
  apply)
    require_input environment "${ENVIRONMENT}"
    args+=(apply --environment "${ENVIRONMENT}")
    append_files
    if [[ "${FILE_COUNT}" == "0" ]]; then
      echo "files is required for operation apply" >&2
      exit 2
    fi
    [[ -n "${PARTITION}" ]] && args+=(--partition "${PARTITION}")
    [[ -n "${SOURCE_REVISION}" ]] && args+=(--source-revision "${SOURCE_REVISION}")
    [[ -n "${DESIRED_REF}" ]] && args+=(--desired-ref "${DESIRED_REF}")
    [[ -n "${OBSERVED_REF}" ]] && args+=(--observed-ref "${OBSERVED_REF}")
    [[ -n "${CANDIDATE_REF}" ]] && args+=(--candidate-ref "${CANDIDATE_REF}")
    [[ "${DRY}" == "true" ]] && args+=(--dry)
    ;;
  converge)
    require_input environment "${ENVIRONMENT}"
    if [[ -n "${UNIT}" && -n "${PARTITION}" ]]; then
      echo "unit and partition are mutually exclusive for operation converge" >&2
      exit 2
    fi
    args+=(converge --environment "${ENVIRONMENT}")
    append_files
    [[ -n "${PARTITION}" ]] && args+=(--partition "${PARTITION}")
    [[ -n "${UNIT}" ]] && args+=(--unit "${UNIT}")
    [[ -n "${SOURCE_REVISION}" ]] && args+=(--source-revision "${SOURCE_REVISION}")
    [[ -n "${DESIRED_REF}" ]] && args+=(--desired-ref "${DESIRED_REF}")
    [[ -n "${OBSERVED_REF}" ]] && args+=(--observed-ref "${OBSERVED_REF}")
    [[ -n "${CANDIDATE_REF}" ]] && args+=(--candidate-ref "${CANDIDATE_REF}")
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
    [[ "${PLAN}" == "true" ]] && args+=(--plan)
    [[ "${REAPPLY}" == "true" ]] && args+=(--reapply)
    ;;
  rollback)
    require_input environment "${ENVIRONMENT}"
    require_input rollback-revision "${ROLLBACK_REVISION}"
    require_input reason "${ROLLBACK_REASON}"
    args+=(
      rollback
      --environment "${ENVIRONMENT}"
      --to-desired-revision "${ROLLBACK_REVISION}"
      --reason "${ROLLBACK_REASON}"
    )
    [[ -n "${DESIRED_REF}" ]] && args+=(--desired-ref "${DESIRED_REF}")
    [[ -n "${OBSERVED_REF}" ]] && args+=(--observed-ref "${OBSERVED_REF}")
    [[ -n "${CANDIDATE_REF}" ]] && args+=(--candidate-ref "${CANDIDATE_REF}")
    if [[ -n "${ROLLBACK_UNITS}" ]]; then
      IFS=',' read -ra rollback_units <<< "${ROLLBACK_UNITS}"
      for raw_unit in "${rollback_units[@]}"; do
        unit="${raw_unit#"${raw_unit%%[![:space:]]*}"}"
        unit="${unit%"${unit##*[![:space:]]}"}"
        if [[ -z "${unit}" ]]; then
          echo "units contains an empty unit name" >&2
          exit 2
        fi
        args+=(--unit "${unit}")
      done
    fi
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
    append_files
    if [[ "${FILE_COUNT}" == "0" ]]; then
      echo "files is required for operation promote" >&2
      exit 2
    fi
    [[ -n "${PARTITION}" ]] && args+=(--partition "${PARTITION}")
    [[ -n "${SOURCE_REVISION}" ]] && args+=(--source-desired-revision "${SOURCE_REVISION}")
    [[ -n "${CANDIDATE_REF}" ]] && args+=(--candidate-ref "${CANDIDATE_REF}")
    ;;
  *)
    echo "operation must be prepare, apply, converge, reconcile, promote, or rollback" >&2
    exit 2
    ;;
esac

gitopsctr "${args[@]}"
