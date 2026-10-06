"""What ADK's loader (and ``adk web``) do with a blueprint at its edges, as load errors."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from google.adk.agents import config_agent_utils
from google.adk.agents.config_agent_utils import from_config

from adk_libpetri.cli import main
from adk_libpetri.net import BlueprintError, PetriNet, parse_blueprint

from ._harness import session, text_of
from .conftest import BLUEPRINTS, Serve


def write(tmp_path: Path, body: str, name: str = "net.yaml") -> Path:
    f = tmp_path / name
    f.write_text(textwrap.dedent(body))
    return f


def cli(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(args), out=out)
    return code, out.getvalue()


HEAD = """\
agent_class: adk_libpetri.net.PetriNet
name: probe
"""
BODY = """\
transitions:
  P_Emit: {in: [userIn], out: eventOut, action: emit}
"""


# ----------------------------------------------------------------------------
#  nodes: entries
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "hint"),
    [
        ("- bp_turns.agent.later_fn", "write - [bp_turns.agent.later_fn]"),
        ("- {agent_class: LlmAgent, name: helper}", "write - [{agent_class: LlmAgent, ...}]"),
    ],
)
def test_a_bare_nodes_entry_is_one_error_at_its_index(
    tmp_path: Path, entry: str, hint: str
) -> None:
    f = write(tmp_path, HEAD + "nodes:\n  - [bp_turns.agent.slow]\n  " + entry + "\n" + BODY)
    with pytest.raises(BlueprintError) as err:
        from_config(str(f))
    assert err.value.path == "nodes[1]"
    assert err.value.hint is not None and err.value.hint.startswith(hint)
    code, out = cli("check", str(f))
    assert code == 1 and out.count("\n") == 1, out


async def test_inline_nodes_in_lists_and_a_child_one_directory_down_run(serve: Serve) -> None:
    node = from_config(str(BLUEPRINTS / "bp_inline" / "root.yaml"))
    assert isinstance(node, PetriNet)
    serve(node)
    for item in node.nodes:
        for el in item if isinstance(item, list | tuple) else (item,):
            if isinstance(el, PetriNet):
                serve(el)
    s = await session(node)
    t = await s.say("hello")
    assert t.error is None, t.error
    assert text_of(t.events[-1]) == "llm says hi"


def test_a_leading_dot_one_directory_below_the_agents_package_does_not_import() -> None:
    # adk web puts only the agents dir on sys.path; bp_nested/bp_mid/mid.yaml's
    # `.agent.fn` means the package `bp_mid`, which is not importable from there.
    # (The other tests put the nested directories on sys.path themselves.)
    code = textwrap.dedent(
        f"""
        import sys, warnings
        warnings.simplefilter("ignore")
        sys.path.insert(0, {str(BLUEPRINTS)!r})
        from google.adk.agents.config_agent_utils import from_config
        try:
            from_config({str(BLUEPRINTS / "bp_nested" / "top.yaml")!r})
        except Exception as err:
            print("FAILED", err)
        """
    )
    env = {**os.environ, "PYTHONPATH": ""}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd="/"
    ).stdout
    assert "FAILED" in out and "Invalid fully qualified name" in out, out


# ----------------------------------------------------------------------------
#  Keys adk web refuses, and pydantic's own errors
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "path"),
    [
        (
            {
                "places": {"args": {"type": "str"}},
                "transitions": {"T": {"in": ["userIn"], "out": "eventOut", "action": "emit"}},
            },
            "places.args",
        ),
        (
            {
                "places": {"a": {}, "b": {}},
                "transitions": {"T": {"in": ["userIn"], "out": {"xor": {"args": "a", "x": "b"}}}},
            },
            "transitions.T.out.xor.args",
        ),
    ],
)
def test_a_key_named_args_is_a_load_error_as_in_adk_web(data: dict[str, Any], path: str) -> None:
    with pytest.raises(BlueprintError) as err:
        parse_blueprint("n", data)
    assert err.value.path == path
    assert "adk web" in err.value.message


def test_check_agrees_with_adk_webs_key_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = write(
        tmp_path,
        HEAD + "places:\n  args: {type: str}\n"
        "transitions:\n  T_In: {in: [userIn], out: eventOut, action: emit}\n",
    )
    code, out = cli("check", str(f))
    assert code == 1 and "places.args" in out
    monkeypatch.setattr(config_agent_utils, "_ENFORCE_YAML_KEY_DENYLIST", True)
    with pytest.raises(ValueError, match="Blocked key 'args'"):
        from_config(str(f))


def test_a_pydantic_error_is_one_check_line_with_a_fix(tmp_path: Path) -> None:
    f = write(tmp_path, HEAD.replace("name: probe", "name: probe-net") + BODY)
    code, out = cli("check", str(f))
    assert code == 1
    assert out.count("\n") == 1, out
    assert out.startswith("ERROR ") and ": name: Node name 'probe-net'" in out
    assert "Fix: use a Python identifier" in out


# ----------------------------------------------------------------------------
#  Messages that name the fix
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("transitions", "path", "hint"),
    [
        (
            {
                "M_Start": {"in": ["userIn"], "out": "vote", "node": "f"},
                "M_Merge": {"in": [{"place": "vote", "count": 2}], "out": "merged"},
            },
            "transitions.M_Merge.in",
            "a count, at_least or all arc hands a list",
        ),
        (
            {"M_Run": {"in": ["userIn"], "out": "eventOut", "node": "f", "action": "emit"}},
            "transitions.M_Run",
            "drop action:",
        ),
        (
            {"M_Split": {"in": ["userIn"], "out": ["vote", "merged"], "node": "f"}},
            "transitions.M_Split.out",
            "{and: [vote, merged]}",
        ),
    ],
)
def test_common_slips_get_a_specific_fix(transitions: dict[str, Any], path: str, hint: str) -> None:
    from google.adk.workflow import FunctionNode

    def f(node_input: Any = None) -> str:
        return "x"

    data = {
        "places": {"vote": {"type": "str"}, "merged": {"type": "str"}},
        "transitions": transitions,
    }
    with pytest.raises(BlueprintError) as err:
        parse_blueprint("n", data, nodes={"f": FunctionNode(func=f)})
    assert err.value.path == path
    assert err.value.hint is not None and hint in err.value.hint, err.value.hint
