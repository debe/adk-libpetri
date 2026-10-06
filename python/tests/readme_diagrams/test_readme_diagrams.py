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

import os
from pathlib import Path

import pytest

from adk_libpetri import colours as C
from adk_libpetri.workflow import compile_workflow
from adk_libpetri.workflow.compiler import CompiledWorkflow
from workflow import samples

from ._dot import Diagram, render

WRITE = os.environ.get("READMEDIAGRAMS_WRITE") == "1"
DOT_DIR = Path(__file__).resolve().parents[3] / "docs" / "diagrams" / "dot"
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
    return [
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
