> 无数据迁移版：本文件保留方法与工程设计；历史状态和原始产物未随仓库提供，接手以 workbuddy-handoff.md 为准。

# 系统架构

## 1. 冻结决策

V1 正式采用三层架构，除非用户明确修改，否则后续开发不得改变：

```text
┌──────────────────────────────────────────────┐
│ ChatGPT：AI 投研大脑                         │
│ 市场/板块/个股/财报/新闻/政策/多空/风险/复盘 │
│ 输出：版本化 research-card JSON + 报告       │
└───────────────────┬──────────────────────────┘
                    │ 本地追加文件（无模型 API）
                    ▼
┌──────────────────────────────────────────────┐
│ FastAPI + Python：数据、计算与领域核心        │
│ 多源行情 → 标准化 → 指标 → 研究索引 → 模拟账本│
│ SQLite（业务）+ DuckDB/Parquet（行情分析）   │
└───────────────────┬──────────────────────────┘
                    │ versioned REST/JSON
                    ▼
┌──────────────────────────────────────────────┐
│ Next.js + React + ECharts：本地展示终端       │
│ 桌面 / 手机 / iPad，自适应、高密度、少操作   │
└──────────────────────────────────────────────┘

Codex：只负责开发、维护、测试和未来自动化升级；
不是运行时组件，Web 正常使用不需要 Codex 开启。
```

## 2. 设计原则

1. **证据先于观点**：事实、计算、推断、风险和未知分层。
2. **确定性核心**：身份、复权、指标、筛选、评分汇总、费用、仓位、账本和风控由 Python 计算。
3. **展示层无业务真相**：React/ECharts 不计算或持久化权威金融结果。
4. **时间点正确**：每个数据点和研究结论都可回答“当时是否已知”。
5. **字段级降级**：不同字段可以来自不同来源；来源失败不覆盖较优字段。
6. **追加与可回放**：原始数据、研究、订单和审计只追加或显式更正。
7. **研究/模拟隔离**：研究只能提出模拟行动，确定性规则与人工批准才能修改模拟账本。
8. **上游可替换**：三套 Skill 均通过 adapter；任何上游不得成为唯一真相。
9. **本地优先**：macOS/Apple Silicon 原生运行，不依赖云、Docker、模型 API 或 Codex 常驻。
10. **质量可证明**：每个页面/API/规则都有自动测试、五态和审计证据。

## 3. 工程拓扑

```text
A股操盘测试/
├── apps/
│   ├── web/                    # Next.js App Router + TypeScript
│   │   ├── app/                # /、/stocks/[symbol]、/watchlist、/portfolio、/decisions
│   │   ├── components/         # shell、cards、tables、charts、research
│   │   └── lib/                # typed API client、formatters、view models
│   └── api/                    # FastAPI
│       ├── app/api/routes/     # REST routers
│       ├── app/models.py       # Pydantic 领域与 API 模型
│       ├── app/services/       # application services
│       ├── app/adapters/       # public sources + Skills
│       ├── app/repositories/   # SQLite/DuckDB/文件 repositories
│       ├── app/sources/        # 腾讯/东财/新浪直接源
│       ├── app/cli/            # 导入、筛选、备份、恢复、稳定性门
│       ├── app/backtests/      # 点时数据、历史事件、账本与正式执行门
│       ├── app/runtime/        # 默认禁用的低Token确定性任务与审计索引
│       └── tests/
├── data/
│   ├── raw/                    # 原始抓取，只追加
│   ├── normalized/             # 标准字段快照
│   ├── market/                 # Parquet OHLCV/分钟数据
│   ├── research/<code>/        # 版本化研究卡 JSON
│   ├── evidence/<code>/        # point-in-time 证据包
│   ├── screening/<date>/       # 300→30 运行产物
│   ├── screening-invalidations/# 失效 run 的追加式审计记录
│   ├── corporate-actions/      # 显式公司行动输入
│   ├── backtests/              # 回测 Parquet 与不可变数据清单
│   ├── derived/                # 可重建指标/宽度/板块
│   └── snapshots/              # point-in-time 回放快照
├── state/
│   ├── app.sqlite3             # 自选、研究索引、审计、配置
│   ├── paper.sqlite3           # 模拟账本，独立备份/校验
│   ├── analytics.duckdb        # 行情 Parquet 视图与分析查询
│   ├── backtests.sqlite3       # 回测运行、检查点和哈希链审计
│   └── backtests.duckdb        # 回测专用历史点时表
│   └── deterministic-runtime.sqlite3 # 手动任务运行与哈希审计
├── schemas/                    # JSON Schema，跨层权威契约
├── rules/                      # 日期版本化交易/费用规则
├── config/                     # 非秘密配置
├── docs/                       # 架构、SOP、执行/验收文档
├── evals/                      # 黄金样本、10维评分和回归证据
├── reports/                    # 研究与复盘的人可读视图
└── templates/                  # 雷达、个股研究、持仓诊断、复盘模板
```

前端和后端为两个独立运行进程。V1 不引入 Node 服务端直接读 SQLite，也不让 FastAPI 代理模型。

## 4. 数据与存储职责

### 4.1 SQLite：业务与审计

`app.sqlite3` 保存：证券主数据索引、自选股、候选状态、研究文件索引、数据源健康、运行记录和审计事件。

`paper.sqlite3` 保存：模拟账户、订单、成交、lot、现金事件、公司行动、每日净值与对账结果。独立数据库降低展示/行情写入对账本的影响。

SQLite 开启 WAL、foreign keys、busy timeout；写操作使用短事务。数据库迁移显式编号并可回滚；备份先 checkpoint 再复制。

### 4.2 DuckDB + Parquet：行情与分析

- Parquet 是大体量 K 线/分钟/派生结果的持久格式，按 `asset/interval/year` 分区。
- DuckDB 负责跨分区查询、宽度/板块/收益曲线聚合和回测读取。
- DuckDB 结果可重建，不保存不可恢复的唯一业务状态。
- 行情落盘采用临时文件 → 校验 → 原子替换/追加 manifest，避免半文件。

### 4.3 文件：研究桥梁

正式研究卡路径：`data/research/<6位代码>/<YYYYMMDDTHHMMSS>-<内容哈希12位>.json`；完整时区保存在 JSON 的 `as_of/created_at`。

证据包路径：`data/evidence/<6位代码>/<YYYYMMDDTHHMMSS>-<内容哈希12位>.json`。筛选产物路径：`data/screening/<YYYY-MM-DD>/<run_id>.json`。全市场快照路径：`data/normalized/universe/<YYYY-MM-DD>/<run_id>.json`（本地运行数据，不入 Git）。失效筛选另写 `data/screening-invalidations/<YYYY-MM-DD>/`；原 run 永不删除。所有正式产物均只追加。

写入协议：

1. ChatGPT 生成结构化 JSON。
2. 先按 Schema 校验，再检查时区、时间穿越、证据 ID 唯一性和所有证据引用；CLI 与目录扫描使用同一验证器。
3. 写入临时文件并 `fsync`，再原子移动到目标路径。
4. FastAPI watcher/启动扫描建立索引；哈希防重复。
5. 无效文件不覆盖最新有效版本，记录 `RESEARCH_IMPORT_REJECTED`。
6. API 返回最新语义有效版本，同时暴露历史列表和降级原因；研究质量评测失败的版本只允许在个股历史中带告警查看，不进入首页候选或模拟买入。

## 5. 后端模块

### 5.1 Market Data Plane

- `SourceAdapter`：mootdx、腾讯、东财、新浪、同花顺、财联社、百度、AKShare。
- `RateLimiter/Cache`：按域名串行/并发预算、缓存、指数退避和熔断。
- `Normalizer`：证券 ID、交易所、时区、单位、OHLCV、复权、报告期。
- `FieldResolver`：字段级选源、冲突容差、质量和 stale 判定。
- `MarketRepository`：原始响应、Parquet 和 point-in-time 快照。
- `FeatureEngine`：周期聚合、MA/MACD/RSI/BOLL、宽度、板块、收益/回撤。

### 5.2 Research Plane

- `ResearchImporter`：Schema、路径、时间、证据引用、枚举和版本校验。
- `ResearchRepository`：文件为正文，SQLite 为索引；索引可从文件重建。
- `EvidenceBundleRepository`：逐条内容哈希、点时时间门、冲突引用和原子追加。
- `UniverseCollectionService/Repository`：当日收盘后聚合沪深北清单/快照/历史，计算点时覆盖门并保存标准快照。
- `ScreeningService/Repository`：全市场硬门、300→30 固定评分、确定性排序、追加式运行产物和失效 run 跳过规则。
- `TradingAgentsAdapter/UZIAdapter`：手动结构化桥梁，不执行模型 API；分别强制七角色与 Top3–5 人工触发。
- `ResearchEvaluation`：双向证据、独立证据域、时间穿越、失效条件与强结论覆盖门。
- `ResearchViewService`：将最新/历史研究卡转成 Web DTO，不改变研究内容。

### 5.3 Paper Portfolio Plane

事件：`AccountCreated / OrderProposed / OrderApproved / OrderRejected / OrderFilled / OrderCancelled / CorporateActionApplied / ValuationMarked / Reconciled / ReviewAttached`。

对象：`PaperAccount`、`PaperOrder`、`Fill`、`Lot`、`CashEvent`、`Position`、`PortfolioSnapshot`。所有写入先走规则引擎和幂等键。

订单状态：`DRAFT → PENDING_APPROVAL → APPROVED → QUEUED → PARTIAL/FILLED/CANCELLED/REJECTED/EXPIRED`。

持仓估值通过 `StockService` 并发读取可追溯公开行情，事务更新 `last_price/source_timestamp` 并追加分钟级净值快照。组合页和首页读取前刷新；买入风控读取前也刷新，任一持仓估值缺失、陈旧或早于账本记录即 fail closed。该流程只读行情，不接券商，也不改变持仓数量。

### 5.4 Risk & Rules Plane

- 按 `trade_date + venue + board + asset_type + risk_status` 解析规则快照。
- 处理 T+1、停牌、涨跌幅/无涨跌幅期、价格笼子、最小/最大申报量、ST、费用、印花税、滑点和除权除息。
- 在草案、人工批准和每次模拟成交前，按最新估值复算单股仓位、总仓位、现金、风险预算、当日新增持仓数、日内损失与最大回撤。
- 规则未知时 fail closed；模拟历史按当日生效版本回放。

### 5.5 Deterministic Backtest Plane

- `HistoricalDataRepository` 只导入带 Schema、SHA-256、来源/许可和覆盖断言的 Parquet；正式模式拒绝合成原始观察。
- `HistoricalFrameBuilder` 按 `known_at <= signal_at` 组装未复权行情、身份/风险/质量、分红、利率、费用和公司行动；关键冲突 fail closed。
- `DeterministicHistoryRunner` 固定公司行动/到账→卖出→买入→估值顺序，生成总账户、单股子账、现金流和每日检查点。
- `BacktestRepository` 使用独立 SQLite 保存运行、分页检查点和哈希链事件；正式绩效必须同时通过真实点时、无合成、时间穿越和 0.01 CNY 账本门。
- 主窗口固定为 2016-01-04—2025-12-31；2026 扩展观察使用独立 segment，禁止混入主收益。

### 5.6 Low-Token Deterministic Runtime

- `config/deterministic-runtime-v1.yaml` 默认 `DISABLED + MANUAL_ONLY`；不创建 cron、LaunchAgent、heartbeat 或自动测试。
- `DeterministicRuntimeService` 是本地任务编排器：数据/来源校验、日度退出、月度候选、下一开盘成交、公司行动/账本、年度统计及例外证据包均由确定性代码执行。
- 每项手动任务携带 `run_id`、数据截止时间、执行耗时、规则版本、输入 SHA-256、计数、阻断和结构化摘要；产物只追加到 `data/runtime-runs/` 与 `data/exception-packages/`。
- 运行时在未关闭真实点时/规则门时 fail closed，不产生订单、成交、账本写入、年化收益或替代历史结果。
- 例外包是只读的人工 AI 复核输入，AI 不能自动回写。

## 6. REST API 边界

统一前缀 `/api/v1`，时间为带时区 ISO 8601，金额用 CNY decimal 字符串或明确精度的整数分，比例统一为 decimal ratio。所有响应含：

```json
{
  "data": {},
  "meta": {
    "as_of": "2026-08-24T15:00:00+08:00",
    "retrieved_at": "2026-08-24T15:00:05+08:00",
    "quality": "A",
    "stale": false,
    "degraded": false,
    "sources": []
  },
  "errors": []
}
```

当前核心端点（均位于 `/api/v1`，健康检查除外）：

- `GET /dashboard`：六指数、宽度、板块、研究关注与模拟摘要。
- `GET /stocks/{symbol}/quote|bars|research|research/latest`。
- `GET/POST/DELETE /watchlist`：唯一的普通用户偏好写操作。
- `GET /paper`、`GET /decisions`；`POST /paper/orders` 及人工 `approve|reject|simulate-fill`。
- `GET /pipeline`、`/pipeline/screening/latest`、`/pipeline/research/{symbol}/evaluation`。
- `GET /system`；`GET /health` 位于根路径。
- `GET /backtests|/backtests/readiness|/backtests/{run_id}`：运行列表、正式执行门、分页检查点/事件和单股子账。
- `GET /runtime|/runtime/runs|/runtime/runs/{run_id}|/runtime/runs/{run_id}/exception-package`：默认禁用的任务配置、最近手动运行、只读审计状态和不可变例外包导出；没有 Web 触发执行端点。

OpenAPI 是前端客户端的权威来源；生成/手写 TypeScript 类型必须通过契约测试防漂移。

## 7. 前端模块与路由

- `/`：市场概览、宽度、板块、AI 关注、模拟账户。
- `/stocks/[symbol]`：行情、分时/K线/成交量、指标、研究卡和历史。
- `/watchlist`：自选、AI 候选与筛选阶段。
- `/portfolio`：模拟账户、持仓、净值、回撤、风险和事件。
- `/decisions`：研究版本、行动建议、订单、成交、复盘的时间线。
- `/system`：数据源健康、更新时间、降级、本地服务和低 Token 确定性任务状态（只读）。
- `/backtests`、`/backtests/[run_id]`：窗口、覆盖、阻塞、组合/单股指标、资金流、检查点和证据载荷。

Server Components 构建页面壳和首屏数据，ECharts/需要交互的组件使用 Client Components。客户端只轮询 FastAPI，不直接触碰数据库、文件、Skill 或外部来源。

## 8. 刷新与陈旧策略

- 前台页可见且交易时段：指数/宽度 15 秒、个股 quote 10 秒、板块 60 秒、账户 15 秒。
- 非交易时段：全部降为 5 分钟；研究文件变更通过 30 秒轮询或后端文件 watcher 入库。
- K 线历史按区间缓存；当前分钟/日只增量刷新。
- 浏览器隐藏时暂停高频轮询；恢复时先检查 `as_of`，不并发补发全部请求。
- 同一资源只允许一个后端 refresh lock；其他请求返回最近有效缓存并标 stale。
- 页面必须显示数据更新时间、来源、stale/degraded/error，不使用闪烁表示更新。

## 9. Skill Adapter 边界

- `AShareSkillAdapter`：工具层；行情/K线/指标/事件/纸面交易能力映射到领域接口。
- `TradingAgentsAdapter`：主研究结构；只读取 evidence bundle，输出 claims/情景，不直接写总分/订单。
- `UZIAdapter`：只接受最终 3–5 只候选和明确尽调问题，报告拆成证据/计算/观点。
- `TushareAdapter`：接口占位，V1 禁用且无依赖。

当前 `mootdx 0.11.7` 作为独立公共行情 adapter 已安装并锁定 tag/commit，只提供已核验的不复权 K 线备援；AKShare 1.18.94 虽已安装，但因其东方财富底层端点实探失败而由配置显式禁用。三个研究上游尚未执行其运行时代码，精确 commit 必须在真正安装前冻结。

## 10. 响应式与触控架构

- 断点不是设备名单，而是布局能力：`<768` 单栏手机；`768–1199` iPad/双栏；`≥1200` 桌面多栏。
- 支持 iOS Safari/macOS Safari/Chrome 当前稳定版；尊重 safe-area inset、动态工具栏和横竖屏。
- 触控目标 ≥44×44px；不依赖 hover；所有 hover 信息可通过点击/键盘获得。
- 图表手势：单指移动十字光标，双指缩放，横向 pan 不劫持页面纵向滚动；提供显式“重置缩放”。
- 表格在手机上转为优先字段卡片或带明确滚动边界的表格，不把整页设成水平滚动。
- 关键操作可键盘完成，焦点可见；手机/iPad 不显示仅鼠标可用的细小工具栏。

## 11. UI 与动效架构

- 风格：冷静、专业、清晰；暗色优先并支持系统浅色；系统字体、tabular numbers。
- 中国市场语义：红涨绿跌，但 `+/-`、箭头和文字同时存在。
- 事实/推断/风险使用图标 + 文本 + 色彩三重编码。
- ECharts `animation: false` 用于 K 线、分时、净值和实时更新；数据更新保持坐标/缩放稳定。
- 频繁导航、键盘操作、列表排序、行情刷新不动画。
- 仅允许按压反馈、偶发 popover/toast/状态切换等有目的动效；通常 100–250ms，强 ease-out，只动 transform/opacity。
- 支持 `prefers-reduced-motion`、`prefers-contrast`；玻璃/模糊材料只用于少量浮层，不叠加、不降低数字可读性。

## 12. 可观测性与恢复

- 每个请求有 `request_id`，每次数据/研究/模拟运行有 `run_id`。
- JSON 日志记录延迟、缓存命中、源降级、Schema 失败、规则版本和审计事件；不记录凭据。
- 启动时检查数据库迁移、规则版本、研究目录、可写性和 DuckDB/Parquet manifest。
- `app.sqlite3` 可由研究文件/配置/原始清单部分重建；`paper.sqlite3` 每日备份并做账本校验。
- 数据源全失败时 Web 保持最近快照并明确离线/陈旧，不白屏、不造数。
- `pnpm recovery:check` 只把备份恢复到临时目录，核验 SHA-256、SQLite、DuckDB 和账本链，不覆盖当前状态。
- `pnpm soak:24h` 是 24 小时健康与安全语义门：连续探测 health/system/pipeline/dashboard/paper，并核对禁实盘/禁自动交易/禁模型开关、账本闭合与漏斗预算；短探针只能记录为 smoke，不能替代正式门。

## 13. Architecture Decision Records

实现期创建并冻结：

- ADR-001：三层架构与无模型 API。
- ADR-002：SQLite 业务、DuckDB/Parquet 分析的职责分离。
- ADR-003：版本化研究 JSON 导入协议。
- ADR-004：字段级数据解析与缓存熔断。
- ADR-005：事件溯源模拟账本与日期规则。
- ADR-006：Next.js 只经 FastAPI 访问本地数据。
- ADR-007：响应式/触控和克制动效基线。
