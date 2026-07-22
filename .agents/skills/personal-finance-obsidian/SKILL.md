---
name: personal-finance-obsidian
description: Use when a user asks to import, merge, reconcile, or analyze personal bills and payment records in an Obsidian vault, including Alipay, WeChat Pay, Huabei, JD Baitiao, Yu'e Bao, credit-card statements, refunds, installments, screenshots, CSV/XLSX/PDF files, monthly reports, debt snapshots, or a finance dashboard.
---

# Personal Finance Obsidian

## Overview

Build a local, auditable personal-finance ledger and regenerate Obsidian reports from it. Let the Agent extract and explain; let `scripts/finance.py` calculate, match, deduplicate, and render.

Preserve every original file and raw row. Never upload financial files, ZIP passwords, card numbers, identity data, or account details to the web.

## Workflow

1. Resolve the Obsidian Vault. Use the current workspace when it is clearly the Vault; otherwise ask for its path.
2. Read [source-formats.md](references/source-formats.md) for the supplied platform and file type.
3. Initialize once:

   ```bash
   python3 <SKILL_DIR>/scripts/finance.py init --vault <VAULT>
   ```

4. Route each input:

   | Input | Action |
   | --- | --- |
   | Alipay or WeChat personal CSV | Run `import` directly. |
   | Normalized CSV | Run `import` directly. |
   | XLSX, PDF, HTML, email, screenshot | Run `archive`, extract locally, create a normalized CSV using [normalized-schema.md](references/normalized-schema.md), then run `import`. |
   | Huabei, Baitiao, or credit-card balance screenshot | Archive it, then record verified totals with `debt`. |
   | Encrypted ZIP | Ask the user to unzip locally. Never request or persist its password. |

5. Rebuild every month after all imports. This revises the original consumption month when a later refund arrives:

   ```bash
   python3 <SKILL_DIR>/scripts/finance.py build-all --vault <VAULT>
   ```

6. Inspect `Finance/Reviews/待确认交易.md`. Do not select among multiple refund or duplicate candidates without user evidence. After confirmation, run `link-refund`, then `build-all` again.
7. Read `Finance/Data/analysis-context-YYYY-MM.json` and [analysis-rules.md](references/analysis-rules.md). Replace only the content inside the `AI_ANALYSIS` and `AI_SUGGESTIONS` markers in the month note. Generate at most three actionable suggestions.
8. Verify `Finance/Reviews/数据质量报告.md`, the month note, and `Finance/Dashboard.md`. Report imported rows, linked refunds, duplicates, pending reviews, coverage limitations, and generated paths.

## Accounting Contract

Read [accounting-rules.md](references/accounting-rules.md) before changing matching or totals.

- Hide a fully refunded purchase and its refund from consumption tables and behavior analysis. Keep both in raw records and cash flow.
- Show a partially refunded purchase at its remaining net consumption.
- Keep an ambiguous refund pending; never hide a guessed purchase.
- Count repayments, installments, and internal transfers separately from new consumption.
- Keep both platform and bank rows when deduplicating; mark one canonical consumption row.
- Use purchase date for consumption and actual transaction date for cash flow.
- Treat screenshots as partial coverage unless completeness is independently verified.

## Commands

Run `python3 <SKILL_DIR>/scripts/finance.py <command> --help` for all flags. See [cli.md](references/cli.md) for examples.

| Command | Purpose |
| --- | --- |
| `init` | Create `Finance/`, SQLite, and editable category rules. |
| `import` | Detect and import Alipay, WeChat, or normalized CSV. |
| `archive` | Preserve an unsupported original before Agent extraction. |
| `debt` | Record a verified debt snapshot. |
| `link-refund` | Resolve one reviewed refund without editing raw data. |
| `build` / `build-all` | Regenerate monthly notes and Dashboard. |

## Common Mistakes

- Do not treat a credit-card repayment as another purchase.
- Do not count Yu'e Bao or Lingqiantong transfers as spending; count yields as investment income.
- Do not trust file extensions, fixed header row numbers, or screenshot coverage.
- Do not infer a refund's cash arrival date from its application date.
- Do not overwrite `Finance/Data/config.json` or text inside analysis markers during rebuilds.
