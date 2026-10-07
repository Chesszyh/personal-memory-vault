# ChatGPT official-export regression cases

Public regression tests use synthetic exports. Keep measured counts, tree hashes, and source-specific acceptance reports with the corresponding private vault.

## Supported source shapes

The fixtures cover sharded conversations, selected nodes with descendants, multiple roots, identifiers reused across conversations, later snapshots missing earlier records, unresolved attachments, group chats, and nested export files. These cases are preserved without treating source absence as deletion.

See `tests/test_chatgpt_export.py`, `tests/test_archive_validation.py`, and `tests/test_update_export.py` for executable examples.

## Implemented gate policy

Current hard failures include:

- an invalid Evidence Manifest or evidence-tree mismatch;
- malformed, unsafe, duplicated, missing, or inconsistent provider inventory
  paths when `export_manifest.json` is present;
- an unparseable JSON file or an invalid expected root shape;
- duplicate conversation IDs within one Snapshot;
- mapping-key/node/message ID mismatch inside a conversation, unresolved
  parent, graph cycle, or unresolved `current_node`;
- duplicate/invalid group thread or group message IDs and malformed group
  attachment arrays;
- a Snapshot ID/evidence conflict, incompatible database/import profile, or
  output directory equal to/inside the Evidence root.

Current warnings and soft reconciliation findings include:

- multiple/no roots, a selected node with children, declared-child resolution
  failure, or declared-child/parent disagreement;
- unresolved or ambiguous ordinary-message and library asset references;
- unresolved or ambiguous library thread/message relationships;
- unresolved or ambiguous group-chat asset references.

Cross-conversation ID reuse is handled by scoped identity and is not itself a
warning. Unknown top-level fields are retained in `raw_json`/`unknown_json` but
do not currently emit schema-observation diagnostics. Nullable times and
unrecognized content types also do not currently create warnings. MIME
detection/disagreement, friendly-name conflict reporting, and a complete
cross-source reconciliation report remain planned checks; this document must
not imply that the importer already performs them.
