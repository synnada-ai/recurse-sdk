"""Tiny Tuner: deterministic tuning of a tiny classifier.

The tools implement a complete, reproducible tuning workflow over a small
synthetic dataset whose classes are only separable when the right feature
configuration is chosen. The agent explores feature and training
configurations, measures each candidate, and saves the best validated model
into the run workspace.
"""

import json
import math
import random
from dataclasses import asdict, dataclass

import recurse

_DATASET_SEED = 7
_DATASET_SIZE = 60
_LABEL_MARGIN = 0.05
_NOISY_VALIDATION_POINTS = 2


@dataclass(frozen=True)
class FeatureSpace:
    """One feature configuration applied to the training and validation data.

    Attributes:
        degree: Highest polynomial degree of the expanded features.
        interaction: Whether the pairwise interaction feature is included.
        standardize: Whether features are standardized with training statistics.
        train_features: Expanded training feature vectors.
        train_labels: Training labels.
        validation_features: Expanded validation feature vectors.
        validation_labels: Validation labels.
    """

    degree: int
    interaction: bool
    standardize: bool
    train_features: tuple[tuple[float, ...], ...]
    train_labels: tuple[int, ...]
    validation_features: tuple[tuple[float, ...], ...]
    validation_labels: tuple[int, ...]


@dataclass(frozen=True)
class TrainedModel:
    """A logistic classifier trained on one feature configuration.

    Attributes:
        weights: Learned feature weights.
        bias: Learned bias term.
        configuration: The complete hyperparameter configuration of the trial.
        features: The feature space the model was trained on.
    """

    weights: tuple[float, ...]
    bias: float
    configuration: dict[str, float | int | bool]
    features: FeatureSpace


@dataclass(frozen=True)
class ValidatedModel:
    """A measured tuning trial.

    Attributes:
        precision: Validation precision of the candidate.
        recall: Validation recall of the candidate.
        f1: Validation F1 score of the candidate.
        weights: Learned feature weights.
        bias: Learned bias term.
        model: The candidate's complete hyperparameter configuration.
    """

    precision: float
    recall: float
    f1: float
    weights: tuple[float, ...]
    bias: float
    model: dict[str, float | int | bool]


def _dataset() -> tuple[list[tuple[float, float]], list[int]]:
    """Generate the fixed synthetic dataset shared by every trial."""
    generator = random.Random(_DATASET_SEED)  # noqa: S311 - deterministic ML fixture
    points: list[tuple[float, float]] = []
    labels: list[int] = []
    for _ in range(_DATASET_SIZE):
        x1 = generator.uniform(-1.5, 1.5)
        x2 = generator.uniform(-1.5, 1.5)
        points.append((x1, x2))
        labels.append(1 if x1 * x2 > _LABEL_MARGIN else 0)
    return points, labels


def _expand(point: tuple[float, float], degree: int, interaction: bool) -> list[float]:
    """Expand one raw point into the configured feature vector."""
    features = [point[0], point[1]]
    for power in range(2, degree + 1):
        features.append(point[0] ** power)
        features.append(point[1] ** power)
    if interaction:
        features.append(point[0] * point[1])
    return features


def _standardized(
    train: list[list[float]], validation: list[list[float]]
) -> tuple[list[list[float]], list[list[float]]]:
    """Standardize both splits using training means and deviations."""
    columns = len(train[0])
    means = [sum(row[i] for row in train) / len(train) for i in range(columns)]
    deviations = []
    for i in range(columns):
        variance = sum((row[i] - means[i]) ** 2 for row in train) / len(train)
        deviations.append(math.sqrt(variance))

    def scale(rows: list[list[float]]) -> list[list[float]]:
        """Apply the training statistics to one split."""
        return [[(row[i] - means[i]) / deviations[i] for i in range(columns)] for row in rows]

    return scale(train), scale(validation)


def extract_features(degree: int, interaction: bool, standardize: bool) -> FeatureSpace:
    """Build one feature configuration over the tuning dataset.

    Args:
        degree: Highest polynomial degree to expand each raw feature to.
        interaction: Whether to include the pairwise interaction feature.
        standardize: Whether to standardize features with training statistics.

    Returns:
        The expanded training and validation splits for this configuration.
    """
    points, labels = _dataset()
    train_rows, train_labels, validation_rows, validation_labels = [], [], [], []
    for index, (point, label) in enumerate(zip(points, labels, strict=True)):
        if index % 3 == 0:
            validation_rows.append(_expand(point, degree, interaction))
            validation_labels.append(label)
        else:
            train_rows.append(_expand(point, degree, interaction))
            train_labels.append(label)
    for noisy in range(_NOISY_VALIDATION_POINTS):
        validation_labels[noisy * 10] = 1 - validation_labels[noisy * 10]
    if standardize:
        train_rows, validation_rows = _standardized(train_rows, validation_rows)
    return FeatureSpace(
        degree=degree,
        interaction=interaction,
        standardize=standardize,
        train_features=tuple(tuple(row) for row in train_rows),
        train_labels=tuple(train_labels),
        validation_features=tuple(tuple(row) for row in validation_rows),
        validation_labels=tuple(validation_labels),
    )


def train_model(
    features: FeatureSpace,
    learning_rate: float,
    regularization: float,
    epochs: int,
    seed: int,
) -> TrainedModel:
    """Train a logistic classifier on one feature configuration.

    Training uses full-batch gradient descent with the momentum declared by
    the run's `optimizer_momentum` input, so identical arguments always
    produce an identical model.

    Args:
        features: The feature configuration to train on.
        learning_rate: Gradient descent step size.
        regularization: L2 penalty applied to the weights.
        epochs: Number of full passes over the training split.
        seed: Seed for the deterministic weight initialization.

    Returns:
        The trained candidate model and its complete configuration.
    """
    declared_momentum = recurse.context().inputs["optimizer_momentum"]
    if not isinstance(declared_momentum, int | float):
        raise TypeError("optimizer_momentum must be a number")
    momentum = float(declared_momentum)
    generator = random.Random(seed)  # noqa: S311 - reproducible model initialization
    columns = len(features.train_features[0])
    weights = [generator.gauss(0.0, 0.1) for _ in range(columns)]
    bias = 0.0
    weight_velocity = [0.0] * columns
    bias_velocity = 0.0
    rows = features.train_features
    labels = features.train_labels
    for _ in range(epochs):
        weight_gradient = [0.0] * columns
        bias_gradient = 0.0
        for row, label in zip(rows, labels, strict=True):
            activation = bias + sum(w * x for w, x in zip(weights, row, strict=True))
            error = 1.0 / (1.0 + math.exp(-activation)) - label
            for i in range(columns):
                weight_gradient[i] += error * row[i]
            bias_gradient += error
        for i in range(columns):
            weight_gradient[i] = weight_gradient[i] / len(rows) + regularization * weights[i]
            weight_velocity[i] = momentum * weight_velocity[i] - learning_rate * weight_gradient[i]
            weights[i] += weight_velocity[i]
        bias_velocity = momentum * bias_velocity - learning_rate * bias_gradient / len(rows)
        bias += bias_velocity
    return TrainedModel(
        weights=tuple(weights),
        bias=bias,
        configuration={
            "degree": features.degree,
            "interaction": features.interaction,
            "standardize": features.standardize,
            "learning_rate": learning_rate,
            "regularization": regularization,
            "epochs": epochs,
            "seed": seed,
            "optimizer_momentum": momentum,
        },
        features=features,
    )


def validate_model(
    candidate: TrainedModel, previous: tuple[ValidatedModel, ...]
) -> tuple[ValidatedModel, ...]:
    """Measure a candidate on the validation split and extend the history.

    Args:
        candidate: The trained model to measure.
        previous: Every previously validated model, oldest first.

    Returns:
        The validation history with the new measurement appended.
    """
    features = candidate.features
    true_positive = false_positive = false_negative = 0
    for row, label in zip(features.validation_features, features.validation_labels, strict=True):
        activation = candidate.bias + sum(
            w * x for w, x in zip(candidate.weights, row, strict=True)
        )
        predicted = 1 if activation > 0 else 0
        if predicted and label:
            true_positive += 1
        elif predicted:
            false_positive += 1
        elif label:
            false_negative += 1
    # The fixed validation split always contains positive labels, so the
    # denominator is always positive: a missed positive counts toward it.
    denominator = 2 * true_positive + false_positive + false_negative
    measured = ValidatedModel(
        precision=true_positive / max(1, true_positive + false_positive),
        recall=true_positive / (true_positive + false_negative),
        f1=2 * true_positive / denominator,
        weights=candidate.weights,
        bias=candidate.bias,
        model=dict(candidate.configuration),
    )
    return (*previous, measured)


def save_best_model(validated_models: tuple[ValidatedModel, ...]) -> None:
    """Persist the best validated model as `best-model.json` in the workspace.

    Args:
        validated_models: Every validated model of the run, oldest first.
    """
    best = max(validated_models, key=lambda validated: validated.f1)
    artifact = recurse.context().workspace / "best-model.json"
    artifact.write_text(json.dumps(asdict(best), indent=2, sort_keys=True))
