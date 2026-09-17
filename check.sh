#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
uv sync --locked --group dev
uv run --frozen --no-sync --group dev ruff format --check .
uv run --frozen --no-sync --group dev ruff check .
uv run --frozen --no-sync --group dev mypy
uv run --frozen --no-sync --group dev pytest -n auto --cov --cov-branch --cov-report=term-missing -q
uv build --no-sources
uv run --directory examples/rna-fold-lab/tests --locked \
  pytest --cov --cov-branch --cov-report=term-missing -q
MYPYPATH=../../../src uv run --directory examples/rna-fold-lab/tests --locked \
  mypy --config-file ../../../pyproject.toml ../rna_fold_tools.py test_rna_fold_lab.py
uv run --directory examples/backpack-3d/tests --locked \
  pytest --cov --cov-branch --cov-report=term-missing -q
MYPYPATH=../../../src uv run --directory examples/backpack-3d/tests --locked \
  mypy --config-file pyproject.toml ../tools.py test_backpack.py
uv run --directory examples/predictive-modeler/tests --locked \
  pytest --cov --cov-branch --cov-report=term-missing -q
MYPYPATH=../../../src uv run --directory examples/predictive-modeler/tests --locked \
  mypy --config-file pyproject.toml ../modeler ../tools.py ../benchmarks .
