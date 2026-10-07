# Archive Reader visual prototype

This folder is a visual and interaction prototype for the local reading archive.

- All conversation text, identifiers, dates, file names, and counts in the prototype are deliberately invented demonstration data.
- The prototype does not read the vault, exported conversations, account fields, browser state, or SQLite.
- It explores a restrained three-pane desktop reader with light/dark themes, main-branch reading, folded alternative branches, attachment cards, provenance metadata, snapshot state, search, and filters.
- The `洞察` workspace demonstrates deterministic archive analytics: yearly/monthly activity, conversation/message composition, role/model distribution, branch/regeneration/edit rates, attachment health, snapshot deltas, source coverage, and data-quality checks.
- LLM-derived topic/profile/emotion examples live in a visually isolated `实验区 · 待人工审核`; the prototype never presents them as confirmed facts.
- It is not production code and does not establish the canonical reader data contract.

Serve the repository `designs/` directory over HTTP and open `/archive-reader/`.

## Interaction checklist

- Select a conversation from the left rail.
- Switch between `阅读` and `洞察`; selecting a conversation while in insights returns to reading.
- Search for `附件` or apply the `有分支` / `有附件` filters.
- Expand `2 个替代回答` in the first conversation.
- Open the source drawer, then switch between `溯源` and `快照`.
- Toggle light/dark theme.
- In `洞察`, switch the activity year and inspect the deterministic/experimental boundary.
- At narrow viewport widths, open/close the conversation rail and source drawer as overlays.

## Production note

This design prototype loads pinned React/Babel builds from a CDN. The production reader uses its own offline assets in `src/personal_vault/reader_static/`.
