"""Prediction pipelines, leakage prevention, frozen splits, and forecast horizons."""

from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import pytest
from modeler import learning
from modeler.contracts import ModelerError, resolve
from modeler.learning import cross_validate, fit, measure, partition, validate_configuration
from sklearn.ensemble import ExtraTreesClassifier


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
    plan = partition(frame, spec)
    assert plan == partition(frame, spec)
    assert set(plan["fit"]).isdisjoint(plan["test"])
    assert sorted(set(plan["fit"] + plan["test"] + plan["folds"][0]["validation"])) == list(
        range(len(frame))
    )
    for fold in plan["folds"]:
        assert set(fold["train"]).isdisjoint(fold["validation"])
        assert set(fold["train"] + fold["validation"]).isdisjoint(plan["test"])
        if split == "group":
            assert not set(frame.iloc[fold["train"]]["group"]) & set(
                frame.iloc[fold["validation"]]["group"]
            )
            assert not set(frame.iloc[plan["fit"]]["group"]) & set(
                frame.iloc[plan["test"]]["group"]
            )
        if split == "temporal":
            assert (
                frame.iloc[fold["train"]]["date"].max()
                < frame.iloc[fold["validation"]]["date"].min()
            )
            assert (
                frame.iloc[fold["validation"]]["date"].max()
                < frame.iloc[plan["test"]]["date"].min()
            )


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
    config = validate_configuration(
        {
            "family": family,
            "strategy": strategy,
            **({"trees": 3} if family == "extra_trees" else {}),
        },
        spec,
    )
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
    plan = partition(frame, spec)
    train, validation, test = (
        plan["folds"][0]["train"],
        plan["folds"][0]["validation"],
        plan["test"],
    )
    config = validate_configuration(
        {
            "family": family,
            **({"trees": 3} if family == "extra_trees" else {}),
            **({"lags": [1, 7]} if family in {"linear", "extra_trees"} else {}),
        },
        spec,
    )
    model = fit(frame.iloc[train], spec, config)
    before = model.predict(frame.iloc[train])
    assert len(before) == (20 if panel else 10)
    measured = measure(model, frame.iloc[train], frame.iloc[validation])
    assert measured["mae:{}"] >= 0
    frame.loc[test, "value"] = -100000
    pd.testing.assert_frame_equal(before, model.predict(frame.iloc[train]))
    final = measure(model, frame.iloc[plan["fit"]], frame.iloc[test])
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
    plan = partition(frame, spec)
    train, validation = plan["folds"][0]["train"], plan["folds"][0]["validation"]
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


def test_candidate_feature_selection_stays_within_frozen_eligible_columns(
    frame: pd.DataFrame,
) -> None:
    """The agent can test feature subsets without admitting target-derived or undeclared columns."""
    spec = resolve(
        {"kind": "binary", "targets": ["y"], "features": ["x", "text"], "text_features": ["text"]},
        frame,
        {},
    )
    config = validate_configuration({"feature_subset": ["text"]}, spec)
    model = fit(frame.iloc[:90], spec, config)
    assert model.specification["features"] == ["text"]
    assert spec["features"] == ["x", "text"]
    assert model.predict(frame[["text"]]).tolist() == frame["y"].tolist()
    for subset in [[], ["y"], ["x", "x"], "text"]:
        with pytest.raises(ModelerError, match="feature_subset"):
            validate_configuration({"feature_subset": subset}, spec)


@pytest.mark.parametrize("kind", ["binary", "multiclass", "multilabel", "regression", "forecast"])
def test_complexity_covers_complete_saved_predictor(
    frame: pd.DataFrame, tmp_path: Path, kind: str
) -> None:
    """Complexity works across families and counts raw inputs rather than transformed columns."""
    if kind == "forecast":
        frame = pd.concat([frame.assign(series="a"), frame.assign(series="b")], ignore_index=True)
        proposal = {
            "kind": kind,
            "targets": ["value"],
            "time": "date",
            "frequency": "D",
            "horizon": 10,
            "group": "series",
        }
    else:
        proposal = {
            "kind": kind,
            "targets": ["a", "b"]
            if kind == "multilabel"
            else ["value"]
            if kind == "regression"
            else ["y"],
            "features": ["x", "category"],
        }
    quality = {
        "objective": {"metric": "model_bytes", "direction": "minimize"},
        "constraints": [{"metric": "input_feature_count", "operator": "<=", "value": 3}],
    }
    spec = resolve(proposal, frame, quality)
    config = validate_configuration({"family": "linear"}, spec)
    plan = partition(frame, spec)
    train, validation = plan["folds"][0]["train"], plan["folds"][0]["validation"]
    model = fit(frame.iloc[train], spec, config)
    path = tmp_path / "model.joblib"
    joblib.dump(model, path, compress=0, protocol=5)
    expected = {
        "model_bytes:{}": float(path.stat().st_size),
        "input_feature_count:{}": 3.0 if kind == "forecast" else 2.0,
    }
    assert measure(model, frame.iloc[train], frame.iloc[validation], path) == expected
    assert measure(model, frame.iloc[train], frame.iloc[validation]) == expected
    restored = joblib.load(path)
    assert measure(restored, frame.iloc[train], frame.iloc[validation], path) == expected


def test_feature_subset_and_single_forecast_input_counts(frame: pd.DataFrame) -> None:
    """Selected raw columns count once; a single forecast requires history and time."""
    quality = {"objective": {"metric": "input_feature_count", "direction": "minimize"}}
    cases: list[tuple[dict[str, Any], dict[str, Any], float]] = [
        (
            {"kind": "binary", "targets": ["y"], "features": ["x", "category"]},
            {"feature_subset": ["category"]},
            1.0,
        ),
        (
            {
                "kind": "forecast",
                "targets": ["value"],
                "time": "date",
                "frequency": "D",
                "horizon": 10,
            },
            {"family": "seasonal"},
            2.0,
        ),
    ]
    for proposal, configuration, expected in cases:
        spec = resolve(proposal, frame, quality)
        plan = partition(frame, spec)
        train, validation = plan["folds"][0]["train"], plan["folds"][0]["validation"]
        model = fit(frame.iloc[train], spec, validate_configuration(configuration, spec))
        assert measure(model, frame.iloc[train], frame.iloc[validation]) == {
            "input_feature_count:{}": expected
        }


def test_complexity_objective_preserves_constraint_positive_class(frame: pd.DataFrame) -> None:
    """A recall constraint determines binary threshold orientation when size is optimized."""
    spec = resolve(
        {"kind": "binary", "targets": ["y"], "features": ["category"]},
        frame,
        {
            "objective": {"metric": "model_bytes", "direction": "minimize"},
            "constraints": [
                {
                    "metric": "recall",
                    "parameters": {"positive_label": "0"},
                    "operator": ">=",
                    "value": 1.0,
                }
            ],
        },
    )
    model = fit(frame, spec, validate_configuration({"family": "baseline", "threshold": 0.1}, spec))
    assert set(model.predict(frame)) == {"0"}
    assert (
        measure(model, frame, frame)['recall:{"average": "binary", "positive_label": "0"}'] == 1.0
    )


@pytest.mark.parametrize("date_format", ["%Y-%m-%d", "%b %d %Y"])
@pytest.mark.parametrize("panel", [False, True])
@pytest.mark.parametrize("family", ["baseline", "seasonal", "linear", "extra_trees"])
def test_forecast_chronology_is_independent_of_date_format_and_row_order(
    panel: bool, family: str, date_format: str
) -> None:
    """Equivalent dates preserve chronological splits, fitted forecasts, and rolling MASE."""
    dates = pd.date_range("2000-01-01", periods=60, freq="MS")
    frame = pd.DataFrame(
        {"date": dates, "value": np.arange(60) ** 1.5 + np.sin(np.arange(60)), "series": "a"}
    )
    if panel:
        frame = pd.concat(
            [frame, frame.assign(series="b", value=frame["value"] * 2)], ignore_index=True
        )
    proposal = {
        "kind": "forecast",
        "targets": ["value"],
        "time": "date",
        "frequency": "MS",
        "horizon": 12,
        "group": "series" if panel else None,
    }
    quality = {
        "objective": {
            "metric": "mase",
            "direction": "minimize",
            "parameters": {"seasonal_period": 12},
        }
    }
    spec = resolve(proposal, frame, quality)
    plan = partition(frame, spec)
    config = validate_configuration(
        {
            "family": family,
            **({"trees": 3} if family == "extra_trees" else {}),
            **({"lags": [1, 12]} if family in {"linear", "extra_trees"} else {}),
            **({"seasonal_period": 12} if family == "seasonal" else {}),
        },
        spec,
    )
    expected_model = fit(frame.iloc[plan["fit"]], spec, config)
    expected_predictions = expected_model.predict(frame.iloc[plan["fit"]])
    expected_scores = cross_validate(frame, spec, config, plan)
    expected_final = measure(expected_model, frame.iloc[plan["fit"]], frame.iloc[plan["test"]])
    formatted = frame.assign(date=frame["date"].dt.strftime(date_format))
    shuffled = formatted.sample(frac=1, random_state=23).reset_index(drop=True)
    actual_spec = resolve(proposal, shuffled, quality)
    actual_plan = partition(shuffled, actual_spec)
    expected_rows = [
        plan["fit"],
        plan["test"],
        *[fold[key] for fold in plan["folds"] for key in ["train", "validation"]],
    ]
    actual_rows = [
        actual_plan["fit"],
        actual_plan["test"],
        *[fold[key] for fold in actual_plan["folds"] for key in ["train", "validation"]],
    ]
    for reference, actual in zip(expected_rows, actual_rows, strict=True):
        expected = frame.iloc[reference].reset_index(drop=True)
        observed = shuffled.iloc[actual].reset_index(drop=True)
        observed["date"] = pd.to_datetime(observed["date"])
        pd.testing.assert_frame_equal(expected, observed)
    history = shuffled.iloc[actual_plan["fit"]].sample(frac=1, random_state=11)
    model = fit(history, actual_spec, config)
    pd.testing.assert_frame_equal(expected_predictions, model.predict(history))
    assert cross_validate(shuffled, actual_spec, config, actual_plan) == expected_scores
    assert measure(model, history, shuffled.iloc[actual_plan["test"]]) == pytest.approx(
        expected_final
    )


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("scale", "false", "scale must be a boolean"),
        ("scale", "standard", "scale must be a boolean"),
        ("scale", 0, "scale must be a boolean"),
        ("scale", None, "scale must be a boolean"),
        ("scale", [], "scale must be a boolean"),
        ("family", [], "family must be"),
        ("family", {}, "family must be"),
        ("class_weight", [], "class_weight must be"),
        ("class_weight", {}, "class_weight must be"),
        ("strategy", [], "strategy must be"),
        ("strategy", {}, "strategy must be"),
        ("feature_subset", [["x"]], "feature_subset must be"),
        ("feature_subset", [{}], "feature_subset must be"),
        ("lags", 7, "Supply a list"),
        ("lags", True, "Supply a list"),
        ("lags", {1: "invalid"}, "Supply a list"),
        ("lags", (1, 7), "Supply a list"),
    ],
)
def test_malformed_candidate_values_raise_actionable_domain_errors(
    spec: dict[str, Any], option: str, value: Any, message: str
) -> None:
    """Malformed proposal values fail before fitting instead of coercion or generic errors."""
    with pytest.raises(ModelerError, match=message):
        validate_configuration({option: value}, spec)


@pytest.mark.parametrize("kind", ["binary", "multiclass"])
@pytest.mark.parametrize("labels", ["integers", "numeric_strings", "names"])
def test_balanced_trees_preserve_class_labels_and_inverse_frequency_weights(
    kind: str, labels: str
) -> None:
    """Class balancing matches integer-encoded training for every supported label spelling."""
    target = np.repeat(
        np.arange(2 if kind == "binary" else 3), [60, 30] if kind == "binary" else [60, 30, 15]
    )
    values = np.arange(len(target))
    frame = pd.DataFrame({"x": np.sin(values), "z": np.cos(values / 7), "target": target})
    if labels != "integers":
        frame["target"] = frame["target"].astype(str)
        if labels == "names":
            frame["target"] = "class-" + frame["target"]
    spec = resolve({"kind": kind, "targets": ["target"], "features": ["x", "z"]}, frame, {})
    config = validate_configuration(
        {"family": "extra_trees", "trees": 3, "class_weight": "balanced"}, spec
    )
    model = fit(frame, spec, config)
    classes, encoded = np.unique(frame["target"].astype(str), return_inverse=True)
    expected = ExtraTreesClassifier(
        n_estimators=3,
        max_depth=12,
        min_samples_leaf=2,
        class_weight="balanced",
        random_state=42,
        n_jobs=1,
    ).fit(model.estimator["features"].transform(frame), encoded)
    np.testing.assert_array_equal(model.estimator.classes_, classes)
    np.testing.assert_allclose(
        model.estimator.predict_proba(frame),
        expected.predict_proba(model.estimator["features"].transform(frame)),
    )
    assert model.configuration["class_weight"] == "balanced"


def test_stratified_folds_reduce_to_available_class_count(frame: pd.DataFrame) -> None:
    """Rare classes remain represented in each fit while reducing the default fold count."""
    frame["y"] = ["rare"] * 5 + ["common"] * 145
    spec = resolve(
        {"kind": "binary", "targets": ["y"], "features": ["x"], "split": "random"}, frame, {}
    )
    plan = partition(frame, spec)
    assert plan["method"] == "stratified_kfold"
    assert len(plan["folds"]) == 4
    assert len(plan["fit"]) == 120
    assert len(plan["test"]) == 30
    for fold in plan["folds"]:
        assert set(frame.iloc[fold["train"]]["y"]) == {"rare", "common"}
        assert set(frame.iloc[fold["validation"]]["y"]) == {"rare", "common"}


@pytest.mark.parametrize(
    "case", ["rare_class", "one_group", "few_timestamps", "small_fold", "short_forecast"]
)
def test_cross_validation_rejects_insufficient_data(frame: pd.DataFrame, case: str) -> None:
    """Unusable resampling plans produce actionable errors before any fitting."""
    proposal: dict[str, Any] = {
        "kind": "regression",
        "targets": ["value"],
        "features": ["x"],
        "split": "random",
    }
    if case == "rare_class":
        frame["y"] = ["rare"] + ["common"] * 149
        proposal.update(kind="binary", targets=["y"])
    elif case == "one_group":
        frame["group"] = 1
        proposal.update(split="group", group="group")
    elif case == "few_timestamps":
        frame["date"] = pd.Timestamp("2020-01-01")
        proposal.update(split="temporal", time="date")
    elif case == "small_fold":
        frame = frame.head(30)
        frame["date"] = pd.date_range("2020-01-01", periods=30)
        proposal.update(split="temporal", time="date")
    spec = resolve(proposal, frame, {})
    if case == "short_forecast":
        spec.update(kind="forecast", horizon=40, time="date", split="temporal")
    with pytest.raises(ModelerError, match=r"cross-validation|Cross-validation"):
        partition(frame, spec)


def test_temporal_folds_preserve_equal_time_groups_and_reduce_when_needed(
    frame: pd.DataFrame,
) -> None:
    """Calendar boundaries never divide a timestamp, and small histories use fewer folds."""
    for data, count in [(frame.head(60).copy(), 4), (frame.copy(), 5)]:
        if len(data) == 150:
            data["date"] = np.repeat(pd.date_range("2020-01-01", periods=50), 3)
        spec = resolve(
            {
                "kind": "regression",
                "targets": ["value"],
                "features": ["x"],
                "split": "temporal",
                "time": "date",
            },
            data,
            {},
        )
        plan = partition(data, spec)
        assert len(plan["folds"]) == count
        for fold in plan["folds"]:
            assert (
                data.iloc[fold["train"]]["date"].max() < data.iloc[fold["validation"]]["date"].min()
            )
            assert (
                data.iloc[fold["validation"]]["date"].max() < data.iloc[plan["test"]]["date"].min()
            )


def test_forecast_origins_expand_before_an_untouched_short_final_horizon(
    frame: pd.DataFrame,
) -> None:
    """Short horizons are valid even though tabular final tests require ten rows."""
    spec = resolve(
        {"kind": "forecast", "targets": ["value"], "time": "date", "frequency": "D", "horizon": 1},
        frame,
        {},
    )
    plan = partition(frame, spec)
    assert plan["method"] == "rolling_origin"
    assert plan["fit"] == list(range(149))
    assert plan["test"] == [149]
    assert plan["folds"] == [
        {"train": list(range(end)), "validation": [end]} for end in [146, 147, 148]
    ]
    model = fit(frame.iloc[plan["fit"]], spec, validate_configuration({"family": "baseline"}, spec))
    assert (
        model.predict(frame.iloc[[148]])["prediction"].tolist()
        == frame.iloc[[148]]["value"].tolist()
    )


def test_cross_validation_refits_preprocessing_and_never_uses_final_test(
    frame: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every fold learns its own imputation statistics; held-out changes cannot alter scores."""
    spec = resolve(
        {"kind": "regression", "targets": ["value"], "features": ["x"], "split": "random"},
        frame,
        {"constraints": [{"metric": "model_bytes", "operator": "<=", "value": 100000}]},
    )
    plan = partition(frame, spec)
    config = validate_configuration({}, spec)
    trained = []

    def observed_fit(
        data: pd.DataFrame, specification: dict[str, Any], configuration: dict[str, Any]
    ) -> Any:
        """Observe statistics learned independently by each fold."""
        model = fit(data, specification, configuration)
        trained.append(
            model.estimator["features"]
            .named_transformers_["numeric"]["impute"]
            .statistics_.tolist()
        )
        return model

    monkeypatch.setattr(learning, "fit", observed_fit)
    result = cross_validate(frame, spec, config, plan)
    assert trained == [[frame.iloc[fold["train"]]["x"].median()] for fold in plan["folds"]]
    assert list(result["scores"]) == ["mae:{}"]
    assert result["scores"]["mae:{}"] == np.mean(
        [fold["scores"]["mae:{}"] for fold in result["folds"]]
    )
    assert [(item["train_rows"], item["validation_rows"]) for item in result["folds"]] == [
        (96, 24)
    ] * 5
    changed = frame.copy()
    changed.loc[plan["test"], ["x", "value"]] = 1e9
    assert cross_validate(changed, spec, config, plan) == result


def test_complexity_only_cv_skips_fitting(
    frame: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Artifact complexity is evaluated on the deployment model rather than averaged fold models."""
    spec = resolve(
        {"kind": "regression", "targets": ["value"], "features": ["x"], "split": "random"},
        frame,
        {"objective": {"metric": "model_bytes", "direction": "minimize"}},
    )
    monkeypatch.setattr(
        learning, "fit", lambda *args: pytest.fail("No predictive metrics require fold fitting.")
    )
    assert cross_validate(
        frame, spec, validate_configuration({}, spec), partition(frame, spec)
    ) == {"scores": {}, "folds": []}


def test_undefined_score_in_one_fold_remains_infeasible(frame: pd.DataFrame) -> None:
    """A constant early history cannot disappear when later MASE folds are defined."""
    frame.loc[:109, "value"] = 1
    spec = resolve(
        {"kind": "forecast", "targets": ["value"], "time": "date", "frequency": "D", "horizon": 10},
        frame,
        {"objective": {"metric": "mase", "direction": "minimize"}},
    )
    result = cross_validate(
        frame, spec, validate_configuration({"family": "baseline"}, spec), partition(frame, spec)
    )
    assert result["scores"] == {'mase:{"seasonal_period": 1}': None}
    assert result["folds"][0]["scores"] == result["scores"]
    assert result["folds"][1]["scores"]['mase:{"seasonal_period": 1}'] is not None


@pytest.mark.parametrize(
    ("kind", "family", "option", "value"),
    [
        ("forecast", "seasonal", "lags", [1]),
        ("forecast", "baseline", "seasonal_period", 12),
        ("forecast", "linear", "scale", False),
        ("forecast", "linear", "seasonal_period", 12),
        ("regression", "baseline", "regularization", 2),
        ("regression", "linear", "trees", 3),
        ("regression", "extra_trees", "class_weight", "balanced"),
        ("regression", "linear", "lags", [1]),
        ("multiclass", "linear", "threshold", 0.2),
        ("binary", "linear", "strategy", "chain"),
        ("binary", "linear", "ngram_max", 2),
    ],
)
def test_inapplicable_tuning_is_rejected_before_training(
    frame: pd.DataFrame, kind: str, family: str, option: str, value: Any
) -> None:
    """Ignored controls cannot consume trials or support false causal explanations."""
    proposal = {
        "kind": kind,
        "targets": ["value"] if kind in {"regression", "forecast"} else ["y"],
        "features": [] if kind == "forecast" else ["x"],
        "time": "date",
        "frequency": "D",
        "horizon": 10,
    }
    spec = resolve(proposal, frame, {})
    with pytest.raises(ModelerError, match="do not apply"):
        validate_configuration({"family": family, option: value}, spec)
    canonical = validate_configuration({"family": family}, spec)
    assert validate_configuration(canonical, spec) == canonical
