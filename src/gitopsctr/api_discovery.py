"""Default-composition discovery for installed GitOpsCtr API kinds."""

from __future__ import annotations

from functools import cache
from importlib.metadata import entry_points
from types import MappingProxyType
from typing import cast

from gitopsctr.resource_api import GVK, ApiError, ApiKind


def load_api_kinds() -> dict[GVK, ApiKind[object]]:
    """Load exactly one authoritative registration for every installed GVK."""

    kinds: dict[GVK, ApiKind[object]] = {}
    for entry_point in entry_points(group="gitopsctr.apis"):
        api_kind = entry_point.load()
        if not isinstance(api_kind, ApiKind):
            raise ApiError(f"API entry point {entry_point.name!r} did not load an ApiKind")
        if entry_point.name != str(api_kind.gvk):
            raise ApiError(f"API entry point {entry_point.name!r} does not match declared GVK {str(api_kind.gvk)!r}")
        if api_kind.gvk in kinds:
            raise ApiError(f"duplicate API kind entry point: {api_kind.gvk}")
        kinds[api_kind.gvk] = cast(ApiKind[object], api_kind)
    return kinds


@cache
def api_kinds() -> MappingProxyType[GVK, ApiKind[object]]:
    """Return the compatibility-era process cache owned by the composition layer."""

    return MappingProxyType(load_api_kinds())


def registered_api_kind[T](api_kind: ApiKind[T]) -> ApiKind[T]:
    """Require the installed registration object for a GVK."""

    registered = api_kinds().get(api_kind.gvk)
    if registered is None:
        raise ApiError(f"API kind is not installed: {api_kind.gvk}")
    if registered is not api_kind:
        raise ApiError(f"API kind {api_kind.gvk} is not its authoritative registered definition")
    return api_kind
