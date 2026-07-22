# personal-finance-obsidian

A local, auditable personal-finance ledger and Obsidian reporting skill.

`personal-finance-obsidian` turns payment records into a canonical SQLite ledger, then regenerates monthly Obsidian notes and a dashboard from that ledger. It is designed for careful reconciliation: originals are preserved, calculations are reproducible, and uncertain matches stay visible for review.

## What it does

- Imports Alipay, WeChat Pay, and normalized transaction CSV files.
- Archives unsupported originals before manual normalization.
- Produces a local SQLite ledger plus monthly reports and an Obsidian dashboard.
- Separates consumption from cash flow, repayments, installments, and internal transfers.
- Reconciles refunds, detects likely credit-card duplicates, and keeps ambiguous cases pending.
- Records debt snapshots and data-quality reviews.

## Privacy first

Financial records stay on your machine. The tool does not upload bills, account details, screenshots, or databases.

Create your Obsidian vault outside this repository, or keep the generated `Finance/` directory untracked. The root `.gitignore` excludes `Finance/` and local SQLite files as a safeguard. Never commit real transaction exports, credentials, card numbers, IDs, or archive passwords.

This is a bookkeeping aid, not financial, tax, or legal advice.

## Quick start

Requirements: Python 3.9 or later and an Obsidian vault.

```bash
git clone https://github.com/Muzili-01/personal-finance-obsidian.git
cd personal-finance-obsidian

# Replace this with the root directory of your own Obsidian vault.
VAULT="$HOME/Documents/MyObsidianVault"

python3 .agents/skills/personal-finance-obsidian/scripts/finance.py init --vault "$VAULT"
python3 .agents/skills/personal-finance-obsidian/scripts/finance.py import --vault "$VAULT" --file /path/to/alipay.csv
python3 .agents/skills/personal-finance-obsidian/scripts/finance.py build-all --vault "$VAULT"
```

The generated vault layout is:

```text
Finance/
├── Dashboard.md
├── Raw/<source>/
├── Data/
│   ├── finance.sqlite3
│   ├── config.json
│   └── analysis-context-YYYY-MM.json
├── Months/YYYY-MM.md
├── Accounts/
├── Reviews/
│   ├── 待确认交易.md
│   └── 数据质量报告.md
├── Assets/Charts/
└── Templates/
```

The SQLite database is canonical; the Markdown files are derived output. Adjust category rules in `Finance/Data/config.json`, rather than editing raw rows or calculated totals in monthly notes.

## Commands

Run `python3 .agents/skills/personal-finance-obsidian/scripts/finance.py <command> --help` to see all options.

| Command | Purpose |
| --- | --- |
| `init` | Create the local finance directory, database, and editable category rules. |
| `import` | Detect and import an Alipay, WeChat, or normalized CSV export. |
| `archive` | Preserve a PDF, image, or other unsupported original before normalization. |
| `debt` | Save a verified debt snapshot. |
| `link-refund` | Resolve a refund only after the user has reviewed the match. |
| `build` / `build-all` | Regenerate a month or every affected monthly report and the dashboard. |

For examples, source-format notes, and the normalized CSV schema, see [the skill documentation](.agents/skills/personal-finance-obsidian/SKILL.md) and its [`references/`](.agents/skills/personal-finance-obsidian/references) directory.

## Project layout

```text
.agents/skills/personal-finance-obsidian/
├── SKILL.md                 # workflow and accounting contract
├── assets/                  # sample config and normalized CSV template
├── references/              # CLI, source-format, schema, and accounting notes
├── scripts/finance.py       # local command-line implementation
└── tests/test_finance.py    # regression tests
```

`.claude/skills/personal-finance-obsidian` is a compatibility symlink to the same skill for Claude-based setups.

## Development

No third-party runtime dependency is required. Run the regression suite with:

```bash
python3 -m unittest discover -s .agents/skills/personal-finance-obsidian/tests -v
```

Please read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting changes.

## License

[MIT](LICENSE) © 2026 Muzili-01.
