"""Registered API kinds for typed inspection output documents."""

from __future__ import annotations

from dataclasses import dataclass

from gitopsctr.api import GVK, ApiKind
from gitopsctr.contracts import INSPECTION_RESOURCE_LIST_CONTRACT
from gitopsctr.document import DocumentContract

INSPECTION_API_VERSION = "inspection.gitopsctr.io/v1"


@dataclass(frozen=True)
class InspectionOutputApi:
    """A generated CLI output kind that is not a persisted resource family."""

    description: str
    contract: DocumentContract


RESOURCE_LIST = ApiKind(
    GVK(INSPECTION_API_VERSION, "ResourceList"),
    InspectionOutputApi("Provenance-bearing list of inspected resources", INSPECTION_RESOURCE_LIST_CONTRACT),
)
