"""Leakage-aware fitting and deployable prediction for tabular and temporal tasks."""

from dataclasses import dataclass
from typing import Any, cast

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyClassifier, DummyRegressor
from sklearn.ensemble import ExtraTreesClassifier, ExtraTreesRegressor
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.model_selection import GroupShuffleSplit, train_test_split
from sklearn.multioutput import ClassifierChain, MultiOutputClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from .contracts import ModelerError, score

__all__ = ["PredictionModel", "fit", "measure", "partition", "validate_configuration"]
_MIN_TRAIN = 10
_MAX_LAGS = 30
_MAX_LAG = 365


def partition(
    data: pd.DataFrame, spec: dict[str, Any]
) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any], np.ndarray[Any, Any]]:
    """Freeze disjoint training, validation, and test rows before model exploration."""
    indices = np.arange(len(data))
    split = spec["split"]
    if split == "official":
        if "_split" not in data or not data["_split"].isin(["train", "validation", "test"]).all():
            raise ModelerError("Official splits require _split values train, validation, test.")
        parts = tuple(indices[data["_split"].eq(name)] for name in ["train", "validation", "test"])
    elif spec["kind"] == "forecast":
        parts = _forecast_partition(data, spec)
    elif split == "temporal":
        dates = pd.to_datetime(data[spec["time"]])
        unique = sorted(dates.unique())
        train_end, val_end = unique[int(len(unique) * 0.6)], unique[int(len(unique) * 0.8)]
        parts = (
            indices[dates < train_end],
            indices[(dates >= train_end) & (dates < val_end)],
            indices[dates >= val_end],
        )
    elif split == "group":
        groups = data[spec["group"]]
        splitter = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=spec["seed"])
        development, test = next(splitter.split(indices, groups=groups))
        splitter = GroupShuffleSplit(n_splits=1, test_size=0.25, random_state=spec["seed"])
        train, validation = next(splitter.split(development, groups=groups.iloc[development]))
        parts = (development[train], development[validation], test)
    else:
        labels = data[spec["targets"][0]] if spec["kind"] in {"binary", "multiclass"} else None
        development, test = train_test_split(
            indices, test_size=0.2, random_state=spec["seed"], stratify=labels
        )
        train, validation = train_test_split(
            development,
            test_size=0.25,
            random_state=spec["seed"],
            stratify=None if labels is None else labels.iloc[development],
        )
        parts = (train, validation, test)
    if min(len(part) for part in parts) < _MIN_TRAIN:
        raise ModelerError("Each split needs at least ten observations; supply more data.")
    return parts[0], parts[1], parts[2]


def _forecast_partition(data: pd.DataFrame, spec: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Reserve two validation origins and one final horizon per series."""
    parts: list[list[int]] = [[], [], []]
    groups = data.groupby(spec["group"], sort=True) if spec["group"] else [("series", data)]
    horizon = spec["horizon"]
    for _, frame in groups:
        ordered = frame.sort_values(spec["time"]).index.to_list()
        parts[0].extend(ordered[: -3 * horizon])
        parts[1].extend(ordered[-3 * horizon : -horizon])
        parts[2].extend(ordered[-horizon:])
    return np.asarray(parts[0]), np.asarray(parts[1]), np.asarray(parts[2])


def validate_configuration(config: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Reject unsupported choices and bound each model's computational footprint."""
    defaults: dict[str, Any] = {
        "family": "linear",
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
    if result["family"] not in {"baseline", "linear", "extra_trees", "seasonal"}:
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
    if result["class_weight"] not in {None, "balanced"}:
        raise ModelerError("class_weight must be null or balanced.")
    if result["strategy"] not in {"independent", "chain"}:
        raise ModelerError("Multilabel strategy must be independent or chain.")
    lags = result["lags"]
    if (
        not lags
        or len(lags) > _MAX_LAGS
        or any(type(lag) is not int or not 1 <= lag <= _MAX_LAG for lag in lags)
    ):
        raise ModelerError("Supply 1-30 positive integer lags, each at most 365.")
    result["lags"] = sorted(set(lags))
    return result


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
            positive = spec["quality"]["objective"]["parameters"].get("positive_label", classes[-1])
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
        groups = data.groupby(spec["group"], sort=True) if spec["group"] else [("series", data)]
        rows = []
        for name, frame in groups:
            ordered = frame.sort_values(spec["time"])
            values = ordered[spec["targets"][0]].astype(float).tolist()
            dates = pd.date_range(
                pd.Timestamp(ordered[spec["time"]].iloc[-1]),
                periods=spec["horizon"] + 1,
                freq=spec["frequency"],
            )[1:]
            required = (
                config["seasonal_period"] if config["family"] == "seasonal" else max(config["lags"])
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
    if spec["kind"] != "forecast":
        estimator = _pipeline(data, spec, config)
        target = (
            data[spec["targets"]].to_numpy()
            if spec["kind"] == "multilabel"
            else data[spec["targets"][0]].to_numpy()
        )
        if spec["kind"] in {"binary", "multiclass"}:
            target = target.astype(str)
        estimator.fit(_features(data, spec), target)
        return PredictionModel(spec, config, estimator)
    estimators = {}
    groups = data.groupby(spec["group"], sort=True) if spec["group"] else [("series", data)]
    for name, frame in groups:
        ordered = frame.sort_values(spec["time"])
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
    model: PredictionModel, history: pd.DataFrame, evaluation: pd.DataFrame
) -> dict[str, Any]:
    """Score fixed validation/test observations without supplying their targets to prediction."""
    spec = model.specification
    if spec["kind"] != "forecast":
        truth = (
            evaluation[spec["targets"]].to_numpy()
            if spec["kind"] == "multilabel"
            else evaluation[spec["targets"][0]].to_numpy()
        )
        if spec["kind"] in {"binary", "multiclass"}:
            truth = truth.astype(str)
        return score(spec, truth, model.predict(evaluation))
    measurements = []
    groups = (
        evaluation.groupby(spec["group"], sort=True) if spec["group"] else [("series", evaluation)]
    )
    for name, frame in groups:
        past = history[history[spec["group"]] == name] if spec["group"] else history
        ordered = frame.sort_values(spec["time"])
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
    }
