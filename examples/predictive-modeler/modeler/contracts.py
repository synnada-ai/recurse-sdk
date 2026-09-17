"""Resolve prediction semantics and validate task-specific quality requirements."""

import json
from typing import Any, cast

import numpy as np
import pandas as pd
from sklearn import metrics

__all__ = ["ModelerError", "feasible", "metric_key", "resolve", "score"]

_MIN_ROWS = 30
_BINARY_CLASSES = 2
_CLASSIFICATION = {"binary", "multiclass", "multilabel"}
_HIGHER = {"precision", "recall", "f1", "accuracy"}
_COMPLEXITY = {"model_bytes", "input_feature_count"}
_LOWER = {"mae", "rmse", "mase", "absolute_bias"}


class ModelerError(ValueError):
    """An actionable problem with a task, dataset, or experiment."""


def metric_key(metric: dict[str, Any]) -> str:
    """Give a metric and its options a stable identity in recorded measurements."""
    return str(metric["metric"]) + ":" + json.dumps(metric.get("parameters", {}), sort_keys=True)


def _validate_metric(metric: dict[str, Any], spec: dict[str, Any]) -> None:
    """Validate the metric registry entry and its task-specific parameters."""
    name = metric["metric"]
    classification = spec["kind"] in _CLASSIFICATION
    supported = (_HIGHER if classification else _LOWER) | _COMPLEXITY
    if name not in supported or (name == "mase" and spec["kind"] != "forecast"):
        raise ModelerError(f"Metric {name!r} is not supported for {spec['kind']}.")
    parameters = metric.setdefault("parameters", {})
    allowed = {"average", "positive_label", "label"} if classification else set()
    if name in _COMPLEXITY:
        allowed = set()
    if name == "mase":
        allowed = {"seasonal_period"}
    if set(parameters) - allowed:
        raise ModelerError(f"Unsupported parameters for {name}: {set(parameters) - allowed}")
    if name == "accuracy" and parameters:
        raise ModelerError(
            "accuracy has no parameters; multilabel accuracy means exact label match."
        )
    if name in _HIGHER - {"accuracy"}:
        _classification_options(parameters, spec)
    if name == "mase":
        period = parameters.setdefault("seasonal_period", 1)
        if type(period) is not int or period < 1:
            raise ModelerError("MASE seasonal_period must be a positive integer.")


def _classification_options(options: dict[str, Any], spec: dict[str, Any]) -> None:
    """Resolve averaging and label semantics without changing explicit choices."""
    kind = spec["kind"]
    average = options.setdefault("average", "binary" if kind == "binary" else "macro")
    if average not in {"binary", "micro", "macro", "weighted", "samples"}:
        raise ModelerError(f"Unsupported classification average: {average!r}")
    if average == "samples" and kind != "multilabel":
        raise ModelerError("samples averaging requires multilabel classification.")
    if kind == "binary":
        positive = str(options.setdefault("positive_label", spec["classes"][-1]))
        options["positive_label"] = positive
        if positive not in spec["classes"]:
            raise ModelerError(f"positive_label {positive!r} is absent from the dataset.")
    elif "positive_label" in options or average == "binary":
        raise ModelerError("positive_label and binary averaging require binary classification.")
    if "label" in options:
        labels = spec["targets"] if kind == "multilabel" else spec["classes"]
        if options["label"] not in labels:
            raise ModelerError(f"Unknown label {options['label']!r}.")


def resolve(
    specification: dict[str, Any], data: pd.DataFrame, quality: dict[str, Any]
) -> dict[str, Any]:
    """Validate a proposed task against data and preserve caller quality requirements.

    Args:
        specification: Kind, targets, features, optional text/group/time columns and horizon.
        data: Loaded observations, including optional official split annotations.
        quality: The caller's objective and constraints; defaults fill omissions only.

    Returns:
        A normalized, JSON-compatible evaluation contract.
    """
    spec = cast(dict[str, Any], json.loads(json.dumps(specification)))
    allowed = {
        "kind",
        "targets",
        "features",
        "text_features",
        "group",
        "time",
        "horizon",
        "frequency",
        "split",
        "seed",
    }
    if set(spec) - allowed:
        raise ModelerError(f"Unknown specification fields: {set(spec) - allowed}")
    kind = spec.get("kind")
    if kind not in _CLASSIFICATION | {"regression", "forecast"}:
        raise ModelerError(f"Unsupported problem kind: {kind!r}")
    targets = spec.get("targets", [])
    if not targets or len(set(targets)) != len(targets):
        raise ModelerError("Supply distinct target column names.")
    if kind != "multilabel" and len(targets) != 1:
        raise ModelerError("Only multilabel tasks accept multiple targets in this version.")
    features = spec.setdefault("features", [])
    texts = spec.setdefault("text_features", [])
    group = spec.setdefault("group", None)
    time = spec.setdefault("time", None)
    required = set(targets + features + texts) | {v for v in (group, time) if v}
    if required - set(data.columns):
        raise ModelerError(f"Missing dataset columns: {required - set(data.columns)}")
    if set(targets) & set(features) or not set(texts) <= set(features):
        raise ModelerError(
            "Targets cannot be features; text_features must be a subset of features."
        )
    if len(set(features)) != len(features) or "_split" in features:
        raise ModelerError("Feature names must be distinct and cannot include _split.")
    _validate_targets(spec, data)
    if kind == "forecast":
        _forecast_options(spec, data)
    elif not features:
        raise ModelerError("Select at least one feature for classification or regression.")
    spec.setdefault("seed", 42)
    spec.setdefault("split", "official" if "_split" in data else "random")
    if spec["split"] not in {"random", "group", "temporal", "official"}:
        raise ModelerError("split must be random, group, temporal, or official.")
    if spec["split"] == "group" and not group:
        raise ModelerError("A grouped split requires a group column.")
    if spec["split"] == "temporal" and (
        not time or pd.to_datetime(data[time], errors="coerce").isna().any()
    ):
        raise ModelerError(
            f"Invalid temporal time column {time!r}: "
            "supply a valid timestamp for every row; missing or invalid dates are not allowed."
        )
    spec["quality"] = _resolve_quality(quality, spec)
    spec["measurement_protocol"] = "predictive-modeler/v4"
    spec["aggregation"] = "uniform across series and forecast steps"
    return spec


def _forecast_options(spec: dict[str, Any], data: pd.DataFrame) -> None:
    """Require regular observed histories and a complete forecasting horizon."""
    if spec.get("split") == "official" or "_split" in data:
        raise ModelerError(
            "Forecasting does not support official partitions; supplied assignments cannot be "
            "discarded. Use a forecasting workflow that preserves those partitions."
        )
    if not spec["time"] or not spec.get("frequency"):
        raise ModelerError("Forecasting requires time, frequency (pandas offset), and horizon.")
    horizon = spec.get("horizon")
    if type(horizon) is not int or horizon < 1:
        raise ModelerError("Forecast horizon must be a positive integer.")
    if spec["features"]:
        raise ModelerError(
            "Forecast v1 uses history and calendar features; omit external features."
        )
    spec["split"] = "temporal"
    if spec["group"] and data[spec["group"]].isna().any():
        raise ModelerError(
            f"Series identifier column {spec['group']!r} contains missing values; "
            "supply an identifier for every row."
        )
    groups = data.groupby(spec["group"], sort=True) if spec["group"] else [("series", data)]
    for name, frame in groups:
        dates = pd.DatetimeIndex(pd.to_datetime(frame[spec["time"]])).sort_values()
        expected = pd.date_range(dates[0], periods=len(dates), freq=spec["frequency"])
        if not dates.equals(expected) or len(dates) < max(2 * horizon, 10 + horizon) + 3 * horizon:
            raise ModelerError(
                f"Series {name!r} must be regular and unique, with enough history for at least "
                "two refitted validation horizons and a final test horizon."
            )


def score(
    spec: dict[str, Any], truth: Any, prediction: Any, history: Any = None
) -> dict[str, float | None]:
    """Measure the frozen objective and constraints; undefined values cannot pass."""
    result: dict[str, float | None] = {}
    for metric in [spec["quality"]["objective"], *spec["quality"]["constraints"]]:
        name, options = metric["metric"], metric["parameters"]
        if name in _COMPLEXITY:
            continue
        if name in _HIGHER:
            value = _classification_score(name, options, spec, truth, prediction)
        else:
            value = _regression_score(name, options, truth, prediction, history)
        result[metric_key(metric)] = float(value) if np.isfinite(value) else None
    return result


def _classification_score(
    name: str, options: dict[str, Any], spec: dict[str, Any], truth: Any, prediction: Any
) -> float:
    """Compute classification metrics with fixed labels and zero-division behavior."""
    if name == "accuracy":
        return float(metrics.accuracy_score(truth, prediction))
    arguments: dict[str, Any] = {"average": options["average"], "zero_division": 0}
    if "label" in options:
        if spec["kind"] == "multilabel":
            index = spec["targets"].index(options["label"])
            truth, prediction = truth[:, index], prediction[:, index]
            arguments["average"] = "binary"
        else:
            arguments.update(average="macro", labels=[options["label"]])
    elif spec["kind"] != "multilabel":
        arguments["labels"] = spec["classes"]
    if spec["kind"] == "binary":
        arguments["pos_label"] = options["positive_label"]
    function = {
        "precision": metrics.precision_score,
        "recall": metrics.recall_score,
        "f1": metrics.f1_score,
    }[name]
    return float(function(truth, prediction, **arguments))


def _regression_score(
    name: str, options: dict[str, Any], truth: Any, prediction: Any, history: Any
) -> float:
    """Compute error metrics using historical observations for MASE scaling."""
    error = np.asarray(prediction) - np.asarray(truth)
    if name == "rmse":
        return float(np.sqrt(np.mean(error**2)))
    if name == "absolute_bias":
        return float(abs(np.mean(error)))
    mae = float(np.mean(abs(error)))
    if name != "mase":
        return mae
    period = options["seasonal_period"]
    history = np.asarray(history)
    if len(history) <= period:
        return float("nan")
    scale = float(np.mean(abs(history[period:] - history[:-period])))
    return mae / scale if scale > 0 else float("nan")


def feasible(spec: dict[str, Any], measurements: dict[str, float | None]) -> bool:
    """Require a defined objective and every hard constraint to pass."""
    if any(value is None for value in measurements.values()):
        return False
    for constraint in spec["quality"]["constraints"]:
        value = measurements[metric_key(constraint)]
        if constraint["operator"] == ">=" and value < constraint["value"]:
            return False
        if constraint["operator"] == "<=" and value > constraint["value"]:
            return False
    return True


def _validate_targets(spec: dict[str, Any], data: pd.DataFrame) -> None:
    """Check target values before training or choosing split membership."""
    targets, kind = spec["targets"], spec["kind"]
    if data[targets].isna().any().any() or len(data) < _MIN_ROWS:
        raise ModelerError("Need at least 30 rows and no missing targets.")
    if kind in {"binary", "multiclass"}:
        classes = sorted(data[targets[0]].astype(str).unique().tolist())
        if len(classes) < _BINARY_CLASSES or (kind == "binary" and len(classes) != _BINARY_CLASSES):
            raise ModelerError(
                "Target class count does not match the proposed classification kind."
            )
        spec["classes"] = classes
    elif kind == "multilabel":
        if not data[targets].isin([0, 1]).all().all():
            raise ModelerError("Multilabel targets must be separate binary 0/1 columns.")
    elif not np.isfinite(data[targets].to_numpy(dtype=float)).all():
        raise ModelerError("Regression and forecast targets must be finite numbers.")


def _resolve_quality(quality: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Fill missing objective defaults without relaxing explicit requirements."""
    kind = spec["kind"]
    resolved_quality = json.loads(json.dumps(quality))
    default = (
        {"metric": "f1", "direction": "maximize"}
        if kind in _CLASSIFICATION
        else {"metric": "mae", "direction": "minimize"}
    )
    objective = resolved_quality.setdefault("objective", default)
    constraints = resolved_quality.setdefault("constraints", [])
    for metric in [objective, *constraints]:
        _validate_metric(metric, spec)
    expected = "maximize" if objective["metric"] in _HIGHER else "minimize"
    if objective["direction"] != expected:
        raise ModelerError(f"{objective['metric']} must use direction {expected!r}.")
    for constraint in constraints:
        if constraint["operator"] not in {">=", "<="} or not np.isfinite(constraint["value"]):
            raise ModelerError("Constraints require >= or <= and a finite numeric value.")
    return cast(dict[str, Any], resolved_quality)
