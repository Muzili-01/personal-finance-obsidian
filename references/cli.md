# 高级用法：CLI

普通用户通过 Codex `$personal-finance-obsidian` 或 Claude Code `/personal-finance-obsidian` 操作，无需手动运行命令。Agent 的正常账务流程须调用此处理器。下列命令供 Agent、调试和高级手动维护使用。

`<SKILL_DIR>` 是包含 SKILL.md 的代码目录，`<VAULT>` 是用户明确指定的 Obsidian Vault；两者通常不同。路径用引号保护。Python 3.9+，运行时仅标准库。先查看实际 `--help` 或子命令 `--help`。

```bash
python3 <SKILL_DIR>/scripts/finance.py doctor --vault <VAULT>
python3 <SKILL_DIR>/scripts/finance.py init --vault <VAULT>
python3 <SKILL_DIR>/scripts/finance.py demo --vault <NEW_EMPTY_PATH>
python3 <SKILL_DIR>/scripts/finance.py import --vault <VAULT> --file statement.csv --coverage partial --json
python3 <SKILL_DIR>/scripts/finance.py archive --vault <VAULT> --file statement.png --source huabei-screenshot --coverage partial
python3 <SKILL_DIR>/scripts/finance.py debt --vault <VAULT> --account 示例白条 --as-of 2026-07-31 --outstanding 5000.00 --due 1000.00 --due-date 2026-08-10
python3 <SKILL_DIR>/scripts/finance.py build-all --vault <VAULT>
python3 <SKILL_DIR>/scripts/finance.py build --vault <VAULT> --month 2026-07
python3 <SKILL_DIR>/scripts/finance.py report --vault <VAULT> --month 2026-07 --json
python3 <SKILL_DIR>/scripts/finance.py review --vault <VAULT> --json
```

doctor、report、review 是只读；init/import/archive/debt/决定/构建是写操作。demo 拒绝任何已存在目录。旧 CLI 和 `import_file` 返回新增计数兼容；import CLI 现在输出 JSON 回执。构建默认重建历史月份（含停用后的空月），使跨月退款/撤销得到一致结果。

## 复核、分类与撤销

```bash
python3 <SKILL_DIR>/scripts/finance.py link-refund --vault <VAULT> --refund-id <RID> --purchase-id <PID> --snapshot <SNAPSHOT>
python3 <SKILL_DIR>/scripts/finance.py unlink-refund --vault <VAULT> --refund-id <RID>
python3 <SKILL_DIR>/scripts/finance.py resolve-review --vault <VAULT> --review-id <REVIEW_ID> --snapshot <SNAPSHOT> --changes-json '{"transaction_type":"purchase"}'
python3 <SKILL_DIR>/scripts/finance.py categorize --vault <VAULT> --transaction-id <TID> --category 学习 --scope transaction --json
python3 <SKILL_DIR>/scripts/finance.py categorize --vault <VAULT> --transaction-id <TID> --category 学习 --scope merchant-future
python3 <SKILL_DIR>/scripts/finance.py categorize --vault <VAULT> --transaction-id <TID> --category 学习 --scope merchant-all
python3 <SKILL_DIR>/scripts/finance.py reclassify --vault <VAULT>
python3 <SKILL_DIR>/scripts/finance.py undo-decision --vault <VAULT> --decision-id <DECISION_ID>
python3 <SKILL_DIR>/scripts/finance.py undo-import --vault <VAULT> --batch-id <BATCH_ID> --json
```

resolve-review 允许 transaction_type、direction、status、settlement、transfer_scope、duplicate_of 或 raw_id（明确选择冲突来源观测）；值须来自明确证据/用户决定。duplicate_of 必须是当前候选规范交易，不允许循环。原始行不变，决定与前后值保留审计。撤销人工退款用 unlink-refund，撤销分类/复核字段/重复决定用 undo-decision。每次账务改变后 build-all；API 返回 artifact_status=pending 表示页面尚未同步。

原生支付宝/微信导出可传 --account-name 指定已确认账户；同一账户须持续使用相同名称，不同账户不得共享标签。账户标签会参与导入批次与交易身份，改标签不能当作修正旧导入；应先撤销误标批次再重新导入。

import 的 coverage 为 complete/partial/unknown，旧 CSV 默认 unknown；--source-reference 记录归档来源。--reactivate 仅在用户明确重新纳入已撤销批次时传入。

## 分析

先读 Data/analysis-context-YYYY-MM.json，使用其中的 input_fingerprint。把 findings、suggestions 两个字符串写入临时 JSON，然后调用：

```bash
python3 <SKILL_DIR>/scripts/finance.py write-analysis --vault <VAULT> --month 2026-07 --fingerprint <FINGERPRINT> --file analysis.json
```

指纹过期会拒绝；不要直接改 AI 标记绕过校验。分析正文不包含管理标记。旧分析备份留在 Backups；脚本不会自动清理。

## 数据与恢复

Finance 包括 Raw、Data、Months、Accounts、Reviews、Imports、Backups、Dashboard.md。SQLite 是规范来源，config.json 是配置，Markdown 是派生结果；用户内容写在自动区之外。可将 assets/Finance.base 手动复制到 Finance，未启用不影响核心流程。

- 数据库迁移用 PRAGMA user_version；旧库迁移前创建 Backups/pre-v11-*.sqlite3。备份不自动删除。
- 恢复前停止该 Vault 的所有写入进程。保留当前 finance.sqlite3 的独立副本，再把指定备份复制回 Data/finance.sqlite3；不要覆盖/删除备份。重新 build-all 会迁移并重建。所有操作仅限明确指定 Vault。若有 SQLite WAL/SHM，先正常关闭连接，不能丢弃仍未合并的日志。
- 单文件写入使用同目录临时文件和原子替换；失败时旧文件完整。多文件不是整体原子事务，report_state=failed/pending 时重建所有页面；不能把已提交总账当未导入。
- 自动区标记损坏：先备份当前页，恢复唯一成对 FINANCE_AUTO_START/END 后重建。旧无标记页面完整备份并在新页历史折叠区保留；不能安全识别用户文字时不猜边界。
- .write.lock 串行化整个 Vault 写流程。若进程异常退出，核实锁内 PID 已终止后才移除锁并重试；活跃锁不绕过。
- 报告生成保留自动区之外的文字和非产品 Properties。生成前已读取的页面若发生编辑会拒绝替换；编辑器仍应避免同时修改自动区。

来源没有同口径汇总时，金额核对始终未核对。coverage=complete 是用户/来源的覆盖声明，不是金额对平。以日期/账户可比性为前提解释趋势，部分数据暂定。

回执 linked_refund_count/unlinked_refund_count 为本次关系变化；total_linked_refund_count 为全部活动关联。report_state 与回执生成失败可由 build-all 恢复，数据库内保留重建所需的批次与撤销回执。
