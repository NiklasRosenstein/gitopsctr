"""Discovery and type-safe registration for public API kinds."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib.metadata import entry_points
from types import MappingProxyType
from typing import cast


class ApiError(RuntimeError):
    pass


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
    """A globally registered GVK and the typed interface specification it implements."""

    gvk: GVK
    spec: T


def require_api_spec[T](api_kind: ApiKind[object], expected: type[T], interface: str) -> T:
    """Validate an API kind's interface and return its statically narrowed specification."""

    if not isinstance(api_kind.spec, expected):
        raise ApiError(f"API kind {api_kind.gvk} does not implement {interface}")
    return api_kind.spec


def load_api_kinds() -> dict[GVK, ApiKind[object]]:
    """Load exactly one authoritative registration for every installed GVK."""

    kinds: dict[GVK, ApiKind[object]] = {}
    for entry_point in entry_points(group="gitopsctr.apis"):
        api_kind = entry_point.load()
        if not isinstance(api_kind, ApiKind):
            raise ApiError(f"API entry point {entry_point.name!r} did not load an ApiKind")
        if entry_point.name != str(api_kind.gvk):
            raise ApiError(
                f"API entry point {entry_point.name!r} does not match declared GVK {str(api_kind.gvk)!r}"
            )
        if api_kind.gvk in kinds:
            raise ApiError(f"duplicate API kind entry point: {api_kind.gvk}")
        kinds[api_kind.gvk] = cast(ApiKind[object], api_kind)
    return kinds


@cache
def api_kinds() -> MappingProxyType[GVK, ApiKind[object]]:
    return MappingProxyType(load_api_kinds())


def registered_api_kind[T](api_kind: ApiKind[T]) -> ApiKind[T]:
    """Require a typed API handle to be the globally authoritative registration for its GVK."""

    registered = api_kinds().get(api_kind.gvk)
    if registered is None:
        raise ApiError(f"API kind is not installed: {api_kind.gvk}")
    if registered is not api_kind:
        raise ApiError(f"API kind {api_kind.gvk} is not its authoritative registered definition")
    return api_kind
