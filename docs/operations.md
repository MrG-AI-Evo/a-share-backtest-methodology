> 无数据迁移版：本文件保留方法与工程设计；历史状态和原始产物未随仓库提供，接手以 workbuddy-handoff.md 为准。

# 本地运行、校验、备份与恢复

## 1. 支持环境

- macOS / Apple Silicon。
- Node.js 20+、pnpm 11、Python 3.11–3.13、uv。
- 服务只绑定 `127.0.0.1`；V1 不需要 Docker、云服务器、模型 API key 或券商凭据。

## 2. 首次安装

在项目根目录运行：

```bash
pnpm install
cd apps/api && uv sync --extra mootdx
```

依赖升级不是日常启动步骤。升级前必须记录版本、许可证和回归结果。

## 3. 启动与停止

终端 A：

```bash
pnpm dev:api
```

终端 B：

```bash
pnpm dev:web
```

浏览器打开 `http://127.0.0.1:3000`。用 `Ctrl+C` 分别停止两个进程。Codex 不属于运行时，关闭 Codex 不影响已经启动的本地服务。

健康检查：`http://127.0.0.1:8000/health`。系统页展示行情来源、更新时间、数据库大小、规则版本和两个永久关闭的实盘开关。

## 4. 导入 ChatGPT 研究卡

1. 复制 `templates/research-card.example.json` 作为结构参考。
2. ChatGPT 完成正式研究后输出纯 JSON；禁止把 Markdown fence 保存进文件。
3. 运行：

```bash
pnpm research:import -- /绝对路径/研究卡.json
```

导入器检查 Schema、时区、未来时间、证据 ID 唯一性与引用完整性，再原子追加到 `data/research/<symbol>/`。同内容重复导入幂等；不同内容永不覆盖旧文件。导入事件写入哈希链审计日志。

证据包和全市场筛选输入分别使用：

```bash
pnpm evidence:import -- /绝对路径/证据包草稿.json
pnpm screening:run -- /绝对路径/全市场输入.json --as-of 2026-08-25T15:00:00+08:00 --run-id 20260825-150000-screening-xxxx
```

筛选输入必须是真实 point-in-time 标准字段；没有 20 日流动性、交易状态和历史长度时系统会排除或惩罚，不允许用当日字段冒充。

当日收盘后可直接运行三交易所采集与筛选：

```bash
pnpm screening:live -- --as-of 2026-08-25T15:00:00+08:00 --run-id 20260825-150000-universe-xxxx
```

命令先把完整快照追加到 `data/normalized/universe/`，再检查 SSE/SZSE/BSE 清单、逐交易所 quote 覆盖率（至少 95%）、北交所报告日和至少 300 只完整历史/流动性标的。失败时不会生成 screening run。若已落 run 后发现语义问题，用 `pnpm screening:invalidate -- ...` 追加失效记录，禁止删除旧 JSON。

## 5. 发布前本地校验

```bash
pnpm check
```

该命令依次验证：JSON Schema/YAML/文档链接、Python lint/类型/测试、Web lint/类型/组件测试和 Next.js 生产构建。任何一步失败都不应标记为可用版本。

开发服务使用独立的 `.next-dev` 缓存，生产构建使用 `.next`，因此运行校验不会改写正在预览页面的开发缓存。

需要浏览器端 E2E 时，先启动 API/Web，再运行：

```bash
pnpm test:e2e
```

## 6. 备份

```bash
pnpm backup
```

备份保存在 `backups/<timestamp>-<id>/`，包括 SQLite 在线一致性备份、DuckDB checkpoint 后的副本、当前策略/规则文件和带 SHA-256 的 `manifest.json`。`backups/` 默认不进入 Git。

建议在以下时点备份：规则/费用变更前、首次正式模拟订单前、每周复盘后、依赖或数据库迁移前。

非破坏性恢复演练：

```bash
pnpm recovery:check
```

该命令只在临时目录核对备份 SHA-256、SQLite 完整性、DuckDB 可打开性和模拟账本闭合，不覆盖 `state/`。正式稳定性门使用 `pnpm soak:24h`；探针持续访问 health/system/pipeline/dashboard/paper，并分别累计 HTTP 失败与禁实盘/禁模型开关、账本闭合、正式漏斗等安全语义失败。逐次证据写入 `state/soak/<run_id>.jsonl`，完成或中断摘要均为追加式文件。短时探针只能记为 smoke。

长时间运行不得依赖 Codex 对话进程。旧 run `20260826-170000-soak-24h-launchagent-final` 已按用户调整顺序主动中止，永久保持 `passed=false/interrupted=true`；1000 次无失败探针不能续算成 24 小时通过。若未来需要综合 soak，必须由用户明确授权后再以新的 run_id 手动启动；不得由本地确定性运行时、Codex heartbeat、cron 或 LaunchAgent 自动创建。最终摘要必须同时证明连续 86,400 秒、失败计数为 0、追加证据完整和 `interrupted=false`。

回测数据和执行：

```bash
pnpm backtest:data-import --manifest /绝对路径/manifest.json
pnpm backtest:preflight --run-id YYYYMMDD-HHMMSS-backtest-preflight
pnpm backtest:run --run-id YYYYMMDD-HHMMSS-backtest-formal --scenario BASE
```

正式清单必须携带 SHA-256、许可证据和全部覆盖断言。门未关闭时 `backtest:run` 只落 `BLOCKED` 记录，不产生绩效。

低 Token 确定性任务默认不调度。只能由用户手动运行一项任务：

```bash
pnpm runtime:task --task DATA_UPDATE_VALIDATE --run-id YYYYMMDD-HHMMSS-runtime-source --as-of 2026-09-09T15:00:00+08:00
```

不要为此命令创建 cron、LaunchAgent、heartbeat 或自动测试。任务产物和仅供人工 AI 复核的例外包均追加保存；`BLOCKED` 只报告缺口，绝不补造数据或写入订单、成交与账本。详见[低 Token 确定性运行时](low-token-deterministic-runtime.md)。

## 7. 恢复（人工、fail closed）

恢复会覆盖当前运行状态，因此不提供“一键自动覆盖”命令：

1. 停止 API 和 Web。
2. 先对当前状态再做一次备份。
3. 核对目标备份的 `manifest.json`、规则版本和文件哈希。
4. 明确要恢复的 `paper.sqlite3` / `analytics.duckdb`；研究 JSON 单独按版本核对。
5. 经用户确认后复制回 `state/`，启动 API，检查系统页和账本闭合，再恢复 Web。

若哈希不一致、版本未知或账本不闭合，保持停止新增模拟买入并记录 incident。

## 8. 常见降级

- 板块/宽度为空：系统会保留最后有效缓存或显示不可用；禁止手工填数冒充实时。
- 个股分时不可用：切换到日 K 不代表分时正常，来源状态分别展示。
- 研究卡损坏：读取最近有效版本并显示告警；修复原文件后用新版本导入，不覆盖。
- 模拟订单不成交：检查人工审批、`earliest_fill_at`、交易时段、实时状态、停牌/涨跌停参数、限价和 T+1。

## 9. 数据目录

- `state/paper.sqlite3`：模拟账户、订单、成交、lot、观察池、审计事件。
- `state/analytics.duckdb`：市场与 K 线缓存。
- `data/research/`：正式研究卡历史（append-only）。
- `data/evidence/`：点时证据包（append-only）。
- `data/screening/`：确定性筛选运行产物（append-only）。
- `backups/`：本地备份（不提交）。
- `rules/`、`config/`：版本化规则与策略。

不要把这些运行时文件上传到公开仓库或交给外部商业服务。
