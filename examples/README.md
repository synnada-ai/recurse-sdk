# Structure of a Recurse example

A runnable Recurse example is a complete agent application. It shows:
- The caller's request; i.e. the agent's input contract.
- Choices the agent can make; i.e. the action tools and prompt instructions.
- Tools that measure those choices; i.e. the verifiers.
- The result a caller can inspect; i.e. the completion tool and any artifact-generation tools.

The files below are a common shape; smaller examples may keep all logic in one tool file, while larger ones may include supporting modules or evaluation utilities.

## Directory layout

```text
example/
├── README.md            # Purpose, usage, contract, loop, kickstart prompt, results, and limitations
├── agent.yaml           # Agent identity, runtime, configuration settings, input/output schemas, and tools
├── prompt.md            # Instructions that guide the agent's decisions
├── tools.py             # Action, verifier, and finalization tools the agent will use
├── pyproject.toml       # Python package and dependency declarations
├── uv.lock              # Runtime dependencies
├── inputs/              # Optional runnable requests showing example cases/inputs
├── tests/               # Local checks and unit tests for tools
├── impl/                # Optional implementation package tool functions rely on
└── evals/               # Independent evaluation collateral 
```

Not all examples may require an `impl` package; a single `tools.py` file may be more appropriate if collecting all Python code in one file doesn't surpass 1000 lines. Simple examples with small input/output snippets may place them directly in the `README.md` file instead of having an `inputs/` directory. Almost all examples, except very trivial ones, should contain an `evals/` directory.

## What each part contributes

The `README.md` file is the reader's entry point. It links to (or contains) runnable inputs, test instructions, and how evaluation works.

### README structure

The README begins with the example's name and then moves from what it does to how it works:

| Section | What it describes |
| --- | --- |
| About | The task, what the caller supplies, what the agent creates, and the scope of the example. |
| When to use it | The situations that call for an approach the example demonstrates; why iterative refinement is useful. |
| Running the example | Prerequisites, a runnable request, the command to start a run, and any cost or external resource requirements. |
| Input contract | The request fields, defaults, constraints, and links to sample inputs. |
| Output contract | Final status, agent's return value(s), artifacts. |
| Loop | The agent's actions, independent measurement tools and verifiers, how refinement works, and what ends the run. |
| Kickstart prompt | A reusable prompt that captures the example, its acceptance criteria, and any relevant instructions for building a similar agent. |
| Limitations | Unsupported cases and the limits of what the measurements establish. |

The section detail varies with the example. A small example may require only a short input description, while one with several task types may use a table of runnable requests.

### The manifest

The `agent.yaml` file contains the agent manifest and defines the runtime contract. It declares the input and output shapes, and connects the prompt and Python tools to the Recurse harness.

### The prompt

The `prompt.md` file contains the system prompt for the agent, see [prompting guide](https://recurse.run/SKILL.md#prompting) for details.

### Tools

The `tools.py` file contains the actual functions that perform work the agent can request, and return observations it can use to drive its the next decision.

### Packaging

The `pyproject.toml` and `uv.lock` files describe the environment for packaging purposes and running the example. Supporting Python modules in the `impl/` directory, when present, contain domain logic implementing the tool interface. When present, the `inputs/` directory collects concrete request examples; `tests/` exercise the behavior locally. An `evals/` directory, when present, compares any evaluation collateral to measure or quantify the agent's performance.

## How a run moves through the example

1. A caller submits one input request through `recurse run`. The manifest supplies the schema and runtime wiring.
2. The agent starts with the system prompt, reads its inputs, and then begins an iterative refinement loop where it can calls tools to inspect the problem and create or change a candidate.
3. Measurement tools return scores, checks, or diagnostics. The agent uses that evidence to choose another attempt or stop.
4. A final termination tool checks the candidate result and decides loop convergence. If so, it writes the run's receipt and artifacts, and returns an output matching the output contract.

The exact loop depends on the example. A numerical predictive example may compare held-out scores; a visual example may inspect renders; a puzzle example may use a solver. In each case, the agent should always justify a result with observations or verifiers rather than arbitrary judgement.

## Outputs and evidence

The final response is a compact summary of status and result. Artifacts hold larger deliverables and the evidence behind them, such as a candidate file, report, trial history, or evaluation summary. An unsuccessful run can still produce a receipt and diagnostics even when it doesn't produce an acceptable candidate.
