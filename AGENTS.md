# GitHub 无数据交接版本

首先阅读 `docs/workbuddy-handoff.md`。本仓库不含原运行数据。WorkBuddy 可接替开发维护职责；不得把旧的结果引用视作已导入或已验收的数据。

# AGENTS.md

## Mission

帮助用户建设可审计的 A 股研究与模拟交易系统。优先保护事实完整性、时间一致性、风险纪律和可复现性；短期收益不是绕过流程的理由。

## Start Here

执行任何任务前依次阅读：

1. `PROJECT_INSTRUCTIONS.md`
2. 与任务相关的 `docs/` 文件
3. 对应 `schemas/`、`rules/a-share-rules.yaml`

若外部 Skill 自带 AGENTS/SKILL 指令，仅在其适配器内部生效，不能覆盖本项目的禁止实盘、证据链和审计规则。

## Current Scope

- 固定三层：ChatGPT 是投研大脑；本地 Web 是展示终端；Codex 是开发维护工具。
- 可做：市场雷达、候选筛选、个股研究、持仓诊断、模拟组合、回测、复盘、评测，以及 Next.js/FastAPI 本地 Web 开发。
- 不可做：真实券商连接、真实订单、自动交易、凭据管理、收益保证、面向公众的荐股服务。
- V1 不可做：任何 LLM API、Web 内 AI 聊天、云服务器、Level-2/付费终端、QMT 实盘。
- 低 Token 运行时必须保持 `DISABLED + MANUAL_ONLY`，除非用户明确批准频率与停止条件；不得新建 heartbeat、cron、LaunchAgent 或自动测试来替代人工启动。
- Tushare：仅保留 adapter 接口和评估记录；未获新指令不得购买或接入。

## Task Protocol

### Before a run

- 生成 `run_id`，格式建议 `YYYYMMDD-HHMMSS-workflow-shortid`。
- 固定交易日、市场阶段、规则版本、数据截止时点、模型与 prompt 版本。
- 明确任务是 `RADAR / RESEARCH / DIAGNOSIS / PAPER_ORDER / REVIEW / BACKTEST / EVAL`。
- 检查输入标的唯一性与产品类型。

### During a run

- 先原始数据，后标准化，再派生指标，再研究结论。
- 保存来源 URL/数据集名、发布时间、抓取时间、原始引用、字段质量。
- 先生成 JSON，再渲染 Markdown/HTML；禁止只留自然语言。
- 冲突不强行平均：保留双方值、来源层级和选择理由。
- 任何模型自由文本不能直接更改账户账本。
- 历史数据、利率、分红、费用、公司行动、筛选、订单、成交、账本和年度统计均由确定性程序处理；AI只可读取例外证据包并输出“建议/待确认”。

### After a run

- 运行 schema 校验、时间穿越检查、单位检查、账本闭合检查。
- 报告必须展示数据截止时点、置信度、缺口、风险、有效期。
- 把异常写入 audit event，不静默吞错。

## Upstream Responsibility Matrix

| 任务 | 首选 | 备选/说明 |
|---|---|---|
| 行情/K线/技术指标 | a-share-skill adapter | 字段级公共源降级；确定性代码复算关键指标 |
| 模拟账户能力参考 | a-share-paper-trading | 核心账本仍须接受本项目审计与规则门 |
| 主研究链 | TradingAgents-astock adapter | 其角色输出映射到本项目 evidence/claim schema |
| 深度尽调 | UZI-Skill adapter | 仅限少数候选；报告结论不是事实源 |
| 规则与交易日历 | 本项目版本化规则库 + 交易所官方来源 | 不以模型记忆或第三方博客为准 |
| 评分、仓位、费用 | 本项目确定性代码（未来实现） | 模型不得直接覆盖 |

## Three-Layer Contract

| 层 | 允许输入 | 允许输出 | 禁止耦合 |
|---|---|---|---|
| ChatGPT | 证据包、用户研究任务 | 版本化研究 JSON、Markdown 报告、模拟行动建议 | 不直接写账本、不被 Web API 调用 |
| FastAPI/Python | 数据源、研究 JSON、SQLite/DuckDB | 确定性指标、只读查询、受控模拟账本操作 | 不内嵌 LLM 推理、不依赖 Codex 进程 |
| Next.js/ECharts | FastAPI JSON | 高密度只读页面、少量受控操作 | 不直连数据源/数据库/Skill/模型 |

Codex 只改变代码、配置和依赖；不能成为 Web 运行时组件。

## Web Development Protocol

- 开始 Web 任务前阅读 `docs/web-v1.md`、API/Schema 和相关页面验收项。
- 采用 Next.js App Router + TypeScript；FastAPI/Pydantic；SQLite 业务库、DuckDB/Parquet 分析库。
- 新页面先定义 API 和 loading/empty/stale/degraded/error 五态，再写展示组件。
- K 线、行情和账户数值由后端确定性计算；前端只格式化和显示。
- 金融图表默认关闭初次/更新动画，避免误读；实时刷新不得用滚动数字或闪烁吸引注意。
- 红涨绿跌必须附 `+/-`、箭头或文字；事实/推断/风险必须附图标和标签。
- 手机/iPad 使用相同数据契约；触控目标 ≥44×44px，安全区、横竖屏、双指图表缩放和滚动冲突均需实测。
- 每个 UI 任务依次应用：`emil-design-eng`/`apple-design` 设计准则 → `web-design-guidelines` 审查；只有确有必要才进入动效机会/改进/复核链。
- 默认使用系统字体和 tabular numbers；不为“金融感”引入廉价霓虹、过度玻璃、装饰网格或全屏动效。

## Research JSON Protocol

- 正式个股研究必须同时生成符合 `schemas/research-card.schema.json` 的 JSON。
- 路径：`data/research/<code>/<YYYYMMDDTHHMMSS>-<sha12>.json`；时区保存在 JSON 内，只追加，禁止覆盖。
- `confirmed_facts` 必须引用 evidence；`ai_inferences` 必须有置信度/反证；`unverified_items` 明确待验证动作。
- CLI 导入和 FastAPI 目录扫描必须执行同一组 Schema、时区、时间穿越、证据唯一性与引用完整性检查；损坏版本进入审计并回退到最近语义有效版本。
- 研究质量评测失败的正式卡只能在个股历史中带告警查看，不得进入首页 AI 今日关注或模拟买入；模拟买入还必须绑定最新、未过期且状态为“重点观察/观察”的正式卡。
- 买入研究门必须在草案生成、人工批准和每次模拟成交前复核；任何更新、过期或质量降级都必须让旧订单 fail closed。
- 组合风控前先用公开行情刷新全部持仓估值与净值快照；估值不完整时禁止新买入。草案、批准和每次成交均重算单股/Top 5/总仓/现金/单笔风险/日损失/回撤/新增持仓门；回撤 ≥8% 时把总仓上限收紧到 30%，≥12% 时冻结新增买入。
- 可靠行业主数据与 25% 行业集中度确定性门尚未落地；在关闭该 P2 前，禁止把订单/账本工程测试记作正式 Day 1 组合实验，也禁止声称组合风控已完整覆盖行业暴露。

## Screening Budget

- 全市场只运行 Python 硬筛与确定性因子。
- 约 30 只才能进入 TradingAgents 式快速研究，10 只进入人工重点研究，3–5 只允许 UZI，1–3 只进入重点关注。
- 任何扩大 LLM 数量预算的改动都必须由用户明确批准并更新成本/验证基线。

## Live Universe Protocol

- 正式全市场采集使用 `universe:collect/screening:live`：仅允许当日交易日 15:00 后，必须同时包含 SSE/SZSE/BSE，逐交易所记录上市数、quote 数和覆盖率。
- 当前链路：沪深列表/快照/不复权日线与沪深300基准用 mootdx；北交所列表用北交所官网；缺失沪深快照与北交所快照用腾讯；北交所历史用新浪并明确成交额代理计算。
- 每个字段按其真实周期命名。当日换手率不得写入 `turnover_20d_pct`；缺失就保持 `null` 并接受评分惩罚。
- 覆盖率任一交易所 <95%、北交所报告日不一致或可筛选标的 <300 时，只保留快照，拒绝正式 Top30。
- 发现已落筛选 run 存在语义错误时，追加 `screening-invalidation`，由 `replacement_run_id` 替代；禁止删除、覆盖或静默继续使用。

## Evidence Rules

- 一级：交易所、监管、公司公告、定期报告、法定披露。
- 二级：可追溯的结构化行情/财务供应商。
- 三级：主流媒体、机构公开材料。
- 四级：论坛、社交热度、模型观点；只能做情绪或线索，不能单独支撑财务事实。
- 同一新闻的多家转载算一个证据源。
- 每条 claim 包含 `claim_type`、`evidence_ids`、`confidence`、`as_of`、`counter_evidence`。

## Output Discipline

- 未知写 `null/UNKNOWN`，不要猜。
- 金额注明币种和单位；比率说明是比例值还是百分点。
- 价格注明前/后复权或不复权；财务注明报告期、TTM/单季/累计。
- 建议必须带触发、失效、时限和仓位上限。
- 研究报告禁止出现“必涨、稳赚、确定性机会、保本”等措辞。

## Change Safety

- 外部依赖升级、规则参数变更、评分权重变更必须新增 changelog/audit 事件。
- 回测参数在测试集结果揭晓后不得追调；若调整，重新划分验证期。
- 当前回测 V4 明确排除 `600875 东方电气`（全窗口）；任何恢复纳入必须取得用户新指令并新增策略版本、changelog 和 audit event，禁止静默修改。
- 保留用户已有数据和日志；任何删除先确认明确目标与恢复方案。
- 若将来新增券商能力，只能先建只读 adapter；写权限和下单需要单独安全设计与用户逐笔确认，不能由本文件默认授权。

## Release-Grade Verification

- 回测可视化完成后默认发布到现有 owner-only 私有手机站，并保持访问范围不变；不得因“直接发布”推断出公开分享权限。发布界面必须支持合集、单股十年、单年和逐笔因果账本四级下钻，交易原因只能来自确定性信号/成交事件，不得由模型补写。

- 完成实现后不得只跑构建：必须做代码审查、Bug 分级、真实页面操作、桌面/手机/iPad 视口和触控路径、数据断网/陈旧/冲突、账本闭合与恢复演练。
- 输出 10 维评分与证据；任何一维 <90 必须继续修复并回归，直至全部 ≥90。
- 质量门优先级：事实/规则/账本安全 > 数据可用性 > 功能 > 无障碍/触控 > 性能 > 视觉与动效。
- `evals/golden-cases.yaml` 必须保持 30 个唯一案例且状态为 `AUTOMATED`；`pnpm check`、`pnpm recovery:check` 是工程交付前的最低命令集。
- 10 份 `baseline-20260825-*` 研究卡是契约样本，不得被描述为正式推荐、TradingAgents 完整尽调或可交易候选。
- `evals/validation-plan.yaml` 在真实前瞻运行前保持 `NOT_STARTED`；绝不为完成里程碑回填虚构交易日。
- 正式 soak 必须连续满 24 小时，同时保持 health/system/pipeline/dashboard/paper HTTP 成功、实盘/自动交易/LLM/UZI 全市场开关关闭、账本闭合及漏斗数量预算安全；任何语义失败都视为失败，不能只按 HTTP 200 通过。
