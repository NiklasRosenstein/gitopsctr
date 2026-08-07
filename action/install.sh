set -euo pipefail

case "${PACKAGE_SOURCE}" in
  pypi)
    if [[ -n "${PACKAGE_REPOSITORY}" || -n "${PACKAGE_REVISION}" ]]; then
      echo "package-repository and package-revision require package-source=git" >&2
      exit 2
    fi
    package="gitopsctr"
    if [[ -n "${PACKAGE_VERSION}" ]]; then
      package="${package}==${PACKAGE_VERSION}"
    fi
    ;;
  action)
    if [[ -n "${PACKAGE_VERSION}" || -n "${PACKAGE_REPOSITORY}" || -n "${PACKAGE_REVISION}" ]]; then
      echo "package-version, package-repository, and package-revision do not apply to package-source=action" >&2
      exit 2
    fi
    package="${GITHUB_ACTION_PATH}"
    ;;
  git)
    if [[ -n "${PACKAGE_VERSION}" ]]; then
      echo "package-version applies only to package-source=pypi" >&2
      exit 2
    fi
    if [[ -z "${PACKAGE_REPOSITORY}" || -z "${PACKAGE_REVISION}" ]]; then
      echo "package-source=git requires package-repository and package-revision" >&2
      exit 2
    fi
    repository="${PACKAGE_REPOSITORY}"
    if [[ "${repository}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
      repository="https://github.com/${repository}.git"
    fi
    if [[ "${repository}" != git+* ]]; then
      repository="git+${repository}"
    fi
    package="gitopsctr @ ${repository}@${PACKAGE_REVISION}"
    ;;
  *)
    echo "package-source must be pypi, action, or git" >&2
    exit 2
    ;;
esac

uv tool install --force "${package}"
if [[ -n "${GITHUB_PATH:-}" ]]; then
  uv tool dir --bin >> "${GITHUB_PATH}"
fi
