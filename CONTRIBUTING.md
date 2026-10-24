# Contributing

Thanks for improving `personal-finance-obsidian`.

## Before you start

- Do not commit real transaction exports, screenshots, databases, credentials, card numbers, IDs, or archive passwords.
- Keep generated `Finance/` data outside the repository whenever possible.
- Preserve raw records and avoid silently resolving an ambiguous refund or duplicate.
- Read the accounting contract in `SKILL.md` before changing matching or totals.

## Making a change

1. Keep the change focused and explain its user impact.
2. Add or update tests for changes to imports, matching, accounting calculations, or generated reports.
3. Update the reference documentation when a command, input format, or accounting rule changes.
4. Run the complete test suite:

   ```bash
   TMPDIR=/private/tmp python3 -B -m unittest discover -s tests -v
   ```

5. Open a pull request with a clear summary and any relevant privacy or migration notes.

## Design principles

- The SQLite database is canonical; Markdown is regenerated output.
- A fully refunded purchase remains in raw records and cash flow, but should not inflate consumption.
- Repayments, installments, and internal transfers are not new consumption.
- Uncertain matches must stay reviewable rather than being guessed.

## Workspace and review boundaries

- Keep internal context, handoffs, discussion and execution logs outside the public repository. Use a sibling private-notes directory for internal records, temporary test Vaults/caches/builds under /private/tmp, and sibling release-artifacts for installers. Formal tests and product documentation belong in this repository.
- Never use real statements for validation. Runtime modules use only Python 3.9+ standard library; optional development validators must not become runtime dependencies.
- Before any commit/push, review each intended path for privacy, local absolute paths, secrets, large binaries and unrelated files. Stage individual reviewed paths; do not use git add . . No preflight script is currently bundled, so do not claim one ran.
- Test continuous imports, both purchase/refund orderings, manual overrides, overlapping undo, legacy migration and failure recovery when touching the ledger. Preserve original evidence and test exact minor-unit amounts.
- A passing test suite is not proof of Obsidian or Codex/Claude host compatibility. State the actual Python/OS and which real host checks were not run. CI configuration is not an executed CI result.
