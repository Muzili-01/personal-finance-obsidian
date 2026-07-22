# personal-finance-obsidian

把支付宝、微信、花呗、白条、信用卡账单和截图整理成可追溯的 SQLite 总账、Obsidian 月报与 Dashboard。

## 安装

需要 Python 3.9+。先克隆仓库，然后在仓库根目录执行对应命令；同一份仓库可以同时安装给 Codex 和 Claude Code。

```bash
git clone https://github.com/Muzili-01/personal-finance-obsidian.git
cd personal-finance-obsidian

# Codex
mkdir -p ~/.agents/skills
ln -sfn "$PWD" ~/.agents/skills/personal-finance-obsidian

# Claude Code
mkdir -p ~/.claude/skills
ln -sfn "$PWD" ~/.claude/skills/personal-finance-obsidian
```

重新打开 Codex 或 Claude Code，让它发现新 Skill。

## 直接使用

附上账单文件或截图，然后复制这条提示词：

```text
使用 $personal-finance-obsidian 处理我提供的账单，当前工作区就是 Obsidian Vault。请先归档原始文件，再识别消费、退款、还款、分期和内部转账，完成跨平台去重并更新所有受影响月份、Dashboard、待确认交易与数据质量报告。全额退款不要出现在消费面板和消费习惯分析中，但必须保留原始记录；不能唯一匹配的退款不要猜，放入待确认。最后基于可验证数据写消费分析和最多三条下月建议。
```

## 能做什么

- 直接导入支付宝、微信支付和统一格式 CSV。
- 归档 PDF、XLSX、邮件和截图，再由 Agent 标准化。
- 区分消费、现金流、还款、分期、手续费与内部转账。
- 关联全额、部分和跨月退款；保留无法唯一匹配的候选。
- 检测支付平台与信用卡中的重复消费。
- 生成 SQLite 总账、Obsidian 月报、Dashboard、负债快照和数据质量报告。

## 隐私边界

Skill 自带脚本不会主动联网、上传账单或发送遥测；它只读取用户指定的文件，并把原始文件、SQLite 和 Markdown 写入指定 Vault。

但使用云端 Codex 或 Claude 时，用户发给 Agent 的账单、截图和提示词可能被传输给相应服务提供商，并受其产品设置、隐私政策和数据保留规则约束。本 Skill 不能保证整个处理链路完全本地；有严格本地要求时，请改用离线模型或先对文件脱敏。

原始账单和数据库会在 Vault 中以未加密形式保存，也可能被 Obsidian Sync、iCloud、Git 或其他备份工具复制。不要把解压密码、完整卡号、证件号或登录凭据写入提示词、配置、日志、Issue 或提交记录；加密压缩包请先在本地解压。

这是一套记账与整理工具，不构成投资、税务、法律或信贷建议。

## 输出结构

```text
Finance/
├── Dashboard.md
├── Raw/<source>/
├── Data/
│   ├── finance.sqlite3
│   ├── config.json
│   └── analysis-context-YYYY-MM.json
├── Months/YYYY-MM.md
├── Reviews/
│   ├── 待确认交易.md
│   └── 数据质量报告.md
└── Accounts/
```

SQLite 是规范数据源，Markdown 是可重新生成的展示结果。分类规则保存在 `Finance/Data/config.json`。

## 高级用法

正常使用只需要调用 Skill，不需要手动操作 CLI。调试解析器、批量重建或人工确认退款时，可以直接运行：

```bash
python3 scripts/finance.py --help
python3 scripts/finance.py init --vault /path/to/vault
python3 scripts/finance.py import --vault /path/to/vault --file /path/to/alipay.csv
python3 scripts/finance.py build-all --vault /path/to/vault
```

完整命令示例见 [references/cli.md](references/cli.md)，账单格式与统一字段见 [references/source-formats.md](references/source-formats.md) 和 [references/normalized-schema.md](references/normalized-schema.md)。

## 仓库结构

```text
SKILL.md              # Agent 工作流与核心约束
scripts/finance.py    # 确定性账务处理
references/           # 格式、口径、字段和高级 CLI 说明
assets/               # 分类配置与统一 CSV 模板
agents/openai.yaml    # Codex 界面元数据
tests/                # 回归与仓库结构测试
```

## 开发

```bash
python3 -m unittest discover -s tests -v
```

提交修改前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

[MIT](LICENSE) © 2026 Muzili-01.
