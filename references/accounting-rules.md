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

## V1.1 decisions and provenance

Strong IDs are scoped to platform, account and visible card tail. All observations remain even when they describe one event. Weak no-ID purchases remain distinct and provisional. Cross-platform automatic consumption merging additionally requires a visible payment gateway and matching card tail, unique in both directions. Refunds against a source duplicate reduce the canonical purchase.

Automatic refunds are allocated as a set, with deterministic total-capacity checks, not by whichever SQL row appears first. Repeated runs do not count the current refund against itself. Same order ID with multiple compatible purchases stays ambiguous. Manual choices remain stable; new conflicting candidates are visible. Unlinking records a rejection and pauses automation until the user explicitly chooses again.

Refund success or credit restoration alone does not prove cash arrival. settlement=cash requires evidence; debt_only produces no cash inflow, unknown is disclosed. A successful refund explicitly to a cash balance is evidence. A purchase row marked “refunded” remains a purchase-status observation and creates review, not an invented second refund event. Failed/closed/cancelled events are excluded. Missing success state, unknown types and unknown transfer purpose are reviewed.

New debt-funded purchases are reported gross in their purchase month; a later refund changes purchase-month net consumption, but cannot imply that historical month-end debt was zero. Missing debt snapshots/fields remain unknown. Financial costs are shown separately; asset conversion is not consumption.

Undo deactivates a batch's contribution, not its evidence. Another active batch can still support the same event. Dependent manual relationships remain in history and are not moved onto another transaction. Reactivation of an undone file is explicit. Transaction category overrides outrank merchant rules; future merchant rules use occurrence time, not import time.
