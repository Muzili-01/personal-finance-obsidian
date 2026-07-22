# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security vulnerability or include real financial records in a report.

Use GitHub's private security-advisory form instead:

https://github.com/Muzili-01/personal-finance-obsidian/security/advisories/new

Include a minimal reproduction using synthetic data, the affected version or commit, impact, and suggested mitigation if known. We will acknowledge the report and coordinate a fix privately.

## Handling financial data

The bundled Python script does not initiate network requests or telemetry. However, bills, screenshots, and prompts supplied to cloud-hosted Codex or Claude may be transmitted to that service provider under the user's account settings and the provider's policies. Vault files and SQLite databases are stored unencrypted unless the user adds separate protection, and sync or backup tools may copy them elsewhere.

Do not share bank statements, payment exports, card numbers, account identifiers, credentials, archive passwords, or identity documents in issues, discussions, pull requests, prompts, or logs. Unzip encrypted archives locally rather than sending passwords to an agent.
