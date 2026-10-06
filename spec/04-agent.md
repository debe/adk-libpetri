# ADK adapter (AGT)

- **AGT-001** `PetriAgent` keeps one runner per session, created on the
  session's first invocation.
- **AGT-002** A turn injects the user content on `userIn` and returns the
  first non-partial event; under SSE, every partial up to and including it.
- **AGT-003** Every returned event carries the ADK invocation id.
- **AGT-004** A transition failure during a turn fails that turn and, on a
  runner that declares `turnAbort`, signals it once per failure, so the
  session serves the next turn.
- **AGT-005** With tracing, one `petri.invocation.<agent>` span per
  invocation parents the transition spans, ended when the next invocation of
  the session supersedes it.
- **AGT-006** Live: without a live config the agent returns the egress
  stream; with one, the bridge pumps the request queue into the connection,
  hands server frames to the decoder, authors no events itself, and closes
  the connection on every outcome.
- **AGT-007** (Python) As a node inside an ADK `Workflow`, the node input is
  the turn's user content and the terminal event is the node's output.
