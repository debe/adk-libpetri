"""``schema.json``: every sample blueprint validates, broken ones do not, it tracks the loader."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from adk_libpetri.net import BlueprintError, parse_blueprint
from adk_libpetri.net import blueprint as B
from adk_libpetri.net.node import PetriNet

from .conftest import BLUEPRINTS

SCHEMA: dict[str, Any] = json.loads(
    resources.files("adk_libpetri.net").joinpath("schema.json").read_text(encoding="utf-8")
)
VALIDATOR = Draft202012Validator(SCHEMA)
TESTS = Path(__file__).parents[1]
PATTERN_YAML = TESTS / "demos" / "patterns" / "yaml"


def _samples() -> list[Path]:
    paths = sorted(BLUEPRINTS.rglob("*.yaml")) + sorted(PATTERN_YAML.rglob("*.yaml"))
    return [
        p
        for p in paths
        if (yaml.safe_load(p.read_text()) or {}).get("agent_class") == "adk_libpetri.net.PetriNet"
    ]


SAMPLES = _samples()


def errors(data: Any) -> list[str]:
    return [
        f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in VALIDATOR.iter_errors(data)
    ]


def test_the_schema_is_a_valid_draft_2020_12_schema() -> None:
    Draft202012Validator.check_schema(SCHEMA)


def test_there_are_samples() -> None:
    assert len(SAMPLES) >= 10


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: str(p.relative_to(TESTS)))
def test_every_sample_blueprint_validates(path: Path) -> None:
    assert not errors(yaml.safe_load(path.read_text())), errors(yaml.safe_load(path.read_text()))


MINIMAL = {
    "agent_class": "adk_libpetri.net.PetriNet",
    "name": "n",
    "transitions": {"N_Emit": {"in": ["userIn"], "out": "eventOut", "action": "emit"}},
}


def broken(**changes: Any) -> dict[str, Any]:
    data = json.loads(json.dumps(MINIMAL))
    for path, value in changes.items():
        *parents, last = path.split("__")
        d = data
        for k in parents:
            d = d.setdefault(k, {})
        d[last] = value
    return data


def test_the_minimal_net_validates() -> None:
    assert not errors(MINIMAL)


BROKEN = [
    pytest.param(broken(transitons={}), id="unknown top-level key"),
    pytest.param(broken(args={}), id="args"),
    pytest.param(broken(agent_class="LlmAgent"), id="other agent_class"),
    pytest.param(broken(nodes=["x.yaml"]), id="bare string node ref"),
    pytest.param(broken(nodes=[{"agent_class": "LlmAgent", "name": "x"}]), id="bare inline node"),
    pytest.param(broken(transitions__N_Emit__inn=["userIn"]), id="unknown transition key"),
    pytest.param(broken(transitions__N_Emit__node="x"), id="node and action"),
    pytest.param(broken(transitions__N_Emit__action="shout"), id="unknown action"),
    pytest.param(
        broken(transitions__N_Emit__in=[{"place": "userIn", "count": 2, "all": True}]),
        id="two arc forms",
    ),
    pytest.param(broken(transitions__N_Emit__out={"xor": ["eventOut"]}), id="one-branch xor"),
    pytest.param(
        broken(transitions__N_Emit__out={"and": ["a"], "xor": ["b", "c"]}), id="and plus xor"
    ),
    pytest.param(
        broken(transitions__N_Emit__out={"timeout": 5, "child": {"xor": ["a", "b"]}}),
        id="xor under timeout",
    ),
    pytest.param(broken(transitions__N_Emit__timing={"delay": 5}), id="unknown timing"),
    pytest.param(broken(transitions__N_Emit__priority="high"), id="priority not int"),
    pytest.param(broken(places={"a/b": {}}), id="slash in place name"),
    pytest.param(broken(places={"args": {}}), id="place named args"),
    pytest.param(
        broken(transitions__N_Emit__out={"xor": {"args": "eventOut", "b": "eventOut"}}),
        id="route label args",
    ),
    pytest.param(broken(places={"p": {"type": "str", "seeds": 1}}), id="unknown place key"),
    pytest.param(broken(ports={"userIn": {"place": "userIn"}}), id="port without direction"),
    pytest.param(broken(subnets={"s": {"net": "a", "stock": "router"}}), id="subnet net and stock"),
    pytest.param(broken(subnets={"s": {"net": "a", "from": "b"}}), id="from on a net subnet"),
    pytest.param(broken(subnets={"s": {"stock": "planner"}}), id="unknown stock"),
    pytest.param(broken(prove={"claims": ["deadlock-free"]}), id="misspelt claim"),
    pytest.param(
        broken(prove={"claims": [{"place_bound": {"place": "p"}}]}), id="place_bound no bound"
    ),
    pytest.param(broken(prove={"claims": [{"mutual_exclusion": ["p"]}]}), id="mutex of one place"),
    pytest.param(
        broken(prove={"claims": [{"deadlock_free": None, "place_bound": {}}]}),
        id="two claim kinds",
    ),
    pytest.param(
        broken(prove={"options": {"environment": {"userIn": {"arrival": 1}}}}),
        id="unknown environment mode",
    ),
    pytest.param(broken(prove={"options": {"sink": ["eventOut"]}}), id="unknown option"),
    pytest.param(broken(prove={"onload": True}), id="unknown prove key"),
]
"""Rejected by the schema; every one past the first five is also a loader error."""


@pytest.mark.parametrize("data", BROKEN)
def test_a_broken_blueprint_does_not_validate(data: dict[str, Any]) -> None:
    assert errors(data)


@pytest.mark.parametrize("data", BROKEN[5:])
def test_the_loader_rejects_what_the_schema_rejects(data: dict[str, Any]) -> None:
    body = {k: v for k, v in data.items() if k not in ("agent_class", "name")}
    with pytest.raises(BlueprintError):
        parse_blueprint("n", body)


def test_the_schema_names_every_key_the_loader_accepts() -> None:
    props = SCHEMA["properties"]
    defs = SCHEMA["$defs"]
    assert set(PetriNet.model_fields) | {"agent_class"} == set(props)
    assert set(B.TOP_KEYS) <= set(props)
    assert set(B._TRANSITION_KEYS) == set(defs["transition"]["properties"])
    assert set(B._OPTION_KEYS) == set(defs["proofOptions"]["properties"])
    assert set(B._STOCK) == set(defs["subnet"]["properties"]["stock"]["enum"])
    kinds = {next(iter(c["required"])) for c in defs["claim"]["oneOf"] if "required" in c}
    assert kinds == set(B._CLAIMS)
    type_doc = defs["placeDecl"]["oneOf"][1]["properties"]["type"]["description"]
    assert all(alias in type_doc for alias in B.TYPE_ALIASES)
    places_doc = props["places"]["description"]
    assert all(name in places_doc for name in B.CATALOG)
