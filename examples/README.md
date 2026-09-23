# Structure of a Recurse example

A runnable Recurse example is a self-contained agent application. It shows a caller's request, the choices the agent can make, the tools that measure those choices, and the result a caller can inspect. The files below are a common shape; smaller examples may keep all logic in one tool file, while larger ones may include supporting modules or evaluation utilities.

## Directory layout

```text
example/
├── README.md            # Purpose, usage, contract, loop, results, and limitations
├── agent.yaml           # Agent identity, runtime, schemas, and registered tools
├── prompt.md            # Instructions that guide the agent's decisions
├── tools.py             # Actions, measurements, and finalization exposed to the agent
├── pyproject.toml       # Python package and dependency declarations
├── uv.lock              # Pinned runtime dependencies
├── inputs/              # Optional runnable requests showing supported cases
├── tests/               # Local checks, when kept with the example
├── <domain-package>/    # Optional domain logic used by the tools
└── benchmarks/          # Optional comparisons across cases or revisions
```

File names and optional directories vary by example. The manifest points to the actual prompt and tool files, so their paths are defined by each application rather than by this diagram.

## What each part contributes

The `README.md` is the reader's entry point. It links to runnable inputs and any separate test or evaluation instructions. Its contents are described below.

The `agent.yaml` is the runtime contract. It declares the input and output shapes and connects the prompt and registered Python tools to the Recurse harness. The `prompt.md` gives the agent task-specific judgment and sequencing guidance. The `tools.py` functions perform work the agent can request and return observations it can use for the next decision.

The `pyproject.toml` and `uv.lock` describe the environment used to package and run the example. Supporting Python modules, when present, hold domain logic behind the tool interface. Checked-in files under `inputs/` show concrete requests; `tests/` exercise the behavior locally. A `benchmarks/` directory, when present, compares outcomes across cases or changes to the agent design.

## README structure

The README begins with the example's name and then moves from what it does to how its result is produced and interpreted:

| Section | What it describes |
| --- | --- |
| About | The task, what the caller supplies, what the agent creates, and the scope of the example. |
| When to use it | The situation where iterative choices and measured feedback are useful. |
| Run it | Prerequisites, a runnable request, the command to start it, and any cost or external resource requirements. |
| Contract → Inputs | The request fields, defaults, constraints, and links to sample inputs. |
| Contract → Outputs | Final status, returned values, artifacts, and what a failed run returns. |
| Loop | The agent's actions, the independent measurements, how it revises, and what ends the run. |
| Kickstart prompt | A reusable prompt that captures the example's task and acceptance criteria for building a similar agent. |
| Limitations | Unsupported cases and the limits of what the measurements establish. |

The section detail varies with the example. A small example may need only a short input description, while one with several task types may use a table of runnable requests.

## How a run moves through the example

1. A caller submits one input request through `recurse run`. The manifest supplies the schema and runtime wiring.
2. The agent reads its task and prompt, then calls tools to inspect the problem and create or change a candidate.
3. Measurement tools return scores, checks, or diagnostics. The agent uses that evidence to choose another attempt or stop.
4. A final tool checks the selected result, writes the run's receipt and artifacts, and returns an output matching the declared contract.

The exact loop depends on the task. A numerical example may compare held-out scores; a visual example may inspect renders; a puzzle example may use a solver. In each case, the result is tied to recorded observations rather than the agent's unsupported assertion.

## Outputs and evidence

The final response is a compact summary of status and result. Artifacts hold larger deliverables and the evidence behind them, such as a candidate file, report, trial history, or evaluation record. An unsuccessful run can still produce a receipt and diagnostics even when it has no accepted candidate.
