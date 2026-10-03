> 无数据迁移版：本文件保留方法与工程设计；历史状态和原始产物未随仓库提供，接手以 workbuddy-handoff.md 为准。

# 低 Token 确定性运行时 V1

## 目的与边界

该运行时把历史回测、数据校验、月度筛选、日度退出检查、下一交易日模拟成交、公司行动、账本和年度统计放在本地 Python 确定性平面。它不是 Agent，也不包含提示词、模型 SDK、模型 API、Web 聊天或 Codex 常驻进程。

当前配置为 `DISABLED + MANUAL_ONLY`。没有 LaunchAgent、cron、heartbeat、自动测试或任何周期任务。未来启用调度必须由用户明确确认频率、市场时段、停止条件和数据范围，并创建新的版本化配置与审计记录。

## 手动入口

所有入口使用同一命令；它只运行用户指定的一项任务，要求新的 `run_id`，输出只追加：

```bash
pnpm runtime:task --task DATA_UPDATE_VALIDATE --run-id YYYYMMDD-HHMMSS-runtime-source --as-of 2026-09-09T15:00:00+08:00
```

| 任务 | 未来建议时点 | 确定性工作 | 当前未满足前的结果 |
| --- | --- | --- | --- |
| `DATA_UPDATE_VALIDATE` | 原始数据导入后 | 清单、来源、SHA-256、点时覆盖验证 | 完成校验并列出缺口，不下载或补造数据 |
| `DAILY_EXIT_CHECK` | 每个交易日收盘后 | 以 `known_at <= signal_at` 检查持仓退出条件 | 阻断，不生成卖单 |
| `MONTHLY_CANDIDATE_SCREEN` | 月度首个交易日收盘后 | 20 席位、股息率门槛、质量门和冻结排序 | 阻断，不生成候选替代结果 |
| `NEXT_OPEN_PAPER_FILL` | 下一可交易日开盘 | 交易状态、开盘限价、滑点、费用、T+1 和成交 | 阻断，不写账本 |
| `CORPORATE_ACTIONS_LEDGER` | 公司行动生效日/每日开盘前 | 分红、税费、送转、公司行动和账本闭合 | 阻断，不写账本 |
| `ANNUAL_PNL_SNAPSHOT` | 年末最后交易日收盘后 | 年度快照、年度/累计盈亏、归因 | 无正式检查点时保持空值 |
| `EXCEPTION_EVIDENCE_PACKAGE` | 每次任务后 | 缺口、冲突、异常与需人工确认项 | 完成紧凑证据包 |

`DATA_UPDATE_VALIDATE` 与证据包任务可以在门未关闭时完成“验证/报告”；其余任务在真实数据和规则门未关闭时一律 `BLOCKED`。`BLOCKED` 不等于失败：它证明系统拒绝把未知内容写成交易、账本或绩效。

## 每次运行的不可变证据

每次手动运行都会生成：

- `data/runtime-runs/<task>/<run_id>.json`：任务状态、截止时间、执行耗时、规则版本、输入哈希、处理计数和摘要；
- `data/exception-packages/<run_id>.json`：只供用户手动提交给 AI 的例外/证据包；
- `state/deterministic-runtime.sqlite3`：运行索引和带哈希的 `DETERMINISTIC_TASK_FINISHED` 审计事件。

文件用排他创建，只追加，不覆盖旧 `run_id`。每个运行报告必须符合 `schemas/deterministic-task-run.schema.json`；证据包必须符合 `schemas/exception-evidence-package.schema.json`。

## 年度盈亏口径

只从正式主回测的闭合检查点计算，扩展观察不混入结果。

```text
年度净盈亏 = 年末总资产 + 当年外部取出 − 年初总资产 − 当年外部注入
截至该年累计盈亏 = 年末总资产 + 累计外部取出 − 累计外部注入
```

分红、费用、税费和滑点只显示为账本归因字段；它们已经反映在年末资产中，因此不得再加减一次。没有正式主回测检查点时，全部年度数字为 `null/空`，禁止显示替代净值或收益。

## AI 例外复核边界

证据包仅包含来源冲突、央企/行业/质量门未知、历史利率/费用税费/公司行动缺失、不可成交、账本不闭合、年度统计异常和规则冲突。用户可以手动将其交给 ChatGPT 复核。

任何 AI 输出都必须标为“建议/待确认”，不能自动回写数据、规则、持仓、订单、成交或账本。日常代码和数据工程优先由用户手动选择 GPT-5.6 Terra；只有规则冻结、严重数据冲突、未来函数风险或正式回测前最终审计，才建议用户手动切换 GPT-5.6 Sol。系统不会自动切换或调用任何模型。

## 正式十年回测门

本运行时不启动正式十年回测。只有 `CD_HISTORY`、`QUALITY_GATE`、`FEE_TAX_RULES`、`POINT_IN_TIME_COVERAGE` 和各数据集覆盖门全部关闭，且策略状态冻结为可执行时，才允许用户手动执行正式回测命令。任何缺失都应形成可审计的阻断，而非用模型、当下名单或合成数据填补。
