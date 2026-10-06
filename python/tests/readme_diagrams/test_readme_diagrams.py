"""The README's Python-net diagrams, exported from compiled workflows.

Each diagram is a view (a named subset of the real transitions) of a net that
``compile_workflow`` builds from a sample in ``tests/workflow/samples.py``.
A rename fails here, not silently in the README. Post-processing matches
``ReadmeDiagramsTest`` on the Java side; see ``_dot.py``.

Regenerate after changing the compiler or a sample::

    READMEDIAGRAMS_WRITE=1 pytest tests/readme_diagrams
    cd ../docs/diagrams && npm run build   # dot -Tsvg over every dot/*.dot

The check compares DOT text only, so CI needs no graphviz.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from google.adk.agents.config_agent_utils import from_config

from adk_libpetri import cli
from adk_libpetri import colours as C
from adk_libpetri.net import PetriNet
from adk_libpetri.workflow import compile_workflow
from adk_libpetri.workflow.compiler import CompiledWorkflow
from support.smt_proofs import requires_z3
from workflow import samples

from ._dot import Diagram, render

WRITE = os.environ.get("READMEDIAGRAMS_WRITE") == "1"
DOT_DIR = Path(__file__).resolve().parents[3] / "docs" / "diagrams" / "dot"
HERO_SRC = Path(__file__).resolve().parent / "hero"
HERO_OUT = DOT_DIR.parent / "hero"


@contextlib.contextmanager
def _hero_on_path() -> Iterator[None]:
    """ADK resolves ``.agent.fast`` in ``hero/race.yaml`` as ``hero.agent.fast``,
    imported from ``sys.path``, as ``adk run`` does from the agents' folder."""
    sys.path.insert(0, str(HERO_SRC.parent))
    try:
        yield
    finally:
        sys.path.remove(str(HERO_SRC.parent))


REGENERATE = "regenerate with READMEDIAGRAMS_WRITE=1 pytest tests/readme_diagrams"
BUDGET_K = 3


def _seeds(cw: CompiledWorkflow, view: tuple[str, ...]) -> dict[str, str]:
    """``●`` on every place the runner seeds that the view draws."""
    spec = cw.spec
    drawn = {p.name for n in view for p in spec.transition(n).places()}
    return {p: "●" if k == 1 else f"●×{k}" for p, k in cw.initial_counts().items() if p in drawn}


def diagrams() -> list[Diagram]:
    router = compile_workflow(samples.router())
    router_view = (
        "Wf_Start",
        "Wf_classify_Run",
        "Wf_handle_bug_Run",
        "Wf_handle_other_Run",
        # One of the two terminal ends; Wf_EndTurnOutput_handle_other mirrors it.
        "Wf_EndTurnOutput_handle_bug",
    )
    looping = compile_workflow(
        samples.looping(), back_edge_budget={("counter", "counter"): BUDGET_K}
    )
    loop_view = (
        "Wf_Start",
        "Wf_counter_Run",
        "Wf_Edge_counter_counter",
        "Wf_Edge_counter_counter_Exhausted",
        "Wf_finish_Run",
    )
    # Wf_Start puts K budget tokens on the place each turn, as the Java reask
    # budget's BuildPrompt does; the diagram marks it like reask-budget.dot.
    loop_seeds = _seeds(looping, loop_view) | {"wf/budget/counter->counter": "●×K"}
    with _hero_on_path():
        hero = from_config(str(HERO_SRC / "race.yaml"))
    assert isinstance(hero, PetriNet)
    return [
        Diagram(
            "hero-race",
            "tests/readme_diagrams/hero/race.yaml, the whole net",
            hero.spec,
            tuple(hero.spec.transition_names),
            frozenset({C.USER_IN.name}),
            {},
            free_tests=True,
        ),
        Diagram(
            "workflow-router",
            "compile_workflow(samples.router()), view of the routed turn",
            router.spec,
            router_view,
            frozenset({C.USER_IN.name}),
            _seeds(router, router_view),
        ),
        Diagram(
            "workflow-back-edge-budget",
            "compile_workflow(samples.looping(), back_edge_budget={('counter', 'counter'): 3}),"
            " view of the budgeted back edge",
            looping.spec,
            loop_view,
            frozenset({C.USER_IN.name}),
            loop_seeds,
        ),
    ]


@pytest.mark.parametrize("diagram", diagrams(), ids=lambda d: d.name)
def test_readme_diagram_matches_the_compiled_net(diagram: Diagram) -> None:
    dot = render(diagram)
    golden = DOT_DIR / f"{diagram.name}.dot"
    if WRITE:
        DOT_DIR.mkdir(parents=True, exist_ok=True)
        golden.write_text(dot, encoding="utf-8")
        return
    if not golden.exists():
        pytest.fail(f"{golden} is missing; {REGENERATE}")
    assert golden.read_text(encoding="utf-8") == dot, (
        f"{diagram.name}.dot drifted from {diagram.source}; {REGENERATE}"
    )


def _golden(path: Path, text: str, source: str) -> None:
    if WRITE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    if not path.exists():
        pytest.fail(f"{path} is missing; {REGENERATE}")
    drifted = f"{path.name} drifted from {source}; {REGENERATE}"
    assert path.read_text(encoding="utf-8") == text, drifted


_KEEP = re.compile(r"^(PROVEN|VIOLATED|UNKNOWN) |^  fires: |^  markings:$|^\d+ proven, ")
_MARKING = re.compile(r"^    \d+: \{")


def verify_excerpt(path: Path) -> str:
    """``adk-libpetri verify`` output, verbatim, cut to verdicts, firings and
    each counterexample's last marking (``…`` marks the markings left out).

    The README hero shows these lines; the report and the path line are left out.
    """
    out = io.StringIO()
    with _hero_on_path():
        cli.main(["verify", str(path)], out=out)
    kept: list[str] = []
    markings: list[str] = []
    for line in out.getvalue().splitlines():
        if _MARKING.match(line):
            markings.append(line.rstrip())
            continue
        if markings:
            kept += ["    …", markings[-1]] if len(markings) > 1 else markings
            markings = []
        if _KEEP.match(line):
            kept.append(line.rstrip())
    return "\n".join(kept) + "\n"


@requires_z3
@pytest.mark.parametrize("name", ["race", "race_broken"])
def test_hero_verify_output_matches_the_cli(name: str) -> None:
    """The hero's right panel is the CLI's own output for the file it shows."""
    path = HERO_SRC / f"{name}.yaml"
    source = f"adk-libpetri verify {path.name}"
    _golden(HERO_OUT / f"verify-{name}.txt", verify_excerpt(path), source)


def test_hero_yaml_is_copied_verbatim() -> None:
    """The hero's left panel is the file the net and the verdicts come from."""
    for name in ("race", "race_broken"):
        src = HERO_SRC / f"{name}.yaml"
        _golden(HERO_OUT / f"{name}.yaml", src.read_text(encoding="utf-8"), str(src.name))


def test_the_broken_hero_differs_by_one_arc() -> None:
    good = (HERO_SRC / "race.yaml").read_text(encoding="utf-8").splitlines()
    bad = (HERO_SRC / "race_broken.yaml").read_text(encoding="utf-8").splitlines()
    diff = [(a, b) for a, b in zip(good, bad, strict=True) if a != b]
    assert diff == [
        ("name: race", "name: race_broken"),
        ("    in: [done, permit]", "    in: [done]"),
    ]
    assert good[good.index("    in: [done, permit]") - 1] == "  Race_Commit:"


def test_back_edge_budget_view_carries_the_reask_budget_shape() -> None:
    """Spot check: priority edge, inhibitor fallback, K tokens refilled by Wf_Start."""
    d = next(d for d in diagrams() if d.name == "workflow-back-edge-budget")
    dot = render(d)
    assert 'label="Wf_Edge_counter_counter prio=10"' in dot
    assert 'label="Wf_Edge_counter_counter_Exhausted prio=-10"' in dot
    budget = "p_wf_budget_counter__counter"
    assert f'{budget} -> t_Wf_Edge_counter_counter_Exhausted [color="#dc3545"' in dot
    assert 'arrowhead="odot"' in dot
    looping = compile_workflow(
        samples.looping(), back_edge_budget={("counter", "counter"): BUDGET_K}
    )
    assert looping.budgets == {("counter", "counter"): BUDGET_K}
