# 高级用法：CLI

普通账单处理应通过 `$personal-finance-obsidian` 完成。仅在调试、批量重建、自动化或人工复核退款时直接使用以下命令。

Replace `<SKILL_DIR>` with the directory containing `SKILL.md` and `<VAULT>` with the Obsidian Vault root.

```bash
python3 <SKILL_DIR>/scripts/finance.py init --vault <VAULT>
python3 <SKILL_DIR>/scripts/finance.py import --vault <VAULT> --file alipay.csv
python3 <SKILL_DIR>/scripts/finance.py archive --vault <VAULT> --file statement.png --source huabei-screenshot --coverage partial
python3 <SKILL_DIR>/scripts/finance.py debt --vault <VAULT> --account 京东白条 --as-of 2026-07-31 --outstanding 5000.00 --due 1000.00 --due-date 2026-08-10
python3 <SKILL_DIR>/scripts/finance.py build-all --vault <VAULT>
python3 <SKILL_DIR>/scripts/finance.py link-refund --vault <VAULT> --refund-id <REFUND_ID> --purchase-id <PURCHASE_ID>
```

## Generated Vault layout

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

The SQLite file is canonical. Markdown is derived output. Edit category rules in `Finance/Data/config.json`; do not edit raw rows or calculated totals in month notes.
