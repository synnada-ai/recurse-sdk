"""Opt-in checks against Agentia's actual strict schemas and argument conversion."""

import json
from pathlib import Path
from typing import Any, get_type_hints

import pytest
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.harness


def _check(tools: Any, name: str, arguments: dict[str, Any]) -> None:
    """Validate literal proposals with the engine rather than calling Python directly."""
    core = pytest.importorskip("framework.tools.core")
    schema = pytest.importorskip("framework.tools.schema")
    settings = yaml.safe_load((ROOT / "agent.yaml").read_text())["tools"]["register"][name]
    function = getattr(tools, name)
    definition = core.ToolDefinition(
        name, function, strict=True, no_storage=frozenset(settings["no_storage"])
    )
    assert definition.subst_paths.all_mandatory == frozenset()
    Draft202012Validator(definition.json_schema).validate(arguments)
    hints = get_type_hints(function)
    for key, value in arguments.items():
        converted = schema.from_jsonvalue(value, hints[key], function.__globals__)
        assert converted == value


@pytest.mark.parametrize(
    "name", ["binary", "multiclass", "multilabel", "regression", "forecast", "panel-forecast"]
)
def test_each_task_can_supply_its_review_and_specification_inline(tools: Any, name: str) -> None:
    """All real-data task shapes are constructible without upstream stored objects."""
    request = json.loads((ROOT / "inputs" / f"{name}.json").read_text())
    specification = json.loads((ROOT / "tests/specifications.json").read_text())[name]
    _check(
        tools,
        "review_inputs",
        {
            "task_summary": request["task"],
            "task_quality": request["quality"],
            "conflicts": [],
            "questions": [],
        },
    )
    _check(tools, "resolve_problem", {"specification": specification})


@pytest.mark.parametrize(
    "configuration",
    [
        {"family": "baseline"},
        {"family": "linear", "regularization": 0.5, "scale": True, "threshold": 0.2},
        {"family": "extra_trees", "trees": 50, "max_depth": None, "feature_subset": ["x"]},
        {"family": "linear", "lags": [1, 7, 14]},
    ],
)
def test_candidate_choices_can_be_supplied_inline(
    tools: Any, configuration: dict[str, Any]
) -> None:
    """Scalars, nulls, and both list types reach candidate training without substitution."""
    _check(
        tools,
        "train_candidate",
        {"configuration": configuration, "hypothesis": "Compare a plausible predictive approach."},
    )
