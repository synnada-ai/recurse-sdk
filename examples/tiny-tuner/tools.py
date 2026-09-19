"""Search compact MNIST networks under one fixed cross-validation protocol."""

import fcntl
import json
import math
import shutil
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

import torch
from torch import Tensor, nn
from torchvision.datasets import MNIST

import recurse

__all__ = ["Candidate", "design_network", "evaluate_network", "finish_search", "profile_network"]

_IMAGE_WIDTH = 28
_CLASSES = 10
_MAX_WIDTH = 256
_MAX_DEPTH = 3
_MAX_HEAD_SIZE = 7
_MAX_EPOCHS = 100
_MIN_BATCH = 16
_MAX_BATCH = 1024
_MNIST_TRAIN_SIZE = 60000
_MIN_INNER_CLASS_SIZE = 2


@dataclass(frozen=True)
class Candidate:
    """A dense or convolutional network and its complete training recipe."""

    family: Literal["mlp", "cnn", "separable", "attention"]
    widths: tuple[int, ...]
    learning_rate: float
    weight_decay: float
    epochs: int
    batch_size: int
    normalization: Literal["none", "batch", "layer", "group"]
    activation: Literal["relu", "gelu"]
    pooling: Literal["max", "average"]
    head_size: int = 2
    schedule: Literal["constant", "cosine"] = "constant"
    convs_per_stage: int = 1
    min_lr_ratio: float = 0.1
    schedule_epochs: int = 0


def _check(candidate: Candidate) -> None:  # noqa: PLR0912 - independently bounded recipe fields
    """Reject malformed or unbounded recipes before allocating resources."""
    if candidate.family not in {"mlp", "cnn", "separable", "attention"}:
        raise ValueError("family must be mlp, cnn, separable, or attention")
    if not 1 <= len(candidate.widths) <= _MAX_DEPTH or any(
        type(width) is not int or not 1 <= width <= _MAX_WIDTH for width in candidate.widths
    ):
        raise ValueError("widths must contain 1 to 3 integers between 1 and 256")
    allowed_norms = {
        "mlp": {"none", "batch", "layer"},
        "cnn": {"none", "batch", "group"},
        "separable": {"none", "batch", "group"},
        "attention": {"none", "layer"},
    }
    if candidate.normalization not in allowed_norms[candidate.family]:
        raise ValueError("normalization is incompatible with family; consult design_network")
    if candidate.activation not in {"relu", "gelu"} or candidate.pooling not in {"max", "average"}:
        raise ValueError("activation must be relu/gelu and pooling must be max/average")
    if type(candidate.head_size) is not int or not 1 <= candidate.head_size <= _MAX_HEAD_SIZE:
        raise ValueError("head_size must be an integer between 1 and 7")
    if candidate.family in {"cnn", "separable"} and candidate.head_size > (
        _IMAGE_WIDTH // 2 ** len(candidate.widths)
    ):
        raise ValueError("head_size cannot exceed the final spatial width")
    if candidate.family == "attention" and len(candidate.widths) != 1:
        raise ValueError("attention requires exactly one embedding width")
    if not math.isfinite(candidate.learning_rate) or not 0 < candidate.learning_rate <= 1:
        raise ValueError("learning_rate must be finite and in (0, 1]")
    if candidate.schedule not in {"constant", "cosine"}:
        raise ValueError("schedule must be constant or cosine")
    if not math.isfinite(candidate.min_lr_ratio) or not 0 <= candidate.min_lr_ratio <= 1:
        raise ValueError("min_lr_ratio must be finite and in [0, 1]")
    if (
        type(candidate.schedule_epochs) is not int
        or not 0 <= candidate.schedule_epochs <= _MAX_EPOCHS
    ):
        raise ValueError("schedule_epochs must be an integer between 0 and 100")
    if type(candidate.convs_per_stage) is not int or candidate.convs_per_stage not in {1, 2}:
        raise ValueError("convs_per_stage must be an integer, 1 or 2")
    if not math.isfinite(candidate.weight_decay) or not 0 <= candidate.weight_decay <= 1:
        raise ValueError("weight_decay must be finite and in [0, 1]")
    if type(candidate.epochs) is not int or not 1 <= candidate.epochs <= _MAX_EPOCHS:
        raise ValueError("epochs must be an integer between 1 and 100")
    if (
        type(candidate.batch_size) is not int
        or not _MIN_BATCH <= candidate.batch_size <= _MAX_BATCH
    ):
        raise ValueError("batch_size must be an integer between 16 and 1024")


def design_network(  # noqa: PLR0913 - independently tunable recipe dimensions
    family: Literal["mlp", "cnn", "separable", "attention"],
    widths: tuple[int, ...],
    *,
    learning_rate: float = 0.001,
    weight_decay: float = 0.0,
    epochs: int = 5,
    batch_size: int = 128,
    normalization: Literal["none", "batch", "layer", "group"] = "none",
    activation: Literal["relu", "gelu"] = "relu",
    pooling: Literal["max", "average"] = "max",
    head_size: int = 2,
    schedule: Literal["constant", "cosine"] = "constant",
    convs_per_stage: int = 1,
    min_lr_ratio: float = 0.1,
    schedule_epochs: int = 0,
) -> Candidate:
    """Construct a bounded architecture and training recipe without training it.

    Args:
        family: mlp, cnn (3x3 convolutions), separable (depthwise + pointwise), or attention
            (7x7 patches, 16 tokens, one attention head, residual feedforward block).
        widths: One to three hidden widths or convolution channel counts, each 1 to 256.
        learning_rate: Adam learning rate in (0, 1].
        weight_decay: Adam L2 penalty in [0, 1].
        epochs: Training passes per fold, 1 to 100 and at most the run's max_epochs.
            Under early_stopping this is a ceiling, not a mandatory number of passes.
        batch_size: Minibatch size from 16 to 1024.
        normalization: none; batch for MLP/CNN; layer for MLP/attention; group for CNN
            (one group, channel affine parameters). Applied before each hidden activation.
        activation: relu or gelu for hidden activations.
        pooling: max or average 2x2 spatial pooling for CNNs; ignored by other families.
        head_size: CNN adaptive average pooling output side, 1 to 7, no larger than the
            final spatial width. Larger values preserve spatial detail at a parameter cost.
            Ignored by MLP and attention families.
        schedule: constant or cosine learning rate across epochs. Cosine starts at
            learning_rate and ends at min_lr_ratio times it; one epoch uses learning_rate
            unchanged.
        convs_per_stage: One or two convolution blocks per CNN/separable stage, each
            followed by normalization and activation. Pool once per stage. Ignored by
            MLP and attention. A separable block is depthwise followed by pointwise.
        min_lr_ratio: Final cosine learning rate divided by initial learning_rate, in
            [0, 1], default 0.1. Ignored by constant schedules and one-epoch training.
        schedule_epochs: Cosine horizon, 0 uses epochs (legacy), otherwise 1 to 100.
            Hold the minimum rate after this horizon even if training continues. A horizon
            of 1 uses the initial rate for epoch one and the minimum thereafter.

    Returns:
        A recipe to pass directly to evaluate_network. All layers include trainable biases.
    """
    candidate = Candidate(
        family,
        widths,
        learning_rate,
        weight_decay,
        epochs,
        batch_size,
        normalization,
        activation,
        pooling,
        head_size,
        schedule,
        convs_per_stage,
        min_lr_ratio,
        schedule_epochs,
    )
    _check(candidate)
    return candidate


def _activation(candidate: Candidate) -> nn.Module:
    """Instantiate the chosen parameter-free nonlinearity."""
    return nn.ReLU() if candidate.activation == "relu" else nn.GELU()


def _normalization(candidate: Candidate, width: int, spatial: bool) -> nn.Module:
    """Use normalization with no validation-derived running statistics."""
    if candidate.normalization == "batch":
        return nn.BatchNorm2d(width) if spatial else nn.BatchNorm1d(width)
    if candidate.normalization == "layer":
        return nn.LayerNorm(width)
    if candidate.normalization == "group":
        return nn.GroupNorm(1, width)
    return nn.Identity()


class _PatchAttention(nn.Module):
    """A bounded patch model with positional embeddings and one residual attention block."""

    def __init__(self, candidate: Candidate) -> None:
        """Build a 16-token model with a feedforward expansion of two."""
        super().__init__()
        width = candidate.widths[0]
        self.patch = nn.Conv2d(1, width, 7, stride=7)
        self.position = nn.Parameter(torch.zeros(1, 16, width))
        self.attention = nn.MultiheadAttention(width, 1, batch_first=True)
        self.norm1 = _normalization(candidate, width, False)
        self.norm2 = _normalization(candidate, width, False)
        self.feedforward = nn.Sequential(
            nn.Linear(width, 2 * width), _activation(candidate), nn.Linear(2 * width, width)
        )
        self.head = nn.Linear(width, _CLASSES)

    def forward(self, images: Tensor) -> Tensor:
        """Classify mean-pooled patch representations without an attention mask."""
        tokens = self.patch(images).flatten(2).transpose(1, 2) + self.position
        normalized = self.norm1(tokens)
        attended, _ = self.attention(normalized, normalized, normalized, need_weights=False)
        tokens = tokens + attended
        tokens = tokens + self.feedforward(self.norm2(tokens))
        return cast(Tensor, self.head(tokens.mean(1)))


def _network(candidate: Candidate) -> nn.Module:
    """Build a fresh network; adaptive pooling keeps small CNN heads economical."""
    if candidate.family == "attention":
        return _PatchAttention(candidate)
    layers: list[nn.Module] = []
    if candidate.family == "mlp":
        layers.append(nn.Flatten())
        features = _IMAGE_WIDTH * _IMAGE_WIDTH
        for width in candidate.widths:
            layers.extend(
                (
                    nn.Linear(features, width),
                    _normalization(candidate, width, False),
                    _activation(candidate),
                )
            )
            features = width
    else:
        features = 1
        for width in candidate.widths:
            for _ in range(candidate.convs_per_stage):
                if candidate.family == "separable":
                    layers.extend(
                        (
                            nn.Conv2d(features, features, 3, padding=1, groups=features),
                            nn.Conv2d(features, width, 1),
                        )
                    )
                else:
                    layers.append(nn.Conv2d(features, width, 3, padding=1))
                layers.extend((_normalization(candidate, width, True), _activation(candidate)))
                features = width
            pool = nn.MaxPool2d(2) if candidate.pooling == "max" else nn.AvgPool2d(2)
            layers.append(pool)
        layers.extend(
            (nn.AdaptiveAvgPool2d((candidate.head_size, candidate.head_size)), nn.Flatten())
        )
        features *= candidate.head_size**2
    layers.append(nn.Linear(features, _CLASSES))
    return nn.Sequential(*layers)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialize shared file and training operations, releasing locks even on failure."""
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


@contextmanager
def _cpu() -> Iterator[None]:
    """Bound intra-op CPU parallelism and restore the host setting on every exit path."""
    previous = torch.get_num_threads()
    torch.set_num_threads(4)
    try:
        yield
    finally:
        torch.set_num_threads(previous)


def _write(path: Path, value: object) -> None:
    """Replace a JSON artifact atomically so interrupted writes preserve prior evidence."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))
    temporary.replace(path)


def _data() -> tuple[Tensor, Tensor]:
    """Load only official training data; cache downloads outside exported artifacts."""
    cache = Path(tempfile.gettempdir()) / "recurse-mnist"
    cache.mkdir(exist_ok=True)
    with _locked(cache / "download.lock"):
        dataset = MNIST(str(cache), train=True, download=True)
    return dataset.data, dataset.targets


def _folds(labels: Tensor, count: int, samples: int, seed: int) -> tuple[Tensor, ...]:
    """Select a balanced deterministic sample and partition each class across folds."""
    generator = torch.Generator().manual_seed(seed)
    folds: list[list[Tensor]] = [[] for _ in range(count)]
    capacities = [int((labels == label).sum()) for label in range(_CLASSES)]
    allocation = capacities.copy()
    if samples != _MNIST_TRAIN_SIZE:
        if samples > sum(capacities):
            raise ValueError("MNIST must provide enough examples for the requested sample count")
        remaining = samples
        for offset, label in enumerate(sorted(range(_CLASSES), key=capacities.__getitem__)):
            classes_left = _CLASSES - offset
            allocation[label] = min(capacities[label], math.ceil(remaining / classes_left))
            remaining -= allocation[label]
    for label in range(_CLASSES):
        indices = torch.where(labels == label)[0]
        indices = indices[torch.randperm(len(indices), generator=generator)]
        take = allocation[label]
        if take < count:
            raise ValueError("MNIST must provide enough examples of every class for every fold")
        for fold, partition in zip(folds, indices[:take].tensor_split(count), strict=True):
            fold.append(partition)
    return tuple(torch.cat(fold) for fold in folds)


def _splits(labels: Tensor, settings: dict[str, Any]) -> tuple[tuple[Tensor, Tensor], ...]:
    """Resolve repeatable train/validation partitions without changing the selected sample."""
    method = settings["cv_method"]
    count = 2 if method == "stratified_holdout" else settings["cv_folds"]
    seed = settings["cv_seed"]
    initial = _folds(labels, count, settings["samples"], seed)
    selected = torch.cat(initial)
    splits: list[tuple[Tensor, Tensor]] = []
    for repeat in range(settings["cv_repeats"]):
        generator = torch.Generator().manual_seed(seed + repeat)
        if method == "stratified_holdout":
            training_parts, validation_parts = [], []
            for label in range(_CLASSES):
                indices = selected[labels[selected] == label]
                indices = indices[torch.randperm(len(indices), generator=generator)]
                take = max(1, round(len(indices) * settings["validation_fraction"]))
                validation_parts.append(indices[:take])
                training_parts.append(indices[take:])
            splits.append((torch.cat(training_parts), torch.cat(validation_parts)))
            continue
        if method == "kfold":
            shuffled = selected[torch.randperm(len(selected), generator=generator)]
            folds = tuple(shuffled.tensor_split(count))
        elif repeat == 0:
            folds = initial
        else:
            folds = tuple(
                selected[fold]
                for fold in _folds(labels[selected], count, _MNIST_TRAIN_SIZE, seed + repeat)
            )
        for index, validation in enumerate(folds):
            training_indices = torch.cat(
                [fold for other, fold in enumerate(folds) if other != index]
            )
            splits.append((training_indices, validation))
    return tuple(splits)


def _pixels(images: Tensor, indices: Tensor) -> Tensor:
    """Apply fixed scaling per minibatch without statistics from held-out examples."""
    return (
        images[indices].unsqueeze(1).float().div_(255).contiguous(memory_format=torch.channels_last)
    )


def _set_learning_rate(candidate: Candidate, optimizer: torch.optim.Adam, epoch: int) -> None:
    """Apply cosine decay on its own horizon, then hold the floor without oscillation."""
    if candidate.schedule == "cosine":
        horizon = candidate.schedule_epochs or candidate.epochs
        duration = max(1, horizon - 1)
        factor = (1 + candidate.min_lr_ratio) / 2 + (
            (1 - candidate.min_lr_ratio) / 2 * math.cos(math.pi * min(epoch, duration) / duration)
        )
        for group in optimizer.param_groups:
            group["lr"] = candidate.learning_rate * factor


def _epoch(  # noqa: PLR0913, PLR0917 - explicit training state and data
    model: nn.Module,
    optimizer: torch.optim.Adam,
    candidate: Candidate,
    images: Tensor,
    labels: Tensor,
    indices: Tensor,
    generator: torch.Generator,
    deadline: float,
) -> float:
    """Train one shuffled pass and return example-weighted pre-update cross-entropy."""
    model.train()
    shuffled = indices[torch.randperm(len(indices), generator=generator)]
    total = 0.0
    for batch in shuffled.tensor_split(max(1, math.ceil(len(shuffled) / candidate.batch_size))):
        _remaining(deadline)
        optimizer.zero_grad(set_to_none=True)
        loss = nn.functional.cross_entropy(model(_pixels(images, batch)), labels[batch])
        if not bool(torch.isfinite(loss)):
            raise ValueError("Training loss is non-finite; reduce learning_rate")
        total += float(loss.detach()) * len(batch)
        loss.backward()
        optimizer.step()
    return total / len(indices)


def _train(  # noqa: PLR0913, PLR0917 - explicit data, randomness, and deadline
    candidate: Candidate,
    images: Tensor,
    labels: Tensor,
    indices: Tensor,
    seed: int,
    deadline: float,
) -> nn.Module:
    """Train from scratch with local random state and bounded minibatch allocations."""
    with torch.random.fork_rng(devices=[]):
        torch.manual_seed(seed)
        model = _network(candidate).to(memory_format=torch.channels_last)
        optimizer = torch.optim.Adam(
            model.parameters(), lr=candidate.learning_rate, weight_decay=candidate.weight_decay
        )
        generator = torch.Generator().manual_seed(seed)
        for epoch in range(candidate.epochs):
            _set_learning_rate(candidate, optimizer, epoch)
            _epoch(model, optimizer, candidate, images, labels, indices, generator, deadline)
    return model


def _inner_split(
    labels: Tensor, indices: Tensor, fraction: float, seed: int
) -> tuple[Tensor, Tensor]:
    """Partition only outer-training observations, preserving every class on both sides."""
    generator = torch.Generator().manual_seed(seed)
    training, validation = [], []
    for label in range(_CLASSES):
        selected = indices[labels[indices] == label]
        if len(selected) < _MIN_INNER_CLASS_SIZE:
            raise ValueError(
                f"Early stopping needs at least two outer-training examples of class {label}; "
                "increase samples or use stopping_method='fixed'"
            )
        selected = selected[torch.randperm(len(selected), generator=generator)]
        count = max(1, min(len(selected) - 1, round(len(selected) * fraction)))
        training.append(selected[count:])
        validation.append(selected[:count])
    return torch.cat(training), torch.cat(validation)


def _validation(
    model: nn.Module, images: Tensor, labels: Tensor, indices: Tensor, deadline: float
) -> tuple[float, float]:
    """Measure inner loss and accuracy without gradients or updating normalization buffers."""
    model.eval()
    total, correct = 0.0, 0
    with torch.inference_mode():
        for batch in indices.split(512):
            _remaining(deadline)
            logits = model(_pixels(images, batch))
            total += float(nn.functional.cross_entropy(logits, labels[batch], reduction="sum"))
            correct += int((logits.argmax(1) == labels[batch]).sum())
    _remaining(deadline)
    if not math.isfinite(total):
        raise ValueError("Inner validation loss is non-finite; reduce learning_rate")
    return total / len(indices), correct / len(indices)


def _train_early(  # noqa: PLR0913, PLR0917 - explicit fold data, controls and owned diagnostics
    candidate: Candidate,
    images: Tensor,
    labels: Tensor,
    indices: Tensor,
    seed: int,
    deadline: float,
    settings: Mapping[str, Any],
    split_seed: int,
    diagnostics: dict[str, Any],
) -> nn.Module:
    """Stop on inner-loss stagnation and restore the minimum-loss state, including buffers."""
    training, validation = _inner_split(
        labels, indices, settings.get("stopping_validation_fraction", 0.1), split_seed
    )
    diagnostics.update(
        training_examples=len(training),
        inner_validation_examples=len(validation),
        actual_epochs=0,
        best_epoch=None,
        stop_reason="epoch_ceiling",
        learning_curve=[],
    )
    best_loss = significant_loss = math.inf
    best_state: dict[str, Tensor] = {}
    stale = 0
    try:
        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(seed)
            model = _network(candidate).to(memory_format=torch.channels_last)
            optimizer = torch.optim.Adam(
                model.parameters(), lr=candidate.learning_rate, weight_decay=candidate.weight_decay
            )
            generator = torch.Generator().manual_seed(seed)
            for epoch in range(candidate.epochs):
                _set_learning_rate(candidate, optimizer, epoch)
                training_loss = _epoch(
                    model, optimizer, candidate, images, labels, training, generator, deadline
                )
                diagnostics["actual_epochs"] = epoch + 1
                observation = {
                    "epoch": epoch + 1,
                    "training_loss": training_loss,
                    "inner_validation_loss": None,
                    "inner_validation_accuracy": None,
                    "learning_rate": optimizer.param_groups[0]["lr"],
                }
                diagnostics["learning_curve"].append(observation)
                loss, accuracy = _validation(model, images, labels, validation, deadline)
                observation.update(inner_validation_loss=loss, inner_validation_accuracy=accuracy)
                if loss < best_loss:
                    best_loss = loss
                    best_state = {
                        key: value.detach().clone() for key, value in model.state_dict().items()
                    }
                    diagnostics["best_epoch"] = epoch + 1
                if loss < significant_loss - settings.get("min_delta", 0.0001):
                    significant_loss, stale = loss, 0
                else:
                    stale += 1
                _remaining(deadline)
                if epoch + 1 >= settings.get("min_epochs", 3) and stale >= settings.get(
                    "patience", 3
                ):
                    diagnostics["stop_reason"] = "patience"
                    break
            _remaining(deadline)
            model.load_state_dict(best_state)
            _remaining(deadline)
    except TimeoutError:
        diagnostics["stop_reason"] = "wall_clock"
        raise
    except Exception:
        diagnostics["stop_reason"] = "failed"
        raise
    return model


def _accuracy(
    model: nn.Module, images: Tensor, labels: Tensor, indices: Tensor, deadline: float
) -> float:
    """Measure classification accuracy without gradients or model updates."""
    model.eval()
    correct = 0
    with torch.inference_mode():
        for batch in indices.split(512):
            _remaining(deadline)
            correct += int((model(_pixels(images, batch)).argmax(1) == labels[batch]).sum())
    return correct / len(indices)


def _ledger(workspace: Path) -> list[dict[str, Any]]:
    """Read the authoritative history rather than trusting an agent-supplied score."""
    path = workspace / "trials.json"
    return json.loads(path.read_text()) if path.exists() else []


def _remaining(deadline: float) -> None:
    """Stop at cooperative training boundaries when the wall-clock budget is consumed."""
    if time.monotonic() >= deadline:
        raise TimeoutError("Search wall-clock budget exhausted")


def _protocol_version(settings: Mapping[str, Any], candidate: Candidate) -> int:
    """Label changed stopping or scheduling semantics without relabeling legacy recipes."""
    return (
        4
        if settings.get("stopping_method", "fixed") == "early_stopping" or candidate.schedule_epochs
        else 3
    )


def _deadline(workspace: Path, seconds: float, version: int = 3) -> float:
    """Persist the first profile/evaluation deadline so calls share one allowance."""
    path = workspace / "search.json"
    if not path.exists():
        _write(
            path,
            {
                "deadline": time.monotonic() + seconds,
                "protocol": dict(recurse.context().inputs),
                "protocol_version": version,
            },
        )
    saved = json.loads(path.read_text())
    if version > saved.get("protocol_version", 3):
        saved["protocol_version"] = version
        _write(path, saved)
    return float(saved["deadline"])


def _winner(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Rank feasible trials by size; otherwise retain the most accurate diagnostic."""
    completed = [trial for trial in history if trial["status"] == "completed"]
    feasible = [trial for trial in completed if trial["target_reached"]]
    if feasible:
        return min(
            feasible,
            key=lambda trial: (trial["parameter_count"], -trial["cv_accuracy"], trial["trial"]),
        )
    return min(
        completed,
        key=lambda trial: (-trial["cv_accuracy"], trial["parameter_count"], trial["trial"]),
        default=None,
    )


def profile_network(candidate: Candidate) -> dict[str, Any]:
    """Estimate full-CV training cost from a short discarded training sample.

    Starts and consumes the shared search allowance. No validation accuracy, checkpoint,
    qualification, or trial is produced. Estimates include model/optimizer initialization
    but exclude inner and outer validation, tool latency and runtime variability; reserve
    a margin. With early stopping this projects training through the epoch ceiling using
    the full outer training size, not the unknown epoch count at which stopping will occur.

    Args:
        candidate: Proposed recipe to time before committing to complete CV.

    Returns:
        Measured sample duration, projected training duration for all configured splits,
        parameter count, and remaining seconds. Evidence is retained in profiles.json.
    """
    _check(candidate)
    context = recurse.context()
    settings = context.inputs
    if candidate.epochs > settings["max_epochs"]:
        raise ValueError("epochs exceeds the run's max_epochs")
    with _locked(context.workspace / "search.lock"), _cpu():
        if (context.workspace / "receipt.json").exists():
            raise ValueError("Search is finalized; start a new run for more trials")
        deadline = _deadline(
            context.workspace, settings["max_seconds"], _protocol_version(settings, candidate)
        )
        _remaining(deadline)
        images, labels = _data()
        splits = _splits(labels, dict(settings))
        training = splits[0][0]
        sample = training[: candidate.batch_size * 12]
        pilot = replace(candidate, epochs=1)
        _train(
            pilot, images, labels, sample[: candidate.batch_size * 2], settings["seed"], deadline
        )
        started = time.monotonic()
        model = _train(pilot, images, labels, sample, settings["seed"], deadline)
        elapsed = time.monotonic() - started
        batches = sum(math.ceil(len(train) / candidate.batch_size) for train, _ in splits)
        result = {
            "candidate": asdict(candidate),
            "sample_seconds": elapsed,
            "estimated_training_seconds": elapsed
            / math.ceil(len(sample) / candidate.batch_size)
            * batches
            * candidate.epochs,
            "parameter_count": sum(parameter.numel() for parameter in model.parameters()),
            "remaining_seconds": max(0.0, deadline - time.monotonic()),
        }
        path = context.workspace / "profiles.json"
        profiles = json.loads(path.read_text()) if path.exists() else []
        profiles.append(result)
        _write(path, profiles)
        return result


def evaluate_network(candidate: Candidate) -> dict[str, Any]:  # noqa: PLR0915 - durable evaluation phases
    """Train fresh models on fixed folds and record independently measured CV accuracy.

    Duplicate recipes return cached results. Every new attempt consumes a trial, including
    failures and timeouts. The first profile or evaluation starts the wall-clock allowance.
    Calls serialize to protect history and random state. A timed-out trial has no CV score.

    Args:
        candidate: A recipe returned by design_network; max_epochs is enforced here.

    Returns:
        Trial status and recipe; completed trials additionally include parameter count,
        fold accuracies, their arithmetic mean and standard deviation, and target feasibility.
        Every completed trial retains its last fold's weights, without extra refit training.
    """
    _check(candidate)
    context = recurse.context()
    settings = context.inputs
    if candidate.epochs > settings["max_epochs"]:
        raise ValueError("epochs exceeds the run's max_epochs")
    recipe = json.loads(json.dumps(asdict(candidate)))
    with _locked(context.workspace / "search.lock"), _cpu():
        if (context.workspace / "receipt.json").exists():
            raise ValueError("Search is finalized; start a new run for more trials")
        deadline = _deadline(
            context.workspace, settings["max_seconds"], _protocol_version(settings, candidate)
        )
        history = _ledger(context.workspace)
        for trial in history:
            if trial["candidate"] == recipe:
                return trial
        _remaining(deadline)
        if len(history) >= settings["max_trials"]:
            raise ValueError("max_trials exhausted; call finish_search")
        trial = {"candidate": recipe, "status": "started", "trial": len(history) + 1}
        history.append(trial)
        _write(context.workspace / "trials.json", history)
        try:
            images, labels = _data()
            _remaining(deadline)
            splits = _splits(labels, dict(settings))
            scores = []
            parameter_count = 0
            for index, (training, validation) in enumerate(splits):
                if settings.get("stopping_method", "fixed") == "early_stopping":
                    diagnostics: dict[str, Any] = {"fold": index}
                    trial.setdefault("fold_training", []).append(diagnostics)
                    model = _train_early(
                        candidate,
                        images,
                        labels,
                        training,
                        settings["seed"] + index,
                        deadline,
                        settings,
                        settings["cv_seed"] + index,
                        diagnostics,
                    )
                    training_examples = diagnostics["training_examples"]
                else:
                    model = _train(
                        candidate, images, labels, training, settings["seed"] + index, deadline
                    )
                    training_examples = len(training)
                parameter_count = sum(parameter.numel() for parameter in model.parameters())
                scores.append(_accuracy(model, images, labels, validation, deadline))
            _remaining(deadline)
            mean = sum(scores) / len(scores)
            checkpoint = f"trial-{trial['trial']}.pt"
            torch.save(model.state_dict(), context.workspace / checkpoint)
            trial.update(
                status="completed",
                parameter_count=parameter_count,
                fold_accuracies=scores,
                cv_accuracy=mean,
                cv_std=math.sqrt(sum((score - mean) ** 2 for score in scores) / len(scores)),
                target_reached=mean >= settings["target_accuracy"],
                checkpoint=checkpoint,
                checkpoint_fold=len(splits) - 1,
                training_examples=training_examples,
            )
        except TimeoutError:
            trial.update(status="timed_out")
        except Exception as error:
            trial.update(status="failed", error=str(error))
            raise
        finally:
            remaining = max(0.0, deadline - time.monotonic())
            trial.update(
                remaining_seconds=remaining, elapsed_seconds=settings["max_seconds"] - remaining
            )
            _write(context.workspace / "trials.json", history)
        return trial


def finish_search(
    reason: Literal["wall_clock", "trial_budget", "diminishing_returns"],
) -> dict[str, Any]:
    """Save the smallest feasible measured network and an authoritative completion receipt.

    Ties favor higher CV accuracy, then earlier trials. If no model qualifies, retain the
    most accurate completed trial as a diagnostic. If none completed, metrics are null and
    no model is saved. Weights come from the winner's last CV fold, not a full-data refit.
    Completion performs no training and is idempotent.

    Args:
        reason: wall_clock or trial_budget require their limit to be consumed;
            diminishing_returns records the agent's judgment, never proof of minimality.

    Returns:
        Final output, also written to receipt.json. best-model.pt contains a CPU state_dict;
        best-model.json records its recipe, fold evidence, and evaluation protocol.
    """
    if reason not in {"wall_clock", "trial_budget", "diminishing_returns"}:
        raise ValueError("reason must be wall_clock, trial_budget, or diminishing_returns")
    context = recurse.context()
    with _locked(context.workspace / "search.lock"):
        receipt = context.workspace / "receipt.json"
        if receipt.exists():
            saved: dict[str, Any] = json.loads(receipt.read_text())
            return saved
        history = _ledger(context.workspace)
        if not history and not (context.workspace / "search.json").exists():
            raise ValueError("No attempted trials; evaluate a network before finishing")
        if reason == "trial_budget" and len(history) < context.inputs["max_trials"]:
            raise ValueError("Trial budget remains; continue or use diminishing_returns")
        if reason == "wall_clock" and time.monotonic() < _deadline(
            context.workspace, context.inputs["max_seconds"]
        ):
            raise ValueError("Wall-clock budget remains; continue or use diminishing_returns")
        best = _winner(history)
        if best is not None:
            shutil.copyfile(
                context.workspace / best["checkpoint"], context.workspace / "best-model.pt"
            )
            _write(
                context.workspace / "best-model.json",
                {
                    **best,
                    "protocol": dict(context.inputs),
                    "protocol_version": _protocol_version(
                        context.inputs, Candidate(**best["candidate"])
                    ),
                    "torch_version": torch.__version__,
                    "note": (
                        "CV measures the recipe. Saved weights are from the last fold, "
                        "not a full-data refit."
                    ),
                },
            )
        result = {
            "target_reached": best is not None and best["target_reached"],
            "cv_accuracy": best["cv_accuracy"] if best else None,
            "parameter_count": best["parameter_count"] if best else None,
            "trials_attempted": len(history),
            "stop_reason": reason,
        }
        _write(receipt, result)
        return result
