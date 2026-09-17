"""Literal tool-input types understood by the strict harness schema generator."""

from typing import NotRequired, TypedDict

__all__ = ["Constraint", "MetricOptions", "Objective", "ProposalValue", "ProseQuality"]

# Keep this finite: the harness resolves recursive aliases before building schemas.
type ProposalValue = str | int | float | bool | list[str] | list[int] | None


class MetricOptions(TypedDict, total=False):
    """Supported options whose values the agent can write without storage references."""

    average: str
    positive_label: str
    label: str
    seasonal_period: int


class Objective(TypedDict, total=False):
    """Only the objective details explicitly stated in the natural-language task."""

    metric: str
    direction: str
    parameters: MetricOptions


class Constraint(TypedDict):
    """An explicit prose quality bound, with optional metric-specific settings."""

    metric: str
    operator: str
    value: float
    parameters: NotRequired[MetricOptions]


class ProseQuality(TypedDict, total=False):
    """Independent prose requirements; omitted fields make no additional requirement."""

    objective: Objective
    constraints: list[Constraint]
