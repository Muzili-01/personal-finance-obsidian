# Analysis Rules

Read `Finance/Data/analysis-context-YYYY-MM.json` before writing analysis. Treat it as the only numerical source; inspect the month note for transaction-level context.

## Output shape

Write inside the existing markers only:

### Consumption habits

Produce two to four short findings. For each finding:

1. State the measured behavior.
2. Cite its amount, count, share, merchant, or comparison.
3. Explain the likely budgeting implication without moral judgment.

### Next-month suggestions

Produce at most three suggestions. Each suggestion must name a controllable action and a measurable limit, review, or decision rule. Prefer the largest actionable category over trivial optimization.

## Guardrails

- Do not infer motivation, addiction, health status, or financial distress from purchases.
- Do not claim a trend without comparable prior-month data.
- Do not include fully refunded purchases in habits or suggestions.
- Label results provisional when `needs_review_count` is nonzero or coverage is partial.
- Separate debt-management suggestions from consumption reduction.
- Do not provide investment, tax, legal, or credit advice beyond the available records.
- Preserve the marker comments so later rebuilds retain the Agent-written text.
