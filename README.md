# A 股股息门槛回测：方案、方法与实现

供腾讯 WorkBuddy 接手的无数据项目。仓库保留回测方案、版本化策略、Python 确定性引擎、FastAPI/Next.js 查验界面、数据契约和机制测试；**不包含行情、财报、研究卡、数据库、账户账本、回测结果、日志或任何凭据**。

从 [WorkBuddy 接手说明](docs/workbuddy-handoff.md) 开始，再读 [回测方法](docs/backtest-methodology.md) 和 [数据接入契约](docs/data-integration-contract.md)。测试代码与 `templates/` 中的虚构结构示例只是验证工具，不是市场数据或回测绩效。

## 核心方案

- 主窗口：2016-01-04 至 2025-12-31；2026 扩展观察隔离，不计入主绩效。
- 保守每股分红 `D_cons = min(过去365日已实施分红, 最近3个已完整获知财年的每股分红中位数)`。
- 股息率 `Y = D_cons / 未复权收盘价`；门槛 `H = 合格银行大额存单信号利率 + 0.0300`。
- 每月首个交易日收盘筛选；`Y >= H` 才能入选。下一交易日按限价和流动性门尝试一次性买入 1000 股，最多同时持有 20 只，不补仓、不摊薄。
- 每日收盘检查退出，只有 `Y < H` 才触发；下一可成交开盘卖出实际全部股数，不能在信号日回填成交。
- 共享现金、按真实成交现金缺口注资；分红税、费用、公司行动、资金流和盈亏由程序记账，闭合误差不超过 0.01 CNY。
- V4 全窗口排除 `600875 东方电气`，未经新指令不得恢复。

完整经济规则见 [原始方案](docs/backtest-dividend-hurdle-v1.md)，当前运行策略入口为 [V4](config/dividend-hurdle-backtest-v4.yaml)。固定样本 pilot 是独立机制验证方法，不能替代全市场正式结果。

## 安装与启动

当前支持基线为 macOS / Apple Silicon，Python 3.12（与 `.python-version` 和类型检查基线一致）、uv，以及与 pnpm 11.20.0 兼容的 Node.js（应先检查该 pnpm 版本的 engines 要求）。换到 Windows 时需要适配 shell 和 `.venv/bin` 路径。

```bash
pnpm install --frozen-lockfile
cd apps/api
uv sync --frozen
cd ../..
```

两个终端分别执行 `pnpm dev:api` 和 `pnpm dev:web`。访问 `http://127.0.0.1:3000/backtests`；API 健康端点为 `http://127.0.0.1:8000/health`。

数据来源由 WorkBuddy 根据 [所需数据与验收条件](docs/data-integration-contract.md) 自主查找；仓库里的旧供应商适配器只是可复用代码，不规定新平台使用哪个接口。

无数据启动是预期状态：回测列表为空、历史样本报告不存在、正式运行状态为 `BLOCKED`。服务启动会在本地产生被 Git 忽略的空数据库；普通行情页面可能按现有适配器访问公共源，不等于已获得十年回测所需数据。

## 校验与接入顺序

```bash
pnpm check
pnpm backup
pnpm recovery:check
```

空库恢复演练只证明工程机制，不能证明已恢复原项目数据。正式接入时按 [数据契约](docs/data-integration-contract.md) 标准化，再手动导入清单与预检。WorkBuddy 能连接数据源，不代表来源已具备历史点时、退市股、公司行动或银行利率的完整覆盖。

## 目录

| 目录 | 内容 |
| --- | --- |
| `config/` | V1–V4 策略及固定样本机制方案、运行开关 |
| `docs/` | 回测方法、接口、架构、交接与运行说明 |
| `apps/api/app/backtests/` | 信号、事件引擎、历史输入、账本、绩效与执行门 |
| `apps/api/app/runtime/` | 默认禁用、仅手动的确定性任务 |
| `apps/web/` | 回测与研究查验界面 |
| `schemas/` | 数据清单、研究、结果与审计格式 |
| `rules/` | 版本化交易规则与日历；新数据接入须核验适用期 |
| `scripts/` | 手动采集、转换、审计、pilot 与报表程序；旧路径需映射到新输入 |
| `evals/`、`apps/api/tests/` | 验收标准、30 个黄金案例与隔离机制测试 |

仅用于研究与模拟交易。保持 `DISABLED + MANUAL_ONLY`；不接实盘、不自动下单、不把 LLM 接入 Web 或账本，不自动采集或运行正式回测。
