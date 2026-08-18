"""Internal, dependency-clean resource API kernel."""

from gitopsctr.resource_api.api import GVK, ApiError, ApiKind, require_api_spec
from gitopsctr.resource_api.document import (
    ContractError,
    DocumentContract,
    JsonObject,
    JsonScalar,
    JsonValue,
    TypedDocumentContract,
    require_json_value,
)

__all__ = [
    "ApiError",
    "ApiKind",
    "ContractError",
    "DocumentContract",
    "GVK",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "TypedDocumentContract",
    "require_api_spec",
    "require_json_value",
]
