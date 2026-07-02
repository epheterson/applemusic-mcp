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

## Deferred from the pre-1.0 review (lower priority — not data-loss)
- **Swap uses a resolver per step.** `_playlist_add`, `_confirm_swap_track`, and
  `_playlist_remove` each resolve the playlist name independently. Deterministic
  resolution makes divergence unlikely, but resolving ONCE and threading the resolved
  id through all three would remove the theoretical "add to A, remove from B" edge.
- **`_profile_in_use()` is a no-op on Windows** (`pgrep`). The "server already holds
  the Chrome profile" fast-fail never fires there → a CLI login during an active
  server can collide on the profile lock. Use a cross-platform process check.
- **Multi-track `track` in a swap**: `_add_landed` accepts "Added N, M failed", so a
  swap whose intended track was the failed one could still remove `replace`. Guard
  swaps to a single resolved track.
- **Bare `mkdir` in `track_cache.py` / `audit_log.py`** lacks the friendly-error
  wrapper `get_config_dir` now has — an unwritable `APPLEMUSIC_MCP_HOME` surfaces a
  raw traceback there. Consistency.

## Notes
- Both should reuse the existing transactional-swap + native verify so they inherit
  the "never silently lose a track" guarantee.
- Edges to expect (flagged in the 2026-06-29 dogfood): Music.app's server-side
  revert state during a batch — undo + dry-run both help surface/recover from it.
