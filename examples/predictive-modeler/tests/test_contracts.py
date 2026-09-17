"""Specifications for task resolution, metric semantics, and hard constraints."""

from typing import Any

import numpy as np
import pandas as pd
import pytest
from modeler.contracts import ModelerError, feasible, resolve, score


def test_resolution_preserves_inputs_and_canonicalizes_labels(frame: pd.DataFrame) -> None:
    """Numeric positive labels normalize without mutating caller quality."""
    quality = {
        "objective": {
            "metric": "recall",
            "direction": "maximize",
            "parameters": {"positive_label": 1},
        }
    }
    result = resolve({"kind": "binary", "targets": ["y"], "features": ["x"]}, frame, quality)
    assert quality["objective"]["parameters"] == {"positive_label": 1}
    assert result["quality"] == {
        "objective": {
            "metric": "recall",
            "direction": "maximize",
            "parameters": {"positive_label": "1", "average": "binary"},
        },
        "constraints": [],
    }
    assert result["split"] == "official"


@pytest.mark.parametrize(
    ("kind", "target"), [("multiclass", "y"), ("multilabel", "a"), ("regression", "value")]
)
def test_supported_target_families_resolve(frame: pd.DataFrame, kind: str, target: str) -> None:
    """Target semantics determine objective defaults."""
    result = resolve({"kind": kind, "targets": [target], "features": ["x"]}, frame, {})
    assert result["quality"]["objective"]["metric"] == ("mae" if kind == "regression" else "f1")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"unknown": 1}, "Unknown specification"),
        ({"kind": "clustering"}, "Unsupported problem"),
        ({"targets": []}, "distinct target"),
        ({"targets": ["a", "b"]}, "Only multilabel"),
        ({"targets": ["absent"]}, "Missing dataset"),
        ({"features": ["y"]}, "Targets cannot"),
        ({"text_features": ["text"]}, "text_features"),
        ({"features": ["x", "x"]}, "distinct"),
        ({"features": ["_split"]}, "_split"),
        ({"features": []}, "at least one feature"),
        ({"split": "bad"}, "split must"),
        ({"split": "group"}, "group column"),
        ({"split": "temporal"}, "time column"),
    ],
)
def test_invalid_specifications_fail_before_training(
    frame: pd.DataFrame, change: dict[str, Any], message: str
) -> None:
    """Malformed or leaking target/feature choices cannot create a contract."""
    with pytest.raises(ModelerError, match=message):
        resolve({"kind": "binary", "targets": ["y"], "features": ["x"]} | change, frame, {})


@pytest.mark.parametrize(
    "case", ["few", "missing", "constant", "multiclass", "multilabel", "infinite"]
)
def test_invalid_target_values_fail(frame: pd.DataFrame, case: str) -> None:
    """Invalid labels and insufficient observations are rejected."""
    kind, target = "binary", "y"
    if case == "few":
        frame = frame.head(10)
    elif case == "missing":
        frame.loc[0, "y"] = None
    elif case == "constant":
        frame["y"] = "same"
    elif case == "multiclass":
        frame.loc[0, "y"] = "third"
    elif case == "multilabel":
        kind, target = "multilabel", "x"
    else:
        kind, target = "regression", "value"
        frame.loc[0, "value"] = np.inf
    with pytest.raises(ModelerError):
        resolve({"kind": kind, "targets": [target], "features": ["text"]}, frame, {})


@pytest.mark.parametrize(
    ("kind", "metric", "parameters", "direction"),
    [
        ("binary", "mae", {}, "minimize"),
        ("regression", "mase", {}, "minimize"),
        ("binary", "precision", {"bogus": 1}, "maximize"),
        ("binary", "accuracy", {"average": "macro"}, "maximize"),
        ("binary", "f1", {"average": "bogus"}, "maximize"),
        ("binary", "f1", {"average": "samples"}, "maximize"),
        ("binary", "f1", {"positive_label": "absent"}, "maximize"),
        ("multiclass", "f1", {"average": "binary"}, "maximize"),
        ("multiclass", "f1", {"positive_label": "1"}, "maximize"),
        ("binary", "f1", {"label": "absent"}, "maximize"),
        ("binary", "f1", {}, "minimize"),
    ],
)
def test_incompatible_metric_options_fail(
    frame: pd.DataFrame, kind: str, metric: str, parameters: dict[str, Any], direction: str
) -> None:
    """A metric must have meaningful semantics for the resolved task."""
    with pytest.raises(ModelerError):
        resolve(
            {"kind": kind, "targets": ["y"], "features": ["x"]},
            frame,
            {"objective": {"metric": metric, "direction": direction, "parameters": parameters}},
        )


@pytest.mark.parametrize(("operator", "value"), [("==", 1), (">=", float("inf"))])
def test_constraints_require_supported_comparisons(
    frame: pd.DataFrame, operator: str, value: float
) -> None:
    """Constraint operators and thresholds are checked independently of the caller schema."""
    with pytest.raises(ModelerError, match="Constraints require"):
        resolve(
            {"kind": "binary", "targets": ["y"], "features": ["x"]},
            frame,
            {"constraints": [{"metric": "recall", "operator": operator, "value": value}]},
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"time": None}, "requires time"),
        ({"frequency": None}, "requires time"),
        ({"horizon": 0}, "positive integer"),
        ({"features": ["x"]}, "omit external"),
        ({"horizon": 50}, "enough history"),
        ({"frequency": "2D"}, "regular"),
    ],
)
def test_forecast_requires_usable_time_semantics(
    frame: pd.DataFrame, change: dict[str, Any], message: str
) -> None:
    """A forecasting task has an explicit horizon, frequency, and causal feature boundary."""
    with pytest.raises(ModelerError, match=message):
        resolve(
            {
                "kind": "forecast",
                "targets": ["value"],
                "time": "date",
                "frequency": "D",
                "horizon": 10,
            }
            | change,
            frame,
            {},
        )


@pytest.mark.parametrize("period", [0, 1.5, True])
def test_mase_requires_positive_integer_season(frame: pd.DataFrame, period: Any) -> None:
    """MASE's scale period is validated before evaluation."""
    with pytest.raises(ModelerError, match="seasonal_period"):
        resolve(
            {
                "kind": "forecast",
                "targets": ["value"],
                "time": "date",
                "frequency": "D",
                "horizon": 10,
            },
            frame,
            {
                "objective": {
                    "metric": "mase",
                    "direction": "minimize",
                    "parameters": {"seasonal_period": period},
                }
            },
        )


@pytest.mark.parametrize(
    ("kind", "parameters"),
    [
        ("binary", {}),
        ("multiclass", {"average": "micro"}),
        ("multiclass", {"label": "1"}),
        ("multilabel", {"average": "samples"}),
        ("multilabel", {"label": "a"}),
    ],
)
def test_classification_metrics_score_actual_predictions(
    frame: pd.DataFrame, kind: str, parameters: dict[str, Any]
) -> None:
    """Each classification variant scores its declared prediction target."""
    targets = ["a", "b"] if kind == "multilabel" else ["y"]
    quality = {
        "objective": {"metric": "f1", "direction": "maximize", "parameters": parameters},
        "constraints": [
            {"metric": "recall", "parameters": parameters, "operator": ">=", "value": 0.8},
            {"metric": "precision", "parameters": parameters, "operator": ">=", "value": 0.8},
            {"metric": "accuracy", "operator": ">=", "value": 0.8},
        ],
    }
    spec = resolve({"kind": kind, "targets": targets, "features": ["x"]}, frame, quality)
    truth = frame[targets].to_numpy() if kind == "multilabel" else frame["y"].to_numpy()
    result = score(spec, truth, truth)
    expected = 112 / 150 if parameters.get("average") == "samples" else 1.0
    assert result[next(iter(result))] == pytest.approx(expected)
    assert feasible(spec, result) is (expected >= 0.8)


@pytest.mark.parametrize(
    ("metric", "expected"),
    [("mae", 1.5), ("rmse", np.sqrt(2.5)), ("absolute_bias", 0.5), ("mase", 0.75)],
)
def test_regression_metrics_have_explicit_units(
    frame: pd.DataFrame, metric: str, expected: float
) -> None:
    """Error metrics are calculated from predictions and training-only scales."""
    spec = resolve(
        {"kind": "forecast", "targets": ["value"], "time": "date", "frequency": "D", "horizon": 10},
        frame,
        {"objective": {"metric": metric, "direction": "minimize"}},
    )
    result = score(spec, [1, 2], [3, 1], [0, 2, 4])
    assert list(result.values()) == pytest.approx([expected])


@pytest.mark.parametrize("history", [[1], [1, 1, 1]])
def test_undefined_mase_is_not_feasible(frame: pd.DataFrame, history: list[int]) -> None:
    """Insufficient or constant scale history cannot manufacture a valid score."""
    spec = resolve(
        {"kind": "forecast", "targets": ["value"], "time": "date", "frequency": "D", "horizon": 10},
        frame,
        {"objective": {"metric": "mase", "direction": "minimize"}},
    )
    result = score(spec, [1, 2], [1, 2], history)
    assert result == {'mase:{"seasonal_period": 1}': None}
    assert not feasible(spec, result)


@pytest.mark.parametrize(
    ("operator", "threshold", "passed"),
    [(">=", 0.5, True), (">=", 0.9, False), ("<=", 0.9, True), ("<=", 0.5, False)],
)
def test_feasibility_requires_each_constraint(
    frame: pd.DataFrame, operator: str, threshold: float, passed: bool
) -> None:
    """An objective score cannot compensate for violating a hard constraint."""
    spec = resolve(
        {"kind": "regression", "targets": ["value"], "features": ["x"]},
        frame,
        {"constraints": [{"metric": "mae", "operator": operator, "value": threshold}]},
    )
    assert feasible(spec, {"mae:{}": 0.7}) is passed


@pytest.mark.parametrize("metric", ["model_bytes", "input_feature_count"])
def test_complexity_rejects_classification_options(frame: pd.DataFrame, metric: str) -> None:
    """Complexity is independent of label averaging and has no tunable measurement options."""
    with pytest.raises(ModelerError, match="Unsupported parameters"):
        resolve(
            {"kind": "binary", "targets": ["y"], "features": ["x"]},
            frame,
            {
                "objective": {
                    "metric": metric,
                    "direction": "minimize",
                    "parameters": {"average": "macro"},
                }
            },
        )


@pytest.mark.parametrize("missing", ["one", "series", "all"])
def test_forecast_rejects_missing_series_identifiers(frame: pd.DataFrame, missing: str) -> None:
    """Resolution cannot silently exclude rows whose forecasting series is unidentified."""
    data = pd.concat([frame.assign(series="a"), frame.assign(series="b")], ignore_index=True)
    rows = (
        data.index[-1:]
        if missing == "one"
        else data.index[150:]
        if missing == "series"
        else data.index
    )
    data.loc[rows, "series"] = None
    with pytest.raises(ModelerError, match=r"series.*missing"):
        resolve(
            {
                "kind": "forecast",
                "targets": ["value"],
                "time": "date",
                "group": "series",
                "frequency": "D",
                "horizon": 10,
            },
            data,
            {},
        )
