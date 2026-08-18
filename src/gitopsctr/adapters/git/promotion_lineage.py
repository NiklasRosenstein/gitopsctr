"""Git-only bridge from issued promotion evidence to the legacy schema shape."""

from __future__ import annotations

import re
from collections.abc import Mapping, Set
from dataclasses import dataclass
from types import MappingProxyType

from gitopsctr.application.apply_compilers import PromotionLineage
from gitopsctr.application.apply_projection import PromotionSourceDescriptor
from gitopsctr.application.model import ChannelId, EnvironmentId

_GIT_SNAPSHOT = re.compile(r"git-commit:([0-9a-f]{40})$")


class GitPromotionLineageError(ValueError):
    """Issued promotion evidence is not representable by the legacy Git contract."""


@dataclass(frozen=True, slots=True)
class GitPromotionLineageEncoder:
    """Explicitly translate exact Git channel/snapshot evidence for promotion.

    ``allowed_sources`` is target-environment policy, supplied by composition.
    Every channel is mapped explicitly, so a caller cannot turn an opaque
    channel spelling into a Git ref by convention.
    """

    desired_refs: Mapping[ChannelId, str]
    observed_refs: Mapping[ChannelId, str]
    allowed_sources: Mapping[EnvironmentId, Set[EnvironmentId]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "desired_refs", MappingProxyType(dict(self.desired_refs)))
        object.__setattr__(self, "observed_refs", MappingProxyType(dict(self.observed_refs)))
        object.__setattr__(
            self,
            "allowed_sources",
            MappingProxyType({target: frozenset(sources) for target, sources in self.allowed_sources.items()}),
        )
        for mapping in (self.desired_refs, self.observed_refs):
            for channel, reference in mapping.items():
                if (
                    not isinstance(channel, ChannelId)
                    or not isinstance(reference, str)
                    or not _valid_git_ref(reference)
                ):
                    raise GitPromotionLineageError("Git promotion channel mapping contains an invalid ref")
        for target, sources in self.allowed_sources.items():
            if not isinstance(target, EnvironmentId) or not all(
                isinstance(source, EnvironmentId) for source in sources
            ):
                raise GitPromotionLineageError("Git promotion policy contains invalid environment identities")

    def encode(self, descriptor: PromotionSourceDescriptor) -> PromotionLineage:
        descriptor._validate()
        allowed = self.allowed_sources.get(descriptor.target_environment)
        if allowed is None or descriptor.source_environment not in allowed:
            raise GitPromotionLineageError(
                f"promotion from {descriptor.source_environment!s} to {descriptor.target_environment!s} is not allowed"
            )
        source_desired_ref = self._ref(self.desired_refs, descriptor.source_desired.head.channel_id, "source desired")
        source_observed_ref = self._ref(
            self.observed_refs, descriptor.source_observed.head.channel_id, "source observed"
        )
        target_desired_ref = self._ref(self.desired_refs, descriptor.target_desired.head.channel_id, "target desired")
        target_observed_ref = self._ref(
            self.observed_refs, descriptor.target_observed.head.channel_id, "target observed"
        )
        return PromotionLineage(
            source_environment=descriptor.source_environment,
            source_desired_ref=source_desired_ref,
            source_desired_revision=self._revision(descriptor.source_desired, "source desired"),
            source_observed_ref=source_observed_ref,
            source_observed_revision=self._revision(descriptor.source_observed, "source observed"),
            target_desired_ref=target_desired_ref,
            target_desired_revision=self._revision(descriptor.target_desired, "target desired"),
            target_observed_ref=target_observed_ref,
            target_observed_revision=self._revision(descriptor.target_observed, "target observed"),
            lineage_evidence=descriptor.lineage_evidence,
        )

    @staticmethod
    def _ref(mapping: Mapping[ChannelId, str], channel: ChannelId, description: str) -> str:
        try:
            return mapping[channel]
        except KeyError as exc:
            raise GitPromotionLineageError(f"Git promotion has no {description} channel/ref mapping") from exc

    @staticmethod
    def _revision(plane: object, description: str) -> str:
        snapshot = getattr(getattr(plane, "head", None), "snapshot_id", None)
        value = getattr(snapshot, "value", None)
        match = _GIT_SNAPSHOT.fullmatch(value) if isinstance(value, str) else None
        if match is None:
            raise GitPromotionLineageError(f"Git promotion {description} snapshot is not an exact git-commit ID")
        return match.group(1)


def _valid_git_ref(reference: str) -> bool:
    """Pure equivalent of the relevant ``git check-ref-format`` rules."""

    if not reference or reference.startswith("/") or reference.endswith("/") or reference.endswith("."):
        return False
    if ".." in reference or "@{" in reference or "\\" in reference or "//" in reference:
        return False
    forbidden = set(" ~^:?*[")
    if any(character in forbidden or ord(character) < 32 or ord(character) == 127 for character in reference):
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock") for part in reference.split("/"))
