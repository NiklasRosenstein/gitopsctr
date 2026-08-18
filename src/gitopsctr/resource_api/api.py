"""Library-neutral identities and registrations for versioned API kinds."""

from __future__ import annotations

from dataclasses import dataclass


class ApiError(RuntimeError):
    """An API-kind registration does not satisfy the kernel contract."""


@dataclass(frozen=True, order=True)
class GVK:
    """The universal identity of a versioned API resource kind."""

    api_version: str
    kind: str

    def __post_init__(self) -> None:
        if not self.api_version or "/" not in self.api_version or not self.kind:
            raise ValueError(f"invalid API kind {self.api_version!r}/{self.kind!r}")

    def __str__(self) -> str:
        return f"{self.api_version}/{self.kind}"


@dataclass(frozen=True)
class ApiKind[T]:
    """One authoritative GVK and the typed interface specification it implements."""

    gvk: GVK
    spec: T


def require_api_spec[T](api_kind: ApiKind[object], expected: type[T], interface: str) -> T:
    """Validate an API kind's interface and return its statically narrowed specification."""

    if not isinstance(api_kind.spec, expected):
        raise ApiError(f"API kind {api_kind.gvk} does not implement {interface}")
    return api_kind.spec
