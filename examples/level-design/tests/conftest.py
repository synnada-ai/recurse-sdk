# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared test setup: load one example's agent modules at a time."""

from __future__ import annotations

import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[1]


def use_agent(agent: Path) -> None:
    """Make ``agent`` the only example agent on the import path.

    The examples reuse module names such as ``tools`` and ``builder``, so modules loaded from
    another example's agent folder are dropped before this one is imported.
    """
    for name, module in list(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if origin and EXAMPLES in Path(origin).parents and "agent" in Path(origin).parts:
            del sys.modules[name]
    sys.path[:] = [entry for entry in sys.path if not entry.endswith("/agent")]
    sys.path.insert(0, str(agent))
