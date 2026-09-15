"""Prediction pipelines, leakage prevention, frozen splits, and forecast horizons."""

from typing import Any

import joblib
import numpy as np
import pandas as pd
import pytest
from modeler.contracts import ModelerError, resolve
from modeler.learning import fit, measure, partition, validate_configuration


@pytest.mark.parametrize("split", ["random", "official", "temporal", "group"])
def test_splits_are_disjoint_and_reproducible(frame: pd.DataFrame, split: str) -> None:
    """No observation or grouping unit crosses its evaluation boundary."""
    spec = resolve(
        {
            "kind": "regression",
            "targets": ["value"],
            "features": ["x"],
            "time": "date",
            "group": "group",
            "split": split,
        },
        frame,
        {},
    )
    parts = partition(frame, spec)
    assert sorted(np.concatenate(parts).tolist()) == list(range(len(frame)))
    for first, second in zip(parts, partition(frame, spec), strict=True):
        np.testing.assert_array_equal(first, second)
    if split == "group":
        assert not set(frame.iloc[parts[0]]["group"]) & set(frame.iloc[parts[1]]["group"])
    if split == "temporal":
        assert frame.iloc[parts[0]]["date"].max() < frame.iloc[parts[1]]["date"].min()


@pytest.mark.parametrize("case", ["missing", "invalid", "small"])
def test_invalid_official_splits_fail(frame: pd.DataFrame, spec: dict[str, Any], case: str) -> None:
    """Official split labels must cover enough observations in each partition."""
    spec["split"] = "official"
    if case == "missing":
        frame = frame.drop(columns="_split")
    elif case == "invalid":
        frame.loc[0, "_split"] = "secret"
    else:
        frame["_split"] = "train"
    with pytest.raises(ModelerError):
        partition(frame, spec)


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("unknown", 1),
        ("family", "neural"),
        ("family", "seasonal"),
        ("trees", 0),
        ("trees", 1.5),
        ("trees", True),
        ("threshold", float("nan")),
        ("class_weight", "none"),
        ("strategy", "magic"),
        ("lags", []),
        ("lags", [0]),
        ("lags", list(range(1, 32))),
    ],
)
def test_candidate_choices_are_bounded(spec: dict[str, Any], option: str, value: Any) -> None:
    """Unsupported models and unbounded work are rejected before fitting."""
    with pytest.raises(ModelerError):
        validate_configuration({option: value}, spec)


@pytest.mark.parametrize(
    ("kind", "family", "strategy"),
    [
        ("binary", "baseline", "independent"),
        ("binary", "linear", "independent"),
        ("binary", "extra_trees", "independent"),
        ("multiclass", "linear", "independent"),
        ("regression", "baseline", "independent"),
        ("regression", "linear", "independent"),
        ("regression", "extra_trees", "independent"),
        ("multilabel", "linear", "independent"),
        ("multilabel", "extra_trees", "chain"),
    ],
)
def test_pipelines_train_and_reload(
    frame: pd.DataFrame, tmp_path: Any, kind: str, family: str, strategy: str
) -> None:
    """Saved preprocessing and estimator reproduce the complete prediction behavior."""
    targets = ["a", "b"] if kind == "multilabel" else ["value"] if kind == "regression" else ["y"]
    spec = resolve(
        {
            "kind": kind,
            "targets": targets,
            "features": ["x", "category", "text"],
            "text_features": ["text"],
        },
        frame,
        {},
    )
    config = validate_configuration({"family": family, "strategy": strategy, "trees": 3}, spec)
    model = fit(frame.iloc[:90], spec, config)
    result = measure(model, frame.iloc[:90], frame.iloc[90:120])
    assert all(value is not None for value in result.values())
    path = tmp_path / "model.joblib"
    joblib.dump(model, path)
    np.testing.assert_array_equal(
        model.predict(frame.iloc[120:]), joblib.load(path).predict(frame.iloc[120:])
    )


def test_binary_threshold_respects_explicit_positive_class(frame: pd.DataFrame) -> None:
    """The positive class need not be the lexicographically last class."""
    spec = resolve(
        {"kind": "binary", "targets": ["y"], "features": ["category"]},
        frame,
        {
            "objective": {
                "metric": "recall",
                "direction": "maximize",
                "parameters": {"positive_label": "0"},
            }
        },
    )
    model = fit(frame, spec, validate_configuration({"threshold": 0.1}, spec))
    assert set(model.predict(frame)) == {"0", "1"}
    assert list(measure(model, frame, frame).values()) == [1.0]


def test_preprocessing_uses_training_statistics_only(
    frame: pd.DataFrame, spec: dict[str, Any]
) -> None:
    """Unseen categories and extreme held-out numbers do not change preprocessing."""
    frame.loc[0, "x"] = np.nan
    config = validate_configuration({"scale": False}, spec)
    model = fit(frame.iloc[:90], spec, config)
    imputer = model.estimator["features"].named_transformers_["numeric"]["impute"]
    expected = imputer.statistics_.copy()
    changed = frame.iloc[90:].copy()
    changed["x"] = 1e9
    changed["category"] = "unseen"
    model.predict(changed)
    np.testing.assert_array_equal(expected, imputer.statistics_)
    assert expected.tolist() == [45.0]


def test_constant_multilabel_target_has_valid_probabilities(frame: pd.DataFrame) -> None:
    """A baseline with an always-absent label produces zeros for that label."""
    frame["b"] = 0
    spec = resolve({"kind": "multilabel", "targets": ["a", "b"], "features": ["x"]}, frame, {})
    model = fit(frame, spec, validate_configuration({"family": "baseline"}, spec))
    assert not model.predict(frame)[:, 1].any()


@pytest.mark.parametrize(
    ("family", "panel"),
    [("baseline", False), ("seasonal", False), ("linear", True), ("extra_trees", True)],
)
def test_forecasts_use_full_horizons_without_future_targets(
    frame: pd.DataFrame, family: str, panel: bool
) -> None:
    """Future observations affect measured error, never predictions at the same origin."""
    if panel:
        frame = pd.concat([frame.assign(series="a"), frame.assign(series="b")], ignore_index=True)
    spec = resolve(
        {
            "kind": "forecast",
            "targets": ["value"],
            "time": "date",
            "frequency": "D",
            "horizon": 10,
            "group": "series" if panel else None,
        },
        frame,
        {},
    )
    train, validation, test = partition(frame, spec)
    config = validate_configuration({"family": family, "trees": 3, "lags": [1, 7]}, spec)
    model = fit(frame.iloc[train], spec, config)
    before = model.predict(frame.iloc[train])
    assert len(before) == (20 if panel else 10)
    measured = measure(model, frame.iloc[train], frame.iloc[validation])
    assert measured["mae:{}"] >= 0
    frame.loc[test, "value"] = -100000
    pd.testing.assert_frame_equal(before, model.predict(frame.iloc[train]))
    final = measure(model, frame.iloc[np.concatenate([train, validation])], frame.iloc[test])
    assert final["mae:{}"] > 1000


def test_forecast_requires_sufficient_lag_history(frame: pd.DataFrame) -> None:
    """A lag window cannot exhaust the available training history."""
    spec = resolve(
        {"kind": "forecast", "targets": ["value"], "time": "date", "frequency": "D", "horizon": 10},
        frame,
        {},
    )
    config = validate_configuration({"lags": [149]}, spec)
    with pytest.raises(ModelerError, match="Insufficient training history"):
        fit(frame, spec, config)
    model = fit(frame, spec, validate_configuration({"family": "seasonal"}, spec))
    with pytest.raises(ModelerError, match="historical values"):
        model.predict(frame.head(2))


def test_undefined_forecast_scale_propagates_across_series(frame: pd.DataFrame) -> None:
    """A constant series cannot disappear from the aggregate MASE constraint."""
    frame["value"] = 1.0
    spec = resolve(
        {"kind": "forecast", "targets": ["value"], "time": "date", "frequency": "D", "horizon": 10},
        frame,
        {"objective": {"metric": "mase", "direction": "minimize"}},
    )
    train, validation, _ = partition(frame, spec)
    model = fit(frame.iloc[train], spec, validate_configuration({"family": "baseline"}, spec))
    assert measure(model, frame.iloc[train], frame.iloc[validation]) == {
        'mase:{"seasonal_period": 1}': None
    }


def test_classification_split_cannot_omit_a_training_class(
    frame: pd.DataFrame, spec: dict[str, Any]
) -> None:
    """An official split with a class only in held-out data cannot yield a usable classifier."""
    spec["split"] = "official"
    frame.loc[frame["_split"].eq("train"), "y"] = "0"
    with pytest.raises(ModelerError, match="training split"):
        partition(frame, spec)
