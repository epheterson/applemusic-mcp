# Backlog — post-1.0 feature ideas

Deferred so we can ship 1.0. Each is a clean, self-contained feature.

## Batch operations
`playlist(action="batch", ops=[...], on_error="stop"|"continue", dry_run=false)`
- Each op: `{"op": "add"|"remove"|"swap"|"move", "playlist": ..., "track": ..., "replace": ..., ...}`.
- Resolve all names → catalog ids up front; run each op (reusing the transactional
  swap + native-truth verify); return a per-item ✓/✗ ledger + summary.
- `on_error`: `stop` (halt on first failure — safe default) or `continue`
  (best-effort, report casualties).
- `dry_run=true`: resolve + plan, no writes — preview a destructive batch first.
- Composes with "search-and-act": an op whose target is a query, resolved then acted on.

## Undo (last write)
`config(action="undo")` — reverse the last add/remove(s) using the audit log already
being written (`audit_log.log_action`). Real safety net, especially for batches.
- Needs: a structured, reversible record per write (track + playlist + direction) and
  a bounded undo depth.

## Folder rename off-mac
`playlist(action="rename", folder=...)` works on macOS only — there's no
`_folder_rename_api` (unlike create/delete, which have web-rail equivalents). Off-mac
it returns a clear "requires macOS" error. Add an amp-api folder-rename if the API
exposes one. Genuine capability gap, low frequency.

## Notes
- Both should reuse the existing transactional-swap + native verify so they inherit
  the "never silently lose a track" guarantee.
- Edges to expect (flagged in the 2026-06-29 dogfood): Music.app's server-side
  revert state during a batch — undo + dry-run both help surface/recover from it.
