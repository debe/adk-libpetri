"""ADK's Agent Builder Assistant, taught to write Petri-net blueprints (``@experimental``).

ADK's dev UI has an LLM assistant (``__adk_agent_builder_assistant``) that
writes ``root_agent.yaml`` files for the app being edited. It knows ADK's
AgentConfig schema, and its ``write_config_files`` check passes a blueprint
with any content (``PetriNet``'s keys are extra keys to that schema). This
module keeps ADK's assistant and adds what a net needs:

* ``petri_authoring_guide`` and ``petri_schema``: ``AUTHORING.md`` (by
  section) and the blueprint JSON Schema;
* ``write_petri_blueprints``: writes YAML files only when every blueprint
  among them loads through ADK's loader and builds (else nothing is written,
  and each error names its key path and fix);
* ``check_petri_blueprint`` and ``verify_petri_blueprint``: the CLI's
  ``check`` and ``verify`` as tool results, a violated claim with its
  counterexample as ``steps`` (one line per firing, with the marking after
  it) and, under ``adk-libpetri web``, a ``picture``: an image line the
  dev UI shows inline (:mod:`adk_libpetri.web.proof_view`).

The instruction gains the loop: write, check, verify, fix from the
counterexample; a net is not done while a claim is violated or unknown.

``adk-libpetri web`` serves it in place of ADK's own assistant
(:class:`~adk_libpetri.web.loader.PetriAgentLoader`), so the dev UI's builder
uses it. Under stock ``adk web``, serve it as an app of its own::

    # agents/petri_builder/agent.py
    from adk_libpetri.web.builder import create_petri_builder_assistant
    root_agent = create_petri_builder_assistant()

The tools resolve paths the way ADK's assistant does: against the session's
``root_directory`` state (the dev UI sets ``<app>/tmp/<app>``), relative to
the server's working directory, and never outside that root.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import re
import threading
from collections import OrderedDict
from dataclasses import replace
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .._experimental import experimental
from ..net.counterexample import step_lines, steps, violation
from ..net.report import CheckReport, ClaimResult, VerifyReport, check_file, verify_file
from . import proof_view
from .staging import staged

logger = logging.getLogger(__name__)

ADK_ASSISTANT = "__adk_agent_builder_assistant"
"""The app name the dev UI's builder chats with."""

PETRI_CLASS = "adk_libpetri.net.PetriNet"

INSTRUCTION_ADDENDUM = f"""

# Petri-net blueprints (adk-libpetri)

Besides ADK's own agent classes, this project can write a Petri net as an
agent config: `agent_class: {PETRI_CLASS}`. Choose a net when the flow needs
what ADK's workflow agents cannot state: a race where only the first result
counts, a quorum (answer at k of n), a permit or budget, an inhibitor
fallback, or a timeout. For a plain sequence, loop or fan-out, keep ADK's
own agents.

Before writing a net, call `petri_authoring_guide` (the loop, the format,
the motifs) and start from the closest motif. `petri_schema` gives the exact
shape.

Writing a net is a loop. Never call a net done while a claim is violated or
unknown:

1. Write the files with `write_petri_blueprints` (not `write_config_files`:
   that check does not understand a PetriNet). It writes nothing unless every
   blueprint loads, and each error names the YAML key path and the fix.
2. Put the properties the net must have under `prove:` (at least
   `deadlock_free`, and a `place_bound` on `eventOut` of 1 for one answer per
   turn), then call `verify_petri_blueprint`.
3. A violated claim comes with a counterexample: its `steps`, one line per
   firing with the marking after it, the last saying what breaks. Find the
   step where the bad marking appears; that transition's arcs are the bug.
   When the claim has a `picture` (an `<a><img></a>` line), put that line in
   your reply exactly as given, on a line of its own and not in a code
   block: the user sees the net at the bad step and every step as a picture.
   Never write an image or link of your own. Quote the two or three steps
   that matter, then fix the net (or ask the user) and verify again.
4. End every reply in which you wrote or verified a net with its state, in
   plain words taken from the tool results:
   - the net: its name and size (the `summary` of `write_petri_blueprints`);
   - each claim and its verdict exactly as `verify_petri_blueprint` returned
     it: proven, violated or unknown. Say proven only for a claim the tool
     proved; an unknown claim is not proven. For a violated one, say which
     transition you will change, or ask the user;
   - when every claim is proven: "Click Save to apply it; the net then shows
     in the graph panel." Until Save, the work is a draft.

The builder canvas shows a net's root with the functions and agents it runs
as tools; it cannot show or edit places, transitions or claims, and it never
writes over a net. The net's drawing is in the graph panel after Save.

Conventions: transition names are `<Name>_<Verb>` (`Race_Commit`); never
use a key named `args`; a budget or permit is a place with seed tokens, not
a counter in a node; the turn's input is on `userIn` and the first token on
`eventOut` is the answer.
"""


# ----------------------------------------------------------------------------
#  Tools
# ----------------------------------------------------------------------------


def _packaged(name: str) -> str:
    return resources.files("adk_libpetri.net").joinpath(name).read_text(encoding="utf-8")


def _sections(text: str) -> dict[str, str]:
    """``AUTHORING.md`` by ``##``/``###`` heading (lower-cased), with its body."""
    parts: dict[str, str] = {}
    current, lines = "", []
    for line in text.splitlines():
        m = re.match(r"#{2,3} (.+)", line)
        if m:
            if current:
                parts[current] = "\n".join(lines).strip()
            current, lines = m.group(1).strip("` ").lower(), [line]
        else:
            lines.append(line)
    if current:
        parts[current] = "\n".join(lines).strip()
    return parts


def petri_authoring_guide(section: str = "") -> dict[str, Any]:
    """Read the guide to writing a Petri-net blueprint (agent_class: adk_libpetri.net.PetriNet).

    Args:
      section: A heading to read, e.g. "the loop", "the format", "motifs",
        "permit race", "quorum", "budget", "timed escalation", "prove:",
        "reading a counterexample". Empty for the whole guide.

    Returns:
      ``text`` (the guide or the section) and ``sections`` (every heading).
    """
    text = _packaged("AUTHORING.md")
    sections = _sections(text)
    if not section:
        return {"text": text, "sections": list(sections)}
    want = section.strip().lower()
    hits = [k for k in sections if want in k]
    if not hits:
        return {"error": f"no section matching {section!r}", "sections": list(sections)}
    return {"text": "\n\n".join(sections[k] for k in hits), "sections": list(sections)}


def petri_schema() -> dict[str, Any]:
    """Read the JSON Schema of the Petri-net blueprint YAML format.

    Returns:
      ``schema``: the JSON Schema text.
    """
    return {"schema": _packaged("schema.json")}


def _state(tool_context: Any) -> dict[str, Any] | None:
    state = getattr(tool_context, "state", None)
    if isinstance(state, dict):
        return state
    try:
        return dict(tool_context.state.to_dict())
    except Exception:
        try:
            return dict(tool_context._invocation_context.session.state)
        except Exception:
            return None


def _resolve(path: str, tool_context: Any) -> Path:
    from google.adk.cli.built_in_agents.utils.resolve_root_directory import resolve_file_path

    return resolve_file_path(path, _state(tool_context))


def _root(tool_context: Any) -> Path:
    return _resolve(".", tool_context).resolve()


def _is_petri(content: str) -> bool:
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return False
    return isinstance(data, dict) and data.get("agent_class") == PETRI_CLASS


async def write_petri_blueprints(configs: dict[str, str], tool_context: Any) -> dict[str, Any]:
    """Write YAML files after checking every Petri-net blueprint among them.

    The files are first written to a copy of the project. Each one with
    agent_class: adk_libpetri.net.PetriNet is loaded through ADK's loader and
    its net built. Only when all of them pass are the files written. Use it
    for blueprint files and for the agent configs they mount.

    Args:
      configs: Maps each file path (relative to the project root) to its YAML.

    Returns:
      ``success``, ``summary`` (one line per blueprint), and per file either
      its net summary or ``error`` with the YAML ``key_path`` and a ``hint``
      to fix it. Nothing is written unless ``success`` is true.
    """
    try:
        targets = {p: _resolve(p, tool_context) for p in configs}
    except ValueError as err:
        return {"success": False, "error": str(err)}
    for p, target in targets.items():
        if target.suffix not in (".yaml", ".yml"):
            return {"success": False, "error": f"{p}: only .yaml/.yml files are written"}
    for p, content in configs.items():
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as err:
            return {"success": False, "files": {p: {"error": f"invalid YAML: {err}"}}}
    root = _root(tool_context)
    results = await asyncio.to_thread(_stage_and_check, root, targets, configs)
    ok = all(r.get("ok", True) for r in results.values())
    if ok:
        for p, target in targets.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(configs[p], encoding="utf-8")
            results.setdefault(p, {})["written"] = str(target)
    lines = [
        f"{p}: " + (_net_line(r["net"]) if r.get("ok") else f"does not load: {r.get('error')}")
        for p, r in results.items()
        if "net" in r or "error" in r
    ]
    out: dict[str, Any] = {"success": ok, "files": results}
    if lines:
        out["summary"] = "; ".join(lines) + ("" if ok else ". Nothing was written.")
    return out


def _net_line(net: dict[str, Any] | None) -> str:
    if not net:
        return "loads"
    claims = net["claims"]
    proofs = f"{claims} claims, not verified yet" if claims else "no prove: claims yet"
    size = f"{net['places']} places, {net['transitions']} transitions"
    return f"net {net['name']!r} loads ({size}); {proofs}"


def _stage_and_check(
    root: Path, targets: dict[str, Path], configs: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Check the blueprints among ``configs`` in a copy of ``root`` (:func:`~.staging.staged`)."""
    results: dict[str, dict[str, Any]] = {}
    with staged(root, {targets[p]: configs[p] for p in targets}) as stage:
        for p, target in targets.items():
            if not _is_petri(configs[p]):
                continue
            dest = stage.path(target)
            report = check_file(str(dest)).to_dict()
            if report.get("error"):
                report["error"] = stage.restore(str(report["error"]).replace(str(dest), p))
            report["path"] = p
            results[p] = report
    return results


def _staged_check(root: Path, target: Path) -> CheckReport:
    """``check_file`` on ``target``, loaded from a copy of ``root`` when it is under it."""
    if not target.resolve().is_relative_to(root.resolve()):
        return check_file(str(target))
    with staged(root) as stage:
        report = check_file(str(stage.path(target)))
        return replace(report, path=str(target), error=report.error and stage.restore(report.error))


def _staged_verify(root: Path, target: Path, k: int | None) -> VerifyReport:
    """``verify_file`` on ``target``, loaded from a copy of ``root`` when it is under it."""
    if not target.resolve().is_relative_to(root.resolve()):
        return verify_file(str(target), k)
    with staged(root) as stage:
        report = verify_file(str(stage.path(target)), k)
        return replace(report, path=str(target), error=report.error and stage.restore(report.error))


async def check_petri_blueprint(path: str, tool_context: Any) -> dict[str, Any]:
    """Load a Petri-net blueprint through ADK's loader and build its net (no proofs).

    Args:
      path: The blueprint YAML, relative to the project root.

    Returns:
      ``ok`` with the net's size and a ``summary`` line, or ``error`` with
      its YAML ``key_path`` and ``hint``.
    """
    try:
        target = _resolve(path, tool_context)
    except ValueError as err:
        return {"ok": False, "error": str(err)}
    root = _root(tool_context)
    report = (await asyncio.to_thread(_staged_check, root, target)).to_dict()
    report["summary"] = _net_line(report["net"]) if report["ok"] else "does not load"
    return report


async def verify_petri_blueprint(path: str, tool_context: Any, k: int = 0) -> dict[str, Any]:
    """Prove the prove: claims of a Petri-net blueprint with the libpetri verifier.

    Args:
      path: The blueprint YAML, relative to the project root.
      k: User inputs (turns) the proofs assume; 0 for the claims' defaults.

    Returns:
      ``ok`` (every claim proven), ``summary`` (the verdicts in one line, to
      tell the user) and per claim its ``verdict`` (proven, violated or
      unknown). A violated claim has ``steps`` (its counterexample: one line
      per firing with the marking after it, the last saying what breaks) and,
      when the server can show it, ``picture``: an image line (HTML) to put
      in the reply verbatim.
    """
    try:
        target = _resolve(path, tool_context)
    except ValueError as err:
        return {"ok": False, "error": str(err)}
    digest = await asyncio.to_thread(net_digest, target)
    report = await asyncio.to_thread(_staged_verify, _root(tool_context), target, k or None)
    if not k:
        remember_verdicts(str(target), report, digest=digest)
    out = report.to_dict()
    out["claims"] = [_claim_for_model(c, report) for c in report.claims]
    return {**out, "summary": verdict_line(report), "next": _NEXT}


def _claim_for_model(c: ClaimResult, report: VerifyReport) -> dict[str, Any]:
    """A claim as the model reads it: no empty fields; a counterexample as lines (and a picture)."""
    d: dict[str, Any] = {"net": c.net, "label": c.label, "kind": c.kind, "verdict": c.verdict}
    if c.places:
        d["places"] = list(c.places)
    if c.kind == "place_bound":
        d["bound"] = c.bound
    for key in ("scope", "reason", "report"):
        if getattr(c, key):
            d[key] = getattr(c, key)
    if c.notes:
        d["notes"] = list(c.notes)
    if c.verdict == "violated":
        d["steps"] = step_lines(c)
        image = proof_view.picture(report.graphs.get(c.net), c)
        if image:
            d["picture"] = image
    return d


# ----------------------------------------------------------------------------
#  The last verdicts, by blueprint text
# ----------------------------------------------------------------------------

_NEXT = (
    "Tell the user each claim's verdict as above; for a violated claim, put its picture "
    "line in the reply as given. When every claim is proven, tell them to click Save in "
    "the builder: the net then shows in the graph panel."
)

_VERDICTS: OrderedDict[str, VerifyReport] = OrderedDict()
_VERDICTS_KEPT = 64
_VERDICTS_LOCK = threading.Lock()


_TREE_SKIP = frozenset({"__pycache__", ".git", ".adk", ".venv", "node_modules"})
_TREE_FILES = (".yaml", ".yml", ".py")


def net_digest(path: Path) -> str:
    """A digest of the blueprint at ``path`` and every file it can load beside it.

    Its own name and every ``.yaml``/``.yml``/``.py`` file under its folder
    (mounted blueprints, helper YAML, ``agent.py``), the app's ``tmp/`` and
    caches left out: a mounted file or a node's code changed since a verify
    changes the digest, so its verdicts are no longer this net's. The builder's
    draft and the app it is saved into hold the same files, so the same digest.
    Refs into other packages of the agents folder are not followed.
    """
    root = path.parent
    h = hashlib.sha256(path.name.encode("utf-8"))
    found: list[tuple[str, Path]] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in _TREE_SKIP and not (d == root and p.name == "tmp"):
                    stack.append(p)
            elif p.suffix in _TREE_FILES:
                found.append((p.relative_to(root).as_posix(), p))
    for rel, p in sorted(found):
        try:
            data = p.read_bytes()
        except OSError:
            continue
        h.update(b"\0" + rel.encode("utf-8") + b"\0")
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()


def remember_verdicts(path: str, report: VerifyReport, *, digest: str | None = None) -> None:
    """Keep ``report`` (default ``k``, not recursive) as the verdicts of the net at ``path``.

    Keyed by :func:`net_digest` (``digest``: taken before the verify ran, so a
    file changed during it does not take its verdicts). The builder canvas's
    card and greeting read them back (:func:`known_verdicts`) for the draft
    and, once saved, for the app.
    """
    if digest is None:
        try:
            digest = net_digest(Path(path))
        except OSError:
            return
    report = replace(report, graphs={})
    with _VERDICTS_LOCK:
        _VERDICTS[digest] = report
        _VERDICTS.move_to_end(digest)
        while len(_VERDICTS) > _VERDICTS_KEPT:
            _VERDICTS.popitem(last=False)


def known_verdicts(path: Path | str) -> VerifyReport | None:
    """The last verify of the net at ``path`` as it is now (its files unchanged since)."""
    try:
        digest = net_digest(Path(path))
    except OSError:
        return None
    with _VERDICTS_LOCK:
        return _VERDICTS.get(digest)


def verdict_line(report: VerifyReport) -> str:
    """The verdicts in one plain line: what the user should be told."""
    if report.error:
        return f"Not verified: the blueprint does not load ({report.error})."
    if not report.claims:
        return "Nothing to verify: the blueprint has no prove: claims."
    n = len(report.claims)
    line = f"{report.proven} of {n} claims proven"
    for verdict in ("violated", "unknown"):
        labels = [c.label for c in report.claims if c.verdict == verdict]
        if labels:
            line += f"; {verdict}: " + ", ".join(labels)
    if not report.z3 and report.unknown:
        line += " (no z3 binary was found)"
    return line + "."


PETRI_TOOLS = (
    petri_authoring_guide,
    petri_schema,
    write_petri_blueprints,
    check_petri_blueprint,
    verify_petri_blueprint,
)


# ----------------------------------------------------------------------------
#  The assistant
# ----------------------------------------------------------------------------


# ----------------------------------------------------------------------------
#  Replies the model is not needed for
# ----------------------------------------------------------------------------

GREETING = "hello"
"""What the dev UI's builder panel sends by itself when it opens."""

COMMANDS = ("verify", "check")
"""Messages answered without the model on a net app: ``verify`` proves the net's
``prove:`` claims, ``check`` loads and builds it."""

GREETING_VERIFY_S = 8.0
"""How long the panel's greeting waits for the verdicts (they keep coming in the background)."""
COMMAND_VERIFY_S = 180.0

_CANVAS_NOTE = (
    "Tell me what to change and I will write it{proofs}. Send `verify` or `check` any "
    "time for the verdicts. The canvas is read-only for a net: it lists what the net "
    "runs, and sub-agents, callbacks or renamed functions added there are not saved."
)
_SAVE_NOTE = "Click Save to apply the draft; the net then shows in the graph panel."


def gemini_key_missing(model: Any) -> bool:
    """Whether ``model`` is a Gemini model with no way to reach it (no API key, not Vertex)."""
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in ("1", "true"):
        return False
    if os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"):
        return False
    name = model if isinstance(model, str) else getattr(model, "model", None)
    return isinstance(name, str) and "gemini" in name.lower()


def no_key_text(*, net: bool = False) -> str:
    """How to set a Gemini key; ``net``: the app is a net (what works without one)."""
    folder = Path.cwd().name or "."
    text = (
        "No Gemini API key is set, so I cannot answer yet. Put `GOOGLE_API_KEY=...` in "
        f"`{folder}/.env`, the folder `adk-libpetri web` serves (or set "
        "`GOOGLE_GENAI_USE_VERTEXAI=1` with a Vertex project), and restart it."
    )
    if net:
        text += (
            " The graph panel works without it, and so does chat with a net that calls "
            "no model. I can still check and prove this net: send `check` or `verify`."
        )
    else:
        text += " The graph panel works without it; chat with a Gemini agent needs it too."
    return text


def _app_of(state: dict[str, Any] | None) -> tuple[str, Path] | None:
    """The app the builder edits and its folder, from ``root_directory`` (``<app>/tmp/<app>``)."""
    root = (state or {}).get("root_directory")
    if not isinstance(root, str) or not root.strip("/\\"):
        return None
    draft = Path(root)
    if draft.parent.name == "tmp":
        return draft.name, draft.parent.parent
    return draft.name, draft


def _net_file(state: dict[str, Any] | None) -> tuple[Path, bool] | None:
    """The net the builder edits: its draft's root file (else the app's), and whether it
    is the draft; None when the app's root is not a net."""
    from .builder_guard import is_net_text
    from .canvas_view import ROOT_FILE

    found = _app_of(state)
    if found is None:
        return None
    draft = Path(str((state or {})["root_directory"])) / ROOT_FILE
    path, is_draft = (draft, True) if draft.is_file() else (found[1] / ROOT_FILE, False)
    try:
        if not path.is_file() or not is_net_text(path.read_text(encoding="utf-8")):
            return None
    except (OSError, ValueError):
        return None
    return path, is_draft


def _draft_changed(state: dict[str, Any] | None) -> bool:
    """Whether the builder's draft differs from the app (so Save would change something)."""
    from .drafts import draft_changed as changed
    from .drafts import draft_root, reconcile

    found = _app_of(state)
    if found is None:
        return False
    app_root = found[1]
    tmp_root = draft_root(app_root, found[0])
    if not tmp_root.is_dir():
        return False
    reconcile(app_root, tmp_root)
    return changed(app_root, tmp_root)


def _who(path: Path, state: dict[str, Any] | None) -> str:
    """The net as replies name it: ``race_agent`` (app ``yaml_race``), or one name when
    they are the same."""
    data = _load_yaml(path)
    name = data.get("name") if isinstance(data, dict) else None
    found = _app_of(state)
    app = found[0] if found else path.parent.name
    if not isinstance(name, str) or not name or name == app:
        return f"`{app}`"
    return f"`{name}` (app `{app}`)"


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None


_STEPS_SHOWN = 3
"""The steps of a counterexample a reply quotes: the last ones, where the claim breaks."""


def claim_lines(report: VerifyReport) -> list[str]:
    """Each claim and its verdict as a Markdown list, then each violated claim's last
    steps and its picture (under ``adk-libpetri web``)."""
    if report.error or not report.claims:
        return []
    items: list[str] = []
    details: list[str] = []
    for c in report.claims:
        if c.verdict == "violated":
            ss = steps(c)
            last = ss[-1] if ss else None
            where = ""
            if last is not None and last.index:
                where = f" at step {last.index} ({last.transition} {last.verb}: "
                where += f"{violation(c, last)})"
            items.append(f"- {c.label}: violated{where}")
            lines = step_lines(c)
            block = [f"Steps that break *{c.label}*:", "", *lines[-_STEPS_SHOWN:]]
            image = proof_view.picture(report.graphs.get(c.net), c)
            if image:
                block += ["", image]
            details.append("\n".join(block))
        elif c.verdict == "unknown":
            items.append(f"- {c.label}: unknown" + (f" ({c.reason})" if c.reason else ""))
        else:
            items.append(f"- {c.label}: {c.verdict}")
    out = ["\n".join(items), *details]
    if any("<img" in d for d in details):
        out.append("Click a picture to open it full size, with the net drawn at that step.")
    return out


def _save_line(
    state: dict[str, Any] | None, report: VerifyReport | None, *, loads: bool = True
) -> str | None:
    """What to say about Save: only when the draft differs from the app."""
    if not _draft_changed(state):
        return None
    if not loads:
        return "The draft differs from the app but does not load: fix it before you Save."
    if report is not None and report.ok and not report.error:
        return _SAVE_NOTE
    return "The draft differs from the app: prove its claims (send `verify`), then click Save."


def greeting(
    state: dict[str, Any] | None, report: VerifyReport | None = None, *, slow: bool = False
) -> str | None:
    """The builder's first reply on a net app, from its draft (else its root file); or None.

    The net's size and its verdicts (``report``, a fresh verify of that file:
    each claim, and each violated claim's last steps and picture; else the
    last verdicts of exactly these files, if any; ``slow``: the verify did not
    finish in time). "Click Save" only when the draft differs from the app and
    every claim is proven.
    """
    from .canvas_view import canvas_card

    found = _net_file(state)
    if found is None:
        return None
    path, is_draft = found
    try:
        card = canvas_card(path, draft=is_draft)
    except (OSError, ValueError):
        return None
    what = str(card.get("description") or "Petri net")
    workflow = what.startswith("Petri workflow")
    head = [f"This is {_who(path, state)}, a {what}."]
    if slow and "not verified yet" in what:
        head.append("Proving its claims takes a while; send `verify` for the verdicts.")
    parts = ["\n".join(head)]
    if report is not None:
        parts += claim_lines(report)
    proofs = "" if workflow else ", check that it loads and prove its claims"
    note = _CANVAS_NOTE.format(proofs=proofs)
    known = report if report is not None else known_verdicts(path)
    save = _save_line(state, known, loads="does not load" not in what)
    if save:
        note += " " + save
    return "\n\n".join([*parts, note])


_RUNNING: dict[str, asyncio.Task[VerifyReport | None]] = {}


def _verify_now(path: Path, is_draft: bool, digest: str) -> VerifyReport | None:
    try:
        report = _staged_verify(path.parent, path, None) if is_draft else verify_file(str(path))
    except Exception:  # a verifier crash: the reply says not verified
        logger.exception("builder: verifying %s failed", path)
        return None
    remember_verdicts(str(path), report, digest=digest)
    return report


async def verify_soon(path: Path, is_draft: bool, timeout: float) -> VerifyReport | None:
    """Verify the blueprint at ``path`` (default ``k``), waiting at most ``timeout`` seconds.

    One run per net (:func:`net_digest`: the blueprint and the files beside it)
    at a time: a run that outlasts the wait goes on, and its verdicts are
    remembered (:func:`known_verdicts`) for the canvas card and the next
    ``verify``. A file changed meanwhile is a new net, with a run of its own.
    """
    key = await asyncio.to_thread(net_digest, path)
    loop = asyncio.get_running_loop()
    task = _RUNNING.get(key)
    if task is None or task.get_loop() is not loop or task.done():
        task = loop.create_task(asyncio.to_thread(_verify_now, path, is_draft, key))
        _RUNNING[key] = task

        def forget(t: asyncio.Task[VerifyReport | None]) -> None:
            if _RUNNING.get(key) is t:
                _RUNNING.pop(key, None)

        task.add_done_callback(forget)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except TimeoutError:
        return None


async def _greet(state: dict[str, Any] | None) -> str | None:
    found = await asyncio.to_thread(_net_file, state)
    if found is None:
        return None
    report = await verify_soon(*found, GREETING_VERIFY_S)
    return await asyncio.to_thread(greeting, state, report, slow=report is None)


async def _command(state: dict[str, Any] | None, word: str) -> str | None:
    """``verify`` or ``check`` on the builder's net, answered without the model; or None."""
    found = await asyncio.to_thread(_net_file, state)
    if found is None:
        return None
    path, is_draft = found
    who = await asyncio.to_thread(_who, path, state)
    if word == "check":
        if is_draft:
            checked = await asyncio.to_thread(_staged_check, path.parent, path)
        else:
            checked = await asyncio.to_thread(check_file, str(path))
        d = checked.to_dict()
        if not d["ok"]:
            problem = str(d.get("error") or "it does not load").replace(str(path.parent) + "/", "")
            return f"{who} does not load: {problem}"
        return f"{who}: {_net_line(d['net'])}."
    report = await verify_soon(path, is_draft, COMMAND_VERIFY_S)
    if report is None:
        return (
            f"{who}: still proving after {COMMAND_VERIFY_S:.0f} s. Send `verify` again "
            "later; the run goes on."
        )
    head = verdict_line(report)
    if report.claims and not report.error:  # each claim is listed below
        head = f"{report.proven} of {len(report.claims)} claims proven."
        if not report.z3 and report.unknown:
            head = head.rstrip(".") + " (no z3 binary was found)."
    parts = [f"{who}: {head}", *claim_lines(report)]
    save = await asyncio.to_thread(_save_line, state, report)
    if save and report.claims:
        parts.append(save)
    return "\n\n".join(parts)


def _text_reply(text: str) -> Any:
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


def _first_turn(callback_context: Any) -> bool:
    """Whether this model call answers the session's first message, before any reply."""
    events = getattr(getattr(callback_context, "session", None), "events", None)
    if events is None:
        return False
    return all(getattr(e, "author", None) == "user" for e in events)


def _user_text(callback_context: Any) -> str:
    content = getattr(callback_context, "user_content", None)
    parts = getattr(content, "parts", None) or ()
    return "".join(getattr(p, "text", None) or "" for p in parts).strip()


def _command_word(text: str) -> str:
    return text.strip().strip(".!?`'\" ").removeprefix("/").lower()


async def _before_model(callback_context: Any, llm_request: Any) -> Any:
    """Answer without the model: the panel's own ``hello`` and ``verify``/``check`` on a net
    app, and every reply when no Gemini key is set."""
    from .builder_guard import take_note

    state = _state(callback_context)
    missing = gemini_key_missing(getattr(llm_request, "model", None))
    word = _command_word(_user_text(callback_context))
    greeted = _first_turn(callback_context) and word == GREETING
    reply = None
    if greeted:
        reply = await _greet(state)
    elif word in COMMANDS:
        reply = await _command(state, word)
    if reply is None and not missing:
        return None
    found = _app_of(state)
    note = take_note(found[0]) if found else None
    key = None
    if missing and (reply is None or greeted):
        key = no_key_text(net=await asyncio.to_thread(_net_file, state) is not None)
    parts = [x for x in (note, reply, key) if x]
    return _text_reply("\n\n".join(parts))


async def _after_model(callback_context: Any, llm_response: Any) -> Any:
    """Put what the canvas's last save dropped from a net in front of the next text reply."""
    from google.genai import types

    from .builder_guard import take_note

    content = getattr(llm_response, "content", None)
    parts = list(getattr(content, "parts", None) or ())
    if getattr(llm_response, "partial", False) or not any(getattr(p, "text", None) for p in parts):
        return None
    found = _app_of(_state(callback_context))
    note = take_note(found[0]) if found else None
    if note is None or content is None:
        return None
    content.parts = [types.Part(text=note + "\n\n"), *parts]
    return llm_response


def _with(existing: Any, callback: Any) -> list[Any]:
    if existing is None:
        return [callback]
    return [*existing, callback] if isinstance(existing, list) else [existing, callback]


@experimental
def create_petri_builder_assistant(model: Any = None) -> Any:
    """ADK's Agent Builder Assistant with the Petri tools and instruction added.

    ``model`` defaults to ADK's assistant's own default. Some replies need no
    model and get none: the panel's own ``hello`` on a net app is answered
    with the net's size and verdicts (:func:`greeting`); with no Gemini key,
    every reply says how to set one (:func:`no_key_text`), where ADK's panel
    would show a bare error code. What the canvas's last save dropped from a
    net (:func:`~adk_libpetri.web.builder_guard.take_note`) leads the next reply.
    """
    from google.adk.tools.function_tool import FunctionTool

    tools = [FunctionTool(f) for f in PETRI_TOOLS]
    try:
        from google.adk.cli.built_in_agents.adk_agent_builder_assistant import (
            AgentBuilderAssistant,
        )
    except ImportError:  # pragma: no cover - ADK moved its assistant
        return _fallback_assistant(model, tools)
    base = (
        AgentBuilderAssistant.create_agent()
        if model is None
        else AgentBuilderAssistant.create_agent(model=model)
    )
    instruction = base.instruction

    async def provider(ctx: Any) -> str:
        text: Any = instruction(ctx) if callable(instruction) else instruction
        if inspect.isawaitable(text):
            text = await text
        return str(text) + INSTRUCTION_ADDENDUM

    return base.model_copy(
        update={
            "description": base.description + "; also writes and verifies Petri-net blueprints",
            "instruction": provider,
            "tools": [*base.tools, *tools],
            "before_model_callback": _with(base.before_model_callback, _before_model),
            "after_model_callback": _with(base.after_model_callback, _after_model),
        }
    )


def _fallback_assistant(model: Any, tools: list[Any]) -> Any:
    from google.adk.agents import LlmAgent

    return LlmAgent(
        name="agent_builder_assistant",
        description="Writes and verifies Petri-net blueprints",
        model=model or os.environ.get("ADK_LIBPETRI_BUILDER_MODEL", "gemini-2.5-pro"),
        instruction="You help write ADK agent configs." + INSTRUCTION_ADDENDUM,
        tools=tools,
        before_model_callback=_before_model,
        after_model_callback=_after_model,
    )


__all__ = [
    "ADK_ASSISTANT",
    "GREETING",
    "INSTRUCTION_ADDENDUM",
    "PETRI_TOOLS",
    "check_petri_blueprint",
    "claim_lines",
    "create_petri_builder_assistant",
    "gemini_key_missing",
    "greeting",
    "known_verdicts",
    "net_digest",
    "no_key_text",
    "petri_authoring_guide",
    "petri_schema",
    "remember_verdicts",
    "verdict_line",
    "verify_petri_blueprint",
    "write_petri_blueprints",
]
