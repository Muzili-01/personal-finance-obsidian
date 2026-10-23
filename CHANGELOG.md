# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### V1.1 — 日常使用与可恢复性

- Separate immutable source observations from canonical events; scope trusted IDs by platform/account and retain weak duplicates for review.
- Allocate refunds deterministically, preserve manual choices, support reversible source/type/settlement/duplicate decisions, and handle cross-month or refund-first imports.
- Add batch receipts, explicit reactivation, overlapping-batch undo, three category scopes and reversible single-transaction overrides.
- Back up and migrate V1 databases; preserve manual refund effects across canonical aliases. Serialize writers and atomically replace derived files, with recoverable failed/pending status.
- Preserve user notes and custom Properties; retain legacy pages, reject broken markers and invalidate analysis by effective-input fingerprint, including comparison inputs.
- Show coverage, unknown settlement/debt, all-month pending items, classifications, monthly navigation and separate refund verification; Bases remains optional.
- Add doctor, isolated synthetic demo, read-only report/review, fingerprint-bound analysis writing and natural-language operation routing for both hosts.
- Validate with synthetic regressions, fault injection, independent scenario/code review and a 10,000-record benchmark; see [V1.1 acceptance](docs/V1.1-acceptance.md) for tested scope and limits.

### Changed

- Promoted the Skill payload to the repository root and retained Codex/Claude Code discovery symlinks.
- Reworked the README around Agent installation and a copyable usage prompt; moved CLI instructions to advanced usage.
- Clarified the privacy boundary between the offline script, cloud-hosted agents, and unencrypted Vault storage.

## [0.1.0] - 2026-07-22

### Added

- Initial public release of the local personal-finance and Obsidian reporting skill.
- CSV import, original-file archiving, SQLite ledger generation, reconciliation, debt snapshots, and Markdown report generation.
- Regression tests and public contribution, security, and licensing documentation.
