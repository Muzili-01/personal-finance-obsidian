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
   python3 -m unittest discover -s tests -v
   ```

5. Open a pull request with a clear summary and any relevant privacy or migration notes.

## Design principles

- The SQLite database is canonical; Markdown is regenerated output.
- A fully refunded purchase remains in raw records and cash flow, but should not inflate consumption.
- Repayments, installments, and internal transfers are not new consumption.
- Uncertain matches must stay reviewable rather than being guessed.
