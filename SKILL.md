---
name: personal-finance-obsidian
description: Use when a user asks to import, merge, reconcile, or analyze personal bills and payment records in an Obsidian vault, including Alipay, WeChat Pay, Huabei, JD Baitiao, Yu'e Bao, credit-card statements, refunds, installments, screenshots, CSV/XLSX/PDF files, monthly reports, debt snapshots, or a finance dashboard.
---

# Personal Finance Obsidian

## 核心原则

使用 Agent 理解非结构化账单并撰写分析；使用随 Skill 提供的确定性处理器计算金额、关联退款、去重和生成 Obsidian 文件。始终保留原始文件与原始行，不把不确定结果伪装成已确认事实。

## 隐私边界

Skill 自带脚本不会主动联网、上传账单或发送遥测。

使用云端 Codex 或 Claude 时，用户提供的账单、截图和提示词可能被传输给相应服务提供商。本 Skill 不能保证整个处理链路完全本地。提醒有严格本地要求的用户改用离线模型或先脱敏。

原始账单、SQLite 和报告会在 Vault 中以未加密形式保存，并可能被用户启用的同步或备份工具复制。不要索取或记录压缩包密码、登录凭据、完整卡号或证件号；要求用户在本地解压加密文件。

## 默认工作流

1. 确认 Obsidian Vault。当前工作区明显是 Vault 时直接使用；否则只询问一次路径。
2. 按平台和文件类型读取 [source-formats.md](references/source-formats.md)。需要运行处理器时再读取 [cli.md](references/cli.md)。
3. 初始化财务目录并逐个归档输入。支付宝、微信和统一 CSV 可直接导入；XLSX、PDF、HTML、邮件或截图先归档，再按 [normalized-schema.md](references/normalized-schema.md) 提取为统一 CSV。
4. 将花呗、白条和信用卡页面中可验证的待还、应还与还款日写成负债快照。缺失字段留空，不推测。
5. 导入全部账单后重建所有月份，使跨月退款能够修正原消费月。
6. 检查 `Finance/Reviews/待确认交易.md`。多个候选并存时保持待确认；只有用户提供证据后才人工关联并重新构建。
7. 读取 `Finance/Data/analysis-context-YYYY-MM.json` 和 [analysis-rules.md](references/analysis-rules.md)，只替换月报中的分析标记内容，最多生成三条建议。
8. 验证月报、Dashboard 与数据质量报告，并向用户汇报导入数、退款关联、重复项、待确认项、覆盖限制和生成路径。

## 账务约束

修改匹配或统计前读取 [accounting-rules.md](references/accounting-rules.md)。

- 全额退款的消费与退款都不进入消费面板和习惯分析，但继续保留在原始层与现金流层。
- 部分退款仅展示剩余净消费；跨月退款修正购买月份。
- 还款、分期本金与本人账户间转账不算新增消费。
- 跨平台去重只标记规范记录，不删除任一来源行。
- 截图默认属于不完整覆盖；没有汇总值时不声明已经对平。

## 常见错误

- 不要因为商户和金额相同就强行关联多个可能订单。
- 不要把余额宝或零钱通转入转出当作消费，把收益当作普通工资收入。
- 不要信任扩展名、固定表头行号或截图中未显示的信息。
- 不要从退款申请时间推断实际到账时间。
- 不要覆盖用户修改过的分类配置或分析标记内容。

## 高级维护

仅在调试、批量操作或人工复核时直接运行 `scripts/finance.py`。命令和参数见 [cli.md](references/cli.md)；普通用户应通过 `$personal-finance-obsidian` 调用完整流程。
