# Accounting Rules

## Separate views

Maintain both views:

- Consumption: answer what was purchased and in which purchase month.
- Cash flow: answer when money actually moved.

A financed purchase counts as consumption on the purchase date. Its later Huabei, Baitiao, or credit-card repayment counts as debt repayment and cash outflow, not new consumption.

## Refunds

Match in this order:

1. Same merchant order ID or verified original-order reference
2. Same platform transaction lineage
3. Unique prior purchase with compatible platform/account, merchant, amount, payment method, and date
4. Manual confirmation

Require the refund date to be after the purchase and the refund amount not to exceed the purchase's remaining refundable amount.

- Full refund: consumption net is zero; hide both events from consumption tables and behavior analysis.
- Partial refund: show remaining purchase net.
- Cross-month refund: revise the original purchase month; retain refund cash flow in the refund month.
- Ambiguous refund: keep it in review; do not hide a guessed purchase.
- Missing cash-settlement evidence: do not invent the refund arrival date.

## Cross-platform duplicates

Keep all raw rows. Prefer the richer payment-platform row as canonical consumption when a matching credit-card row has the same amount, a date within two days, a similar merchant, and a compatible card tail/payment method.

Do not automatically merge multiple equally plausible candidates.

## Non-consumption events

Exclude these from new consumption:

- Credit-card, Huabei, and Baitiao repayments
- Installment principal
- Transfers between the user's own accounts
- Yu'e Bao/Lingqiantong transfer-in and transfer-out
- Wallet top-ups and withdrawals
- Investment asset conversions

Keep fees and interest visible as financial costs. Keep investment yields as income. Do not treat refund settlement as ordinary income.

## Time and coverage

Use purchase/transaction date for consumption analysis. Use posting date and statement cycle for bank reconciliation. A statement cycle is not a natural month.

Never claim full reconciliation when the source is a screenshot, the platform excludes deleted records, the statement total is unavailable, or unresolved items remain.
