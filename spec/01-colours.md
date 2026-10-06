# Colours (COL)

- **COL-001** The boundary places are `userIn: Content`, `eventOut: Event`,
  `llmRequest: LlmRequest`, `llmResponse: LlmResponse`, `toolCalls: ToolCalls`,
  `toolResults: ToolResults`, `legacySessionWrite: LegacySessionWrite`,
  `transfer: TransferTarget`, and the unit places `endInvocation`,
  `turnPermit`, `turnAbort`. Names are identical across ports.
- **COL-002** `ToolCalls` carries the calls and the model turn they came from
  (verbatim, so thought signatures survive a re-ask), or none.
- **COL-003** `ToolResults` carries the responses and, required, the model
  turn whose calls they answer.
- **COL-004** `LegacySessionWrite` is the only mapping-shaped colour and is
  consumed only by `PersistState_Persist`. There is no general state colour
  and no raw-payload colour.
- **COL-005** Composition fuses places by name and token type; a name used
  with two types is rejected at composition time.
