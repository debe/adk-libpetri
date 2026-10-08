"""``adk-libpetri check`` and ``verify``: what an authoring agent sees, and the exit codes."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from adk_libpetri.cli import main
from support.smt_proofs import requires_z3

from .conftest import BLUEPRINTS

CLI = BLUEPRINTS / "cli_recursive"
RACE = BLUEPRINTS / "bp_race"


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), out=out)
    return code, out.getvalue()


# -- check ----------------------------------------------------------------------


def test_check_prints_one_ok_line_for_a_good_file() -> None:
    code, out = run("check", str(RACE / "race.yaml"))
    assert code == 0
    assert out == (
        f"OK {RACE / 'race.yaml'}: PetriNet 'speculative_race': 9 places, 7 transitions, "
        "0 subnets, 4 claims\n"
    )


def test_check_loads_children_and_counts_subnets() -> None:
    code, out = run("check", str(CLI / "parent.yaml"))
    assert code == 0, out
    assert "1 subnets, 2 claims" in out


def test_check_prints_the_blueprint_error_with_its_key_path_and_hint() -> None:
    code, out = run("check", str(CLI / "typo.yaml"))
    assert code == 1
    assert out.count("\n") == 1, out
    assert out.startswith("ERROR ")
    assert "typo.yaml: transitions.Typo_Emit.out.and[1]: unknown place 'wonn'" in out
    assert "Fix: did you mean 'won'?" in out


def test_check_reports_a_ref_cycle() -> None:
    code, out = run("check", str(BLUEPRINTS / "bp_cycle" / "a.yaml"))
    assert code == 1
    assert "nodes: ref cycle: a.yaml -> b.yaml -> a.yaml" in out


def test_check_rejects_a_file_that_is_not_a_petri_net() -> None:
    code, out = run("check", str(BLUEPRINTS / "bp_llm" / "helper.yaml"))
    assert code == 1
    assert "defines a LlmAgent, not a PetriNet" in out


def test_check_reports_a_missing_file(tmp_path: Path) -> None:
    code, out = run("check", str(tmp_path / "nope.yaml"))
    assert code == 1
    assert "no such file" in out


def test_check_puts_the_agents_dir_on_sys_path_as_adk_web_does(tmp_path: Path) -> None:
    pkg = tmp_path / "cli_sys_path_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "agent.py").write_text("def shout(node_input=None):\n    return 'HI'\n")
    (pkg / "root_agent.yaml").write_text(
        "agent_class: adk_libpetri.net.PetriNet\n"
        "name: shouter\n"
        "nodes: [[.agent.shout]]\n"
        "places: {loud: {type: str}}\n"
        "transitions:\n"
        "  S_Shout: {in: [userIn], out: loud, node: shout}\n"
        "  S_Emit: {in: [loud], out: eventOut, action: emit}\n"
    )
    assert str(tmp_path) not in sys.path
    code, out = run("check", str(pkg / "root_agent.yaml"))
    assert code == 0, out
    assert str(tmp_path) not in sys.path


# -- verify ---------------------------------------------------------------------


@requires_z3
def test_verify_prints_a_line_per_claim_and_exits_0_when_all_are_proven() -> None:
    code, out = run("verify", str(RACE / "race.yaml"))
    assert code == 0, out
    lines = out.splitlines()
    assert lines[0].startswith("verify speculative_race (")
    one = "  under: userIn: the 1 token(s) initial_marking seeds, no further turn"
    assert lines[1:] == [
        "PROVEN   one commit per turn  [place_bound]",
        one,
        "PROVEN   place_bound(eventOut, 1)  [place_bound]",
        one,
        "PROVEN   deadlock_free  [deadlock_free]",
        one,
        "PROVEN   permit never stacks  [place_bound]",
        "  under: userIn: exactly 2 arrival(s) (userIn in any order, not turn by turn)",
        "  note: assumes every firing is one step, node runs included (assume_atomic_nodes)",
        "4 proven, 0 violated, 0 unknown",
    ]


@requires_z3
def test_verify_exits_1_and_prints_the_counterexample_of_a_violated_claim() -> None:
    code, out = run("verify", str(RACE / "race_inhibitor.yaml"))
    assert code == 1
    assert "VIOLATED place_bound(raceWon, 1)  [place_bound]" in out
    assert (
        "  fires: Race_CommitA -> Race_CommitB -> complete:Race_CommitA -> complete:Race_CommitB"
        in out
    )
    assert "  markings:\n    0: {branchADone: 1, branchBDone: 1}" in out
    assert "4: {eventOut: 2, raceWon: 2}" in out
    assert "  report:" in out
    assert out.rstrip().endswith("0 proven, 1 violated, 0 unknown")


@requires_z3
def test_verify_k_changes_the_number_of_user_inputs() -> None:
    code, out = run("verify", str(CLI / "parent.yaml"))
    assert code == 0, out
    assert "under: 1 turn" in out
    code, out = run("verify", str(CLI / "parent.yaml"), "--k", "2")
    # Two turns, each input after the previous answer: still one answer a turn.
    assert code == 0, out
    assert "PROVEN   place_bound(eventOut, 1)" in out
    assert "under: 2 turns, each input after the previous answer" in out


def test_verify_reports_a_load_error_like_check() -> None:
    code, out = run("verify", str(CLI / "typo.yaml"))
    assert code == 1
    assert out.startswith("ERROR ") and "unknown place 'wonn'" in out


def test_verify_without_claims_says_so() -> None:
    code, out = run("verify", str(BLUEPRINTS / "bp_basic" / "triage.yaml"))
    assert code == 0
    assert "  no claims under prove:" in out
    assert "0 proven, 0 violated, 0 unknown" in out


@requires_z3
def test_verify_recursive_also_proves_each_mounted_child_alone() -> None:
    code, out = run("verify", str(CLI / "parent.yaml"))
    assert code == 0, out
    assert "child one winner" not in out
    code, out = run("verify", str(CLI / "parent.yaml"), "--recursive")
    assert code == 0, out
    assert "verify child_race (" in out
    assert "PROVEN   child one winner  [place_bound]" in out
    assert "3 proven, 0 violated, 0 unknown" in out


@requires_z3
def test_verify_recursive_fails_on_a_childs_own_violated_claim() -> None:
    code, out = run("verify", str(CLI / "parent_bad.yaml"))
    assert code == 0, out
    code, out = run("verify", str(CLI / "parent_bad.yaml"), "--recursive")
    assert code == 1
    assert "verify child_bad_race (" in out
    assert "VIOLATED child one winner  [place_bound]" in out
    assert "  fires: " in out


@requires_z3
def test_verify_recursive_verifies_a_child_mounted_twice_once() -> None:
    code, out = run("verify", str(BLUEPRINTS / "bp_compose" / "twice.yaml"), "--recursive")
    assert code == 0, out
    assert out.count("verify two_way_race (") == 1


def test_bad_usage_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in ([], ["verify"], ["verify", "x.yaml", "--k", "0"], ["prove", "x.yaml"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2
    capsys.readouterr()


# -- packaged files and the console script ------------------------------------------


def test_guide_and_schema_print_the_packaged_files() -> None:
    code, out = run("guide")
    assert code == 0 and out.startswith("# Authoring net blueprints")
    code, out = run("schema")
    assert code == 0
    assert json.loads(out)["title"] == "adk-libpetri net blueprint"


def test_pyproject_declares_the_console_script_and_ships_the_package_data() -> None:
    pyproject = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    assert pyproject["project"]["scripts"]["adk-libpetri"] == "adk_libpetri.cli:main"
    artifacts = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["artifacts"]
    assert "src/adk_libpetri/net/schema.json" in artifacts
    assert "src/adk_libpetri/net/AUTHORING.md" in artifacts


def test_the_module_runs_as_a_program() -> None:
    ok = subprocess.run(
        [sys.executable, "-m", "adk_libpetri.cli", "check", str(RACE / "race.yaml")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert ok.stdout.startswith("OK ")
    bad = subprocess.run(
        [sys.executable, "-m", "adk_libpetri.cli", "check", str(CLI / "typo.yaml")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert bad.returncode == 1
    assert "unknown place 'wonn'" in bad.stdout


# -- --json -----------------------------------------------------------------------


def test_check_json_names_the_key_and_the_fix() -> None:
    code, out = run("check", "--json", str(CLI / "typo.yaml"))
    assert code == 1
    report = json.loads(out)
    assert report["ok"] is False
    assert report["key_path"].startswith("transitions.Typo_Emit")
    good_code, good = run("check", "--json", str(RACE / "race.yaml"))
    assert good_code == 0
    assert json.loads(good)["net"]["name"] == "speculative_race"


@requires_z3
def test_verify_json_matches_the_text_verdicts() -> None:
    path = str(CLI / "parent_bad.yaml")
    text_code, text = run("verify", "--recursive", path)
    json_code, out = run("verify", "--recursive", "--json", path)
    assert json_code == text_code
    report = json.loads(out)
    assert f"{report['proven']} proven, {report['violated']} violated" in text
    for claim in report["claims"]:
        assert f"{claim['verdict'].upper():<9}{claim['label']}" in text
        if claim["verdict"] == "violated":
            assert "  fires: " + " -> ".join(claim["fires"]) in text
