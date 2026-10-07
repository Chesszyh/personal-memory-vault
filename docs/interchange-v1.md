# Source-neutral adapter interchange v1

`pmv.interchange-event.v1` is the boundary for future Gemini, DeepSeek,
Claude, WeChat, QQ, and other source adapters. Provider parsers retain their
own raw snapshots; they emit one JSON object per line without pretending that
different providers share an original schema.

Every event has these exact fields:

- `schema`, fixed to `pmv.interchange-event.v1`;
- globally stable `event_id` within the adapter snapshot;
- `provider`, `identity_scope`, and `snapshot_id` provenance;
- `kind`: `thread`, `participant`, `message`, `attachment`, or `relation`;
- optional `thread_native_id`, `native_id`, and `observed_at`;
- normalized `payload` used by canonical projections;
- complete provider-specific `raw_payload` for future reprocessing.

Adapters should represent replies, forwards, edits, withdrawals, and branch
edges as relation events instead of flattening them into message text. Missing
media remains an attachment event with an explicit unavailable state. A
snapshot that lacks an older event does not imply deletion unless the source
provides an explicit deletion or withdrawal event.

The current implementation validates this envelope and its deterministic
digest. Projection of interchange events into canonical schema v2 remains a
later adapter-specific step; the validator does not write the archive.
