"""The compute-only quickstart is executable and packaged without agent artifacts."""

import importlib.util
from pathlib import Path

import jsonschema
import pytest

import recurse

EXAMPLE = Path(__file__).parents[1] / "examples" / "echo"


@pytest.mark.parametrize("message", ["", "Hello from my private tool", "Unicode: 你好"])
def test_echo_returns_schema_valid_output(message: str) -> None:
    """The real documented function preserves empty, ordinary and Unicode strings."""
    spec = importlib.util.spec_from_file_location("echo_example", EXAMPLE / "echo_tools.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = module.echo(message)
    assert output == {"message": message}
    manifest = recurse.load_tools_manifest(EXAMPLE)
    function = manifest["tools"]["register"]["echo"]
    jsonschema.Draft202012Validator(function["output_schema"]).validate(output)
    assert function.get("apis", []) == []


def test_echo_builds_the_standard_private_source_bundle() -> None:
    """The quickstart uses the existing Tools builder and needs no agent manifest."""
    artifacts, record = recurse.build_tools_bundle(EXAMPLE)
    assert set(artifacts) == {"source"}
    assert record["apiVersion"] == "recurse.tools.application/v1alpha1"
    assert record["source"]["size_bytes"] == len(artifacts["source"])
    assert not (EXAMPLE / "agent.yaml").exists()
