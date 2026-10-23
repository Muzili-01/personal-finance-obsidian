# Normalized CSV Schema

Use `assets/normalized-transactions-template.csv`. Keep one row per visible source transaction and store amounts as positive decimal strings; use `direction` for flow direction.

## Required columns

| Column | Values / meaning |
| --- | --- |
| `transaction_date` | `YYYY-MM-DD HH:MM:SS`; use visible precision only. |
| `source_platform` | `alipay`, `wechat`, `jd_finance`, `credit_card`, `bank`, or a stable source key. |
| `account_name` | Human-readable account or card name. |
| `merchant` | Source merchant/counterparty text; do not over-normalize. |
| `description` | Product or transaction summary. |
| `amount` | Positive decimal amount, for example `36.50`. |
| `direction` | `expense`, `income`, or `neutral`. |
| `transaction_type` | See the allowed values below. |
| `payment_method` | Source payment method; retain card tail when visible. |
| `status` | Original status text. |
| `platform_transaction_id` | Empty when absent; never invent one. |
| `merchant_order_id` | Empty when absent; never invent one. |
| `account_last4` | Four digits or empty. |

## Transaction types

```text
purchase, refund, transfer, repayment, installment, fee, interest,
cash_withdrawal, income, investment_buy, investment_sell,
investment_income, topup, withdrawal, adjustment, unknown
```

Classify unclear rows as `unknown` and put them in review rather than forcing a purchase/refund classification.

## Screenshot rules

- Archive the screenshot before creating this CSV.
- Preserve displayed merchant, description, amount, time, status, and payment method verbatim.
- Leave absent IDs and card tails empty.
- Use `neutral` for internal movements or when the display does not establish income/expense.
- Do not create rows for clipped or unreadable transactions; report them as coverage limitations.

## V1.1 optional fields and compatibility

| Field | Meaning / default |
| --- | --- |
| currency | CNY only; absent/empty means CNY; other currencies rejected |
| settlement | cash / debt_only / unknown (default); successful refund explicitly to cash balance is evidence, credit restoration is not |
| transfer_scope | internal / external / unknown (default); internal needs own-account evidence |
| source_reference | archived source reference, e.g. Raw/screenshots/...png; never an instruction to read arbitrary files |
| source_locator | visible page/image/row/location; do not manufacture hidden precision |

Old CSV columns remain supported. Missing type defaults to purchase for the legacy interface, but ambiguous extraction must explicitly use unknown. Missing status is reviewed and excluded from effective totals until confirmed; missing account prevents strong-ID merging. Amount must be finite and nonnegative; direction is not inferred by taking absolute value. Dates may be YYYY-MM-DD, YYYY-MM-DD HH:MM or YYYY-MM-DD HH:MM:SS; preserve visible precision, invalid calendar dates reject the whole file. Platform original columns are retained under _source_raw; normalized extra columns are retained verbatim in raw JSON.

coverage is supplied per import as complete/partial/unknown; screenshots must use partial even when normalized to CSV. A source file is hashed, each source row has an immutable raw record, and observation rows map each batch to a canonical event. Trusted transaction IDs merge only within platform+account+card-tail; order IDs alone do not establish duplicate identity. Identical IDs with conflicting content remain reviewed. No-ID repeats are not merged automatically.
