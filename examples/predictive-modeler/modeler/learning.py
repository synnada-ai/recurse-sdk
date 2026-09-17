"""Leakage-aware fitting and deployable prediction for tabular and temporal tasks."""

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryFile
from typing import Any, cast

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyClassifier, DummyRegressor
from sklearn.ensemble import ExtraTreesClassifier, ExtraTreesRegressor
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.model_selection import (
    GroupKFold,
    GroupShuffleSplit,
    KFold,
    StratifiedKFold,
    TimeSeriesSplit,
    train_test_split,
)
from sklearn.multioutput import ClassifierChain, MultiOutputClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.utils.class_weight import compute_sample_weight

from .contracts import ModelerError, score

__all__ = [
    "PredictionModel",
    "complexity",
    "cross_validate",
    "fit",
    "measure",
    "partition",
    "validate_configuration",
]
_MIN_TRAIN = 10
_MIN_FOLDS = 2
_MAX_FOLDS = 5
_MAX_ORIGINS = 3
_MAX_LAGS = 30
_MAX_LAG = 365


def partition(data: pd.DataFrame, spec: dict[str, Any]) -> dict[str, Any]:
    """Freeze cross-validation folds and an untouched final test before exploration."""
    data = data.reset_index(drop=True)
    indices = np.arange(len(data))
    split = spec["split"]
    folds = []
    try:
        if split == "official":
            if (
                "_split" not in data
                or not data["_split"].isin(["train", "validation", "test"]).all()
            ):
                raise ModelerError("Official splits require _split values train, validation, test.")
            development, validation, test = [
                indices[data["_split"].eq(name)] for name in ["train", "validation", "test"]
            ]
            folds = [{"train": development.tolist(), "validation": validation.tolist()}]
            method = "official"
        elif spec["kind"] == "forecast":
            return _forecast_partition(data, spec)
        elif split == "temporal":
            dates = pd.to_datetime(data[spec["time"]])
            unique = np.sort(dates.unique())
            boundary = int(len(unique) * 0.8)
            development = indices[dates.isin(unique[:boundary])]
            test = indices[dates.isin(unique[boundary:])]
            folds = _temporal_folds(indices, dates, unique[:boundary])
            method = "time_series_split"
        elif split == "group":
            groups = data[spec["group"]]
            outer = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=spec["seed"])
            development, test = next(outer.split(indices, groups=groups))
            count = min(_MAX_FOLDS, groups.iloc[development].nunique())
            inner = GroupKFold(n_splits=count)
            folds = [
                {
                    "train": development[train].tolist(),
                    "validation": development[validation].tolist(),
                }
                for train, validation in inner.split(development, groups=groups.iloc[development])
            ]
            method = "group_kfold"
        else:
            labels = (
                data[spec["targets"][0]].astype(str)
                if spec["kind"] in {"binary", "multiclass"}
                else None
            )
            development, test = train_test_split(
                indices, test_size=0.2, random_state=spec["seed"], stratify=labels
            )
            if labels is not None:
                count = min(_MAX_FOLDS, int(labels.iloc[development].value_counts().min()))
                inner = StratifiedKFold(n_splits=count, shuffle=True, random_state=spec["seed"])
                splits = inner.split(development, labels.iloc[development])
                method = "stratified_kfold"
            else:
                splits = KFold(n_splits=_MAX_FOLDS, shuffle=True, random_state=spec["seed"]).split(
                    development
                )
                method = "kfold"
            folds = [
                {
                    "train": development[train].tolist(),
                    "validation": development[validation].tolist(),
                }
                for train, validation in splits
            ]
    except ValueError as error:
        raise ModelerError(
            f"Cannot construct {split} cross-validation: {error}. "
            "Supply more observations/groups per class."
        ) from error
    if (
        len(test) < _MIN_TRAIN
        or not folds
        or any(len(fold["train"]) < _MIN_TRAIN or not fold["validation"] for fold in folds)
    ):
        raise ModelerError(
            "Cross-validation requires at least ten training rows per fold, "
            "a nonempty validation fold, and ten final-test rows; supply more data."
        )
    if spec["kind"] in {"binary", "multiclass"} and any(
        set(data.iloc[fold["train"]][spec["targets"][0]].astype(str)) != set(spec["classes"])
        for fold in folds
    ):
        raise ModelerError("Every cross-validation training split must contain every target class.")
    return {"method": method, "fit": development.tolist(), "test": test.tolist(), "folds": folds}


def _temporal_folds(indices: Any, dates: pd.Series[Any], unique: Any) -> list[dict[str, list[int]]]:
    """Keep equal timestamps together and reduce folds until initial fits have enough rows."""
    folds = []
    for count in range(min(_MAX_FOLDS, len(unique) - 1), _MIN_FOLDS - 1, -1):
        folds = [
            {
                "train": indices[dates.isin(unique[train])].tolist(),
                "validation": indices[dates.isin(unique[validation])].tolist(),
            }
            for train, validation in TimeSeriesSplit(n_splits=count).split(unique)
        ]
        if min(len(fold["train"]) for fold in folds) >= _MIN_TRAIN:
            break
    return folds


def _forecast_partition(data: pd.DataFrame, spec: dict[str, Any]) -> dict[str, Any]:
    """Reserve a final horizon and refit at two or three earlier expanding origins."""
    groups = data.groupby(spec["group"], sort=True) if spec["group"] else [("series", data)]
    ordered = [
        frame.sort_values(spec["time"], key=pd.to_datetime).index.tolist() for _, frame in groups
    ]
    horizon = spec["horizon"]
    minimum = max(2 * horizon, _MIN_TRAIN + horizon)
    count = min(_MAX_ORIGINS, *((len(rows) - horizon - minimum) // horizon for rows in ordered))
    if count < _MIN_FOLDS:
        raise ModelerError(
            "Forecast cross-validation needs at least two full validation horizons "
            "plus a final horizon after sufficient training history; "
            "supply more history or a shorter horizon."
        )
    folds: list[dict[str, list[int]]] = [{"train": [], "validation": []} for _ in range(count)]
    development, test = [], []
    for rows in ordered:
        development.extend(rows[:-horizon])
        test.extend(rows[-horizon:])
        for origin, fold in enumerate(folds):
            end = len(rows) - (count - origin + 1) * horizon
            fold["train"].extend(rows[:end])
            fold["validation"].extend(rows[end : end + horizon])
    return {"method": "rolling_origin", "fit": development, "test": test, "folds": folds}


def cross_validate(
    data: pd.DataFrame, spec: dict[str, Any], config: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    """Refit per frozen fold; omit fits for complexity-only requests."""
    metrics = [spec["quality"]["objective"], *spec["quality"]["constraints"]]
    if all(metric["metric"] in {"model_bytes", "input_feature_count"} for metric in metrics):
        return {"scores": {}, "folds": []}
    results: list[dict[str, Any]] = []
    for fold in plan["folds"]:
        training, validation = data.iloc[fold["train"]], data.iloc[fold["validation"]]
        model = fit(training, spec, config)
        results.append(
            {
                "scores": measure(model, training, validation, include_complexity=False),
                "train_rows": len(training),
                "validation_rows": len(validation),
            }
        )
    scores = {
        key: None
        if any(result["scores"][key] is None for result in results)
        else float(np.mean([result["scores"][key] for result in results]))
        for key in results[0]["scores"]
    }
    return {"scores": scores, "folds": results}


def validate_configuration(config: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Reject unsupported choices and bound each model's computational footprint."""
    defaults: dict[str, Any] = {
        "family": "linear",
        "feature_subset": None,
        "scale": True,
        "regularization": 1.0,
        "trees": 100,
        "max_depth": 12,
        "min_samples_leaf": 2,
        "class_weight": None,
        "threshold": 0.5,
        "strategy": "independent",
        "lags": [1, 7, 14],
        "seasonal_period": 7,
        "max_features": 10000,
        "ngram_max": 1,
    }
    if set(config) - set(defaults):
        raise ModelerError(f"Unknown candidate options: {set(config) - set(defaults)}")
    result = defaults | config
    if not isinstance(result["scale"], bool):
        raise ModelerError("scale must be a boolean (true or false); omitted defaults to true.")
    subset = result["feature_subset"]
    if subset is not None and (
        not isinstance(subset, list)
        or not subset
        or any(not isinstance(name, str) for name in subset)
        or len(set(subset)) != len(subset)
        or not set(subset) <= set(spec["features"])
    ):
        raise ModelerError(
            "feature_subset must be a nonempty distinct subset of eligible features."
        )
    if result["family"] not in ("baseline", "linear", "extra_trees", "seasonal"):
        raise ModelerError("family must be baseline, linear, extra_trees, or seasonal.")
    if result["family"] == "seasonal" and spec["kind"] != "forecast":
        raise ModelerError("seasonal is only available for forecasting.")
    bounds = {
        "regularization": (1e-6, 1e6),
        "trees": (1, 300),
        "max_depth": (1, 30),
        "min_samples_leaf": (1, 100),
        "threshold": (0.01, 0.99),
        "seasonal_period": (1, 365),
        "max_features": (100, 30000),
        "ngram_max": (1, 2),
    }
    for key, (lower, upper) in bounds.items():
        value = result[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not lower <= value <= upper
        ):
            raise ModelerError(f"{key} must be between {lower} and {upper}.")
        if key not in {"regularization", "threshold"} and not isinstance(value, int):
            raise ModelerError(f"{key} must be an integer.")
    if result["class_weight"] not in (None, "balanced"):
        raise ModelerError("class_weight must be null or balanced.")
    if result["strategy"] not in ("independent", "chain"):
        raise ModelerError("Multilabel strategy must be independent or chain.")
    lags = result["lags"]
    if (
        not isinstance(lags, list)
        or not lags
        or len(lags) > _MAX_LAGS
        or any(type(lag) is not int or not 1 <= lag <= _MAX_LAG for lag in lags)
    ):
        raise ModelerError("Supply a list of 1-30 positive integer lags, each at most 365.")
    result["lags"] = sorted(set(lags))
    family, kind = result["family"], spec["kind"]
    active = _applicable_options(spec, result)
    inactive = set(defaults) - active
    changed = sorted(key for key in inactive if result[key] != defaults[key])
    if changed:
        raise ModelerError(
            f"Options {changed} do not apply to {kind}/{family}; remove them. "
            "Seasonal forecasts use seasonal_period, not lags."
        )
    result.update({key: defaults[key] for key in inactive})
    return result


def _applicable_options(spec: dict[str, Any], config: dict[str, Any]) -> set[str]:
    """Identify controls that affect this predictor rather than create no-op trials."""
    family, kind = config["family"], spec["kind"]
    active = {"family"}
    if family == "linear":
        active.add("regularization")
    if family == "extra_trees":
        active.update({"trees", "max_depth", "min_samples_leaf"})
    if kind == "forecast":
        if family in {"linear", "extra_trees"}:
            active.add("lags")
        if family == "seasonal":
            active.add("seasonal_period")
    else:
        active.update({"feature_subset", "scale"})
        selected = config["feature_subset"] or spec["features"]
        if set(selected) & set(spec["text_features"]):
            active.update({"max_features", "ngram_max"})
        if kind in {"binary", "multilabel"}:
            active.add("threshold")
        if kind == "multilabel":
            active.add("strategy")
        if kind != "regression" and family in {"linear", "extra_trees"}:
            active.add("class_weight")
    return active


def _estimator(config: dict[str, Any], classification: bool) -> Any:
    """Construct a bounded reproducible estimator from supported choices."""
    family = config["family"]
    if family == "baseline":
        return DummyClassifier(strategy="prior") if classification else DummyRegressor()
    if family == "linear":
        return (
            LogisticRegression(
                C=1 / config["regularization"],
                max_iter=500,
                class_weight=config["class_weight"],
                random_state=42,
            )
            if classification
            else Ridge(alpha=config["regularization"], solver="lsqr")
        )
    arguments = {
        "n_estimators": config["trees"],
        "max_depth": config["max_depth"],
        "min_samples_leaf": config["min_samples_leaf"],
        "random_state": 42,
        "n_jobs": 1,
    }
    return (
        ExtraTreesClassifier(**arguments, class_weight=config["class_weight"])
        if classification
        else ExtraTreesRegressor(**arguments)
    )


def _pipeline(data: pd.DataFrame, spec: dict[str, Any], config: dict[str, Any]) -> Pipeline:
    """Fit tabular encoders and text vocabulary inside each candidate pipeline."""
    texts = spec["text_features"]
    numeric = [
        name
        for name in spec["features"]
        if name not in texts and pd.api.types.is_numeric_dtype(data[name])
    ]
    categorical = [name for name in spec["features"] if name not in texts + numeric]
    numeric_steps: list[tuple[str, Any]] = [
        ("impute", SimpleImputer(strategy="median", keep_empty_features=True))
    ]
    if config["scale"]:
        numeric_steps.append(("scale", StandardScaler()))
    transformers: list[tuple[str, Any, Any]] = [
        ("numeric", Pipeline(numeric_steps), numeric),
        (
            "categorical",
            Pipeline(
                [
                    ("impute", SimpleImputer(strategy="most_frequent", keep_empty_features=True)),
                    ("encode", OneHotEncoder(handle_unknown="ignore")),
                ]
            ),
            categorical,
        ),
    ]
    for index, name in enumerate(texts):
        transformers.append(
            (
                f"text_{index}",
                TfidfVectorizer(
                    max_features=config["max_features"], ngram_range=(1, config["ngram_max"])
                ),
                name,
            )
        )
    estimator = _estimator(config, spec["kind"] != "regression")
    if spec["kind"] == "multilabel":
        estimator = (
            ClassifierChain(estimator, random_state=42)
            if config["strategy"] == "chain"
            else MultiOutputClassifier(estimator, n_jobs=1)
        )
    return Pipeline([("features", ColumnTransformer(transformers)), ("model", estimator)])


def _features(data: pd.DataFrame, spec: dict[str, Any]) -> pd.DataFrame:
    """Normalize missing values without fitting statistics on prediction inputs."""
    result = data[spec["features"]].copy()
    for name in spec["text_features"]:
        result[name] = result[name].fillna("").astype(str)
    # sklearn imputers expect np.nan rather than pandas' nullable scalar.
    return cast(pd.DataFrame, result.astype(object).where(result.notna(), np.nan))


def _calendar(date: pd.Timestamp) -> list[float]:
    """Extract calendar values known at any future forecast date."""
    return [float(date.dayofweek), float(date.month), float(date.dayofyear)]


def _forecast_rows(values: list[float], dates: Any, lags: list[int]) -> tuple[Any, Any]:
    """Construct training features from strictly earlier target observations."""
    features, targets = [], []
    for index in range(max(lags), len(values)):
        features.append([values[index - lag] for lag in lags] + _calendar(dates[index]))
        targets.append(values[index])
    if len(targets) < _MIN_TRAIN:
        raise ModelerError("Insufficient training history for these lags; reduce lags or add data.")
    return features, targets


@dataclass
class PredictionModel:
    """A complete fitted predictor; forecasts accept only history through their origin.

    Attributes:
        specification: Frozen task semantics and feature selection.
        configuration: Normalized experimental choices, including prediction threshold.
        estimator: Fitted tabular pipeline or per-series forecasting estimators.
    """

    specification: dict[str, Any]
    configuration: dict[str, Any]
    estimator: Any

    def predict(self, data: pd.DataFrame) -> Any:
        """Predict tabular rows or forecast a horizon from each series' supplied history."""
        spec, config = self.specification, self.configuration
        if spec["kind"] == "forecast":
            return self._forecast(data)
        features = _features(data, spec)
        if spec["kind"] in {"regression", "multiclass"}:
            return self.estimator.predict(features)
        probabilities = self.estimator.predict_proba(features)
        if spec["kind"] == "binary":
            classes = self.estimator.classes_
            positive = next(
                (
                    metric["parameters"]["positive_label"]
                    for metric in [spec["quality"]["objective"], *spec["quality"]["constraints"]]
                    if "positive_label" in metric["parameters"]
                ),
                classes[-1],
            )
            index = list(classes).index(positive)
            return np.where(
                probabilities[:, index] >= config["threshold"], positive, classes[1 - index]
            )
        if isinstance(probabilities, list):
            probabilities = np.column_stack(
                [
                    values[:, list(classes).index(1)] if 1 in classes else np.zeros(len(values))
                    for values, classes in zip(probabilities, self.estimator.classes_, strict=True)
                ]
            )
        return (probabilities >= config["threshold"]).astype(int)

    def _forecast(self, data: pd.DataFrame) -> pd.DataFrame:
        """Predict recursively without reading actual future targets."""
        spec, config = self.specification, self.configuration
        if spec["group"] and data[spec["group"]].isna().any():
            raise ModelerError(
                f"Series identifier column {spec['group']!r} contains missing values; "
                "supply an identifier for every row."
            )
        groups = data.groupby(spec["group"], sort=True) if spec["group"] else [("series", data)]
        rows = []
        for name, frame in groups:
            ordered = frame.sort_values(spec["time"], key=pd.to_datetime)
            values = ordered[spec["targets"][0]].astype(float).tolist()
            dates = pd.date_range(
                pd.Timestamp(ordered[spec["time"]].iloc[-1]),
                periods=spec["horizon"] + 1,
                freq=spec["frequency"],
            )[1:]
            required = (
                config["seasonal_period"]
                if config["family"] == "seasonal"
                else 1
                if config["family"] == "baseline"
                else max(config["lags"])
            )
            if len(values) < required:
                raise ModelerError(f"Series {name!r} needs at least {required} historical values.")
            for date in dates:
                if config["family"] in {"baseline", "seasonal"}:
                    period = config["seasonal_period"] if config["family"] == "seasonal" else 1
                    prediction = values[-period]
                else:
                    features = [values[-lag] for lag in config["lags"]] + _calendar(date)
                    prediction = float(self.estimator[str(name)].predict([features])[0])
                values.append(prediction)
                row = {spec["time"]: date, "prediction": prediction}
                if spec["group"]:
                    row[spec["group"]] = name
                rows.append(row)
        return pd.DataFrame(rows)


def fit(data: pd.DataFrame, spec: dict[str, Any], config: dict[str, Any]) -> PredictionModel:
    """Fit only the supplied training observations, including all preprocessing."""
    if config["feature_subset"] is not None:
        spec = spec | {
            "features": config["feature_subset"],
            "text_features": [
                name for name in spec["text_features"] if name in config["feature_subset"]
            ],
        }
    if spec["kind"] != "forecast":
        estimator = _pipeline(data, spec, config)
        target = (
            data[spec["targets"]].to_numpy()
            if spec["kind"] == "multilabel"
            else data[spec["targets"][0]].to_numpy()
        )
        fit_options = {}
        if spec["kind"] in {"binary", "multiclass"}:
            target = target.astype(str)
            if config["family"] == "extra_trees" and config["class_weight"] == "balanced":
                # sklearn 1.9.1 converts numeric-string keys to integers when expanding
                # a forest's balanced class-weight dictionary. Direct sample weights
                # preserve the same inverse-frequency weighting and original labels.
                estimator.set_params(model__class_weight=None)
                fit_options["model__sample_weight"] = compute_sample_weight("balanced", target)
        estimator.fit(_features(data, spec), target, **fit_options)
        return PredictionModel(spec, config, estimator)
    estimators = {}
    groups = data.groupby(spec["group"], sort=True) if spec["group"] else [("series", data)]
    for name, frame in groups:
        ordered = frame.sort_values(spec["time"], key=pd.to_datetime)
        if config["family"] not in {"baseline", "seasonal"}:
            features, target = _forecast_rows(
                ordered[spec["targets"][0]].tolist(),
                pd.to_datetime(ordered[spec["time"]]).tolist(),
                config["lags"],
            )
            estimator = _estimator(config, False)
            estimator.fit(features, target)
            estimators[str(name)] = estimator
    return PredictionModel(spec, config, estimators)


def measure(
    model: PredictionModel,
    history: pd.DataFrame,
    evaluation: pd.DataFrame,
    model_path: Path | None = None,
    *,
    include_complexity: bool = True,
) -> dict[str, Any]:
    """Score fixed validation/test observations without supplying their targets to prediction."""
    spec = model.specification
    complexity_scores = complexity(model, model_path) if include_complexity else {}
    if spec["kind"] != "forecast":
        truth = (
            evaluation[spec["targets"]].to_numpy()
            if spec["kind"] == "multilabel"
            else evaluation[spec["targets"][0]].to_numpy()
        )
        if spec["kind"] in {"binary", "multiclass"}:
            truth = truth.astype(str)
        return score(spec, truth, model.predict(evaluation)) | complexity_scores
    measurements = []
    groups = (
        evaluation.groupby(spec["group"], sort=True) if spec["group"] else [("series", evaluation)]
    )
    for name, frame in groups:
        past = history[history[spec["group"]] == name] if spec["group"] else history
        past = past.sort_values(spec["time"], key=pd.to_datetime)
        ordered = frame.sort_values(spec["time"], key=pd.to_datetime)
        for start in range(0, len(ordered), spec["horizon"]):
            window = ordered.iloc[start : start + spec["horizon"]]
            predictions = model.predict(past)["prediction"].to_numpy()
            measurements.append(
                score(
                    spec,
                    window[spec["targets"][0]].to_numpy(),
                    predictions,
                    past[spec["targets"][0]].to_numpy(),
                )
            )
            # Reveal observations only after the entire horizon was predicted.
            past = pd.concat([past, window])
    return {
        key: None
        if any(item[key] is None for item in measurements)
        else float(np.mean([item[key] for item in measurements]))
        for key in measurements[0]
    } | complexity_scores


def complexity(model: PredictionModel, model_path: Path | None = None) -> dict[str, float]:
    """Measure the complete predictor once, outside forecast-origin aggregation."""
    spec = model.specification
    requested = {
        item["metric"] for item in [spec["quality"]["objective"], *spec["quality"]["constraints"]]
    }
    result = {}
    if "model_bytes" in requested:
        if model_path is None:
            with TemporaryFile() as stream:
                joblib.dump(model, stream, compress=0, protocol=5)
                size = stream.tell()
        else:
            size = model_path.stat().st_size
        result["model_bytes:{}"] = float(size)
    if "input_feature_count" in requested:
        columns = (
            set(spec["targets"] + [spec["time"]]) | ({spec["group"]} if spec["group"] else set())
            if spec["kind"] == "forecast"
            else set(spec["features"])
        )
        result["input_feature_count:{}"] = float(len(columns))
    return result
