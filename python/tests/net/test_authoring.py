"""Every YAML block in ``AUTHORING.md`` is a file that loads, and its claims are proven.

Each ``yaml`` block starts with ``# file: <name>.yaml``. The blocks are written
into one agent folder with an ``agent.py`` that defines every ``.agent.<fn>``
they name, then each goes through ``adk-libpetri check`` (and ``verify``), so
the guide an agent copies from cannot drift from the loader.
"""

from __future__ import annotations

import io
import re
from importlib import resources
from pathlib import Path

import pytest
import yaml

from adk_libpetri.cli import main
from support.smt_proofs import requires_z3

GUIDE = resources.files("adk_libpetri.net").joinpath("AUTHORING.md").read_text(encoding="utf-8")
BLOCKS = re.findall(r"^```yaml\n(.*?)^```", GUIDE, flags=re.S | re.M)
FILE_LINE = re.compile(r"\A# file: (\S+\.yaml)\n")
PACKAGE = "cli_authoring_snippets"


def _files() -> dict[str, str]:
    files: dict[str, str] = {}
    for block in BLOCKS:
        m = FILE_LINE.match(block)
        assert m, f"a yaml block in AUTHORING.md must start with '# file: <name>.yaml':\n{block}"
        assert m.group(1) not in files, f"two blocks write {m.group(1)}"
        files[m.group(1)] = block
    return files


FILES = _files()
NETS = sorted(
    name
    for name, text in FILES.items()
    if yaml.safe_load(text).get("agent_class") == "adk_libpetri.net.PetriNet"
)


@pytest.fixture(scope="module")
def folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``<agents dir>/cli_authoring_snippets/``, holding every block and its ``agent.py``."""
    d = tmp_path_factory.mktemp("agents") / PACKAGE
    d.mkdir()
    (d / "__init__.py").write_text("")
    fns = sorted({fn for text in FILES.values() for fn in re.findall(r"\.agent\.(\w+)", text)})
    (d / "agent.py").write_text(
        "from typing import Any\n\n"
        + "".join(
            f"\n\ndef {fn}(node_input: Any = None) -> Any:\n    return node_input\n" for fn in fns
        )
    )
    for name, text in FILES.items():
        (d / name).write_text(text)
    return d


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), out=out)
    return code, out.getvalue()


def test_the_guide_has_every_motif() -> None:
    assert {"race.yaml", "quorum.yaml", "retry.yaml", "optimistic.yaml", "escalate.yaml"} <= set(
        FILES
    )
    assert len(NETS) >= 7


@pytest.mark.parametrize("name", NETS)
def test_each_guide_block_checks(folder: Path, name: str) -> None:
    code, out = run("check", str(folder / name))
    assert code == 0, out
    assert out.startswith("OK "), out


@requires_z3
@pytest.mark.parametrize("name", NETS)
def test_each_guide_block_is_proven(folder: Path, name: str) -> None:
    code, out = run("verify", str(folder / name), "--recursive")
    assert code == 0, out
    assert "VIOLATED" not in out and "UNKNOWN" not in out, out


def test_the_blocks_match_the_schema() -> None:
    import json

    from jsonschema import Draft202012Validator

    schema = json.loads(
        resources.files("adk_libpetri.net").joinpath("schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    for name in NETS:
        errors = [e.message for e in validator.iter_errors(yaml.safe_load(FILES[name]))]
        assert not errors, (name, errors)
