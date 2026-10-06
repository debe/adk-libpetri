# adk-libpetri specification

Requirement IDs are `<CHAPTER>-<NNN>`. A requirement is normative ("MUST")
unless marked otherwise. Each port's tests cite the IDs they cover; the
[coverage matrix](coverage-matrix.md) collects them.

| Chapter | IDs | Ports |
|---|---|---|
| [Colours](01-colours.md) | COL | Java, Python |
| [Stock subnets](02-subnets.md) | SUB | Java, Python |
| [Runner](03-runner.md) | RUN | Java, Python |
| [ADK adapter](04-agent.md) | AGT | Java, Python |
| [Session registry](05-registry.md) | REG | Java, Python |
| [Verification](06-verification.md) | VER | Java, Python |
| [Compiled workflows](07-workflow.md) | WF | Python |
| [Net blueprints](08-blueprints.md) | NET | Python |

## Fixture format

`fixtures/nets/<subnet>.json` is `json.dumps(obj, indent=2, sort_keys=True)`
plus a newline, byte for byte:

- `name`: the net name;
- `places`: `{name, type}` sorted by name, `type` the token type's simple
  name (`Void` for a unit place);
- `ports`: `{name, direction: in|out|inout, place}` sorted by name;
- `transitions`: sorted by name, each with `inputs` (`{kind, place, count}`
  sorted by place), `output` (a place name, or `{and|xor: [...]}`,
  `{timeout, child}`, `{forward: [from, to]}` in declaration order), `reads`,
  `inhibitors`, `resets` (sorted place names), `priority`, `timing` (`null`
  for immediate, else `{kind, earliest_ms, latest_ms}`) and `match`
  (`null` or `"present"`).
