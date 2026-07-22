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
