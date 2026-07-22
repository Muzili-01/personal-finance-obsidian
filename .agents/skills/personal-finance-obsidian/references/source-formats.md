# Source Formats

Use content signatures, not filenames. Keep unknown columns in the raw JSON and stop with a clear unsupported-format message instead of silently dropping rows.

## Input priority

1. Personal reconciliation CSV/XLSX
2. Text PDF, HTML, or email statement
3. Transaction-detail screenshot
4. Scrolling bill-list screenshot

Treat levels 3–4 as partial coverage. Archive overlapping screenshots separately, then remove visual overlap only in the normalized layer.

## Alipay

Prefer the personal reconciliation export from the Alipay bill page. Common fields are:

```text
交易时间, 交易分类, 交易对方, 对方账号, 商品说明, 收/支,
金额, 收付款方式, 交易状态, 交易订单号, 商家订单号, 备注
```

Expect title/footer lines, whitespace around values, GB18030/GBK or UTF-8 encoding, and order numbers that must remain strings. Read Huabei purchases from `收付款方式`; use Huabei screenshots for outstanding debt, installments, unbilled amounts, and due dates.

Classify Yu'e Bao transfer-in/out as asset movement, yield as investment income, and a purchase paid from Yu'e Bao as one purchase.

## WeChat Pay

Prefer “用于个人对账”. Common fields are:

```text
交易时间, 交易类型, 交易对方, 商品, 收/支, 金额(元),
支付方式, 当前状态, 交易单号, 商户单号, 备注
```

Expect CSV or XLSX. `收/支` may be `/` for repayment, top-up, withdrawal, Lingqiantong movement, or investment activity; classify from `交易类型` and status rather than discarding the row.

## JD Finance and Baitiao

Treat JD Finance personal CSV as a transaction source. Until a real file is supported by the bundled parser, archive it and normalize it with the schema reference.

Treat the Baitiao monthly page as a debt source: record outstanding balance, current due, due date, installment principal, and fees separately. A refund may restore credit, reduce debt, or return to a bank card/JD wallet depending on repayment state.

## Credit cards

Expect bank-specific PDF/HTML/email/CSV/XLSX layouts. Extract both transaction date and posting date when present. Preserve card last four digits, statement cycle, statement date, due date, total due, minimum due, currency, description, and amount.

Credit-card rows often lack order IDs and use gateway descriptions such as `支付宝-商户` or `财付通-商户`. Use amount, date distance, merchant similarity, payment channel, and card tail for cross-platform duplicate candidates.

## Screenshots and PDFs

Archive the original first. Extract only visible facts; do not invent IDs, hidden rows, statement totals, dates, or payment channels. For a scrolling screenshot set, preserve source image and sequence, then create one normalized row per visible transaction.

For a debt summary screenshot, use `debt` after verifying account name, as-of date, outstanding amount, current due, and due date. Mark missing values explicitly rather than guessing.
