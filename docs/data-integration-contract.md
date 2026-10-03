# WorkBuddy 数据接入契约

这是数据无关的工程接口，不指定 WorkBuddy 已具备哪个数据源。接入新源时先写 adapter，将原始响应标准化为 Parquet，再由本项目程序校验导入；不要让模型把网页摘要直接写入历史表。

## 必需数据集

九类数据集的精确列名与类型以 `apps/api/app/backtests/historical.py` 的 `TABLE_DDL` 为准，清单格式以 `schemas/backtest-dataset-manifest.schema.json` 为准。

| 数据集 | 主要内容 |
| --- | --- |
| prices | 未复权OHLC、成交量/额、证券代码与交易所 |
| security_master | 历史证券身份、产品类型、上市/退市、所有制 |
| security_status | 历史ST/退市整理、交易/停牌状态 |
| dividends | 分红财年、公告、除权、派息、每股金额及类型 |
| dividend_fiscal_years | 已完整决议财年的每股分红；明确零与未知分开 |
| financial_facts | 报告期、行业质量profile、指标值、单位与首次公开时间 |
| corporate_actions | 送转拆并股、现金事件、条款及生效时间 |
| cd_rates | 银行、期限、真实产品年利率、有效期和条款 |
| fee_rules | 按市场、方向和生效期的费率、最低收费、税务条款 |

共同保留 `effective_date`、`known_at`、`source_id`、`source_uri`、`is_synthetic`。抓取时间不能替代首次公开时间；清单另带 `retrieved_at`、`created_at`、SHA-256、行数、日期范围、列集合、来源和许可范围。

正式输入为 `REAL_POINT_IN_TIME` 且原始合成观察数0。测试只能用 `TEST_FIXTURE`，不得提升为正式输入。用当前名单回填历史会造成幸存者偏差；用报告期末替代公告日会造成时间穿越。

## 手动接入顺序

1. 列出目标数据源逐字段能力、许可与历史覆盖。先验证少量边界样本，不自动扩展采集。
2. 原始响应写到本地被忽略的 `data/raw/`，记录来源与哈希；不提交 GitHub。
3. 按上述表标准化，保存为 `data/backtests/parquet/<dataset-version>.parquet`。
4. 生成符合清单 Schema 的 JSON。覆盖断言必须有实际证据，不能靠填 `true` 关闭门。
5. 手动运行 `pnpm backtest:data-import --manifest <绝对路径/manifest.json>`。
6. 使用新的唯一 run_id 执行 `pnpm backtest:preflight --run-id <run_id>`。数据缺失时预期结果为 `BLOCKED`，不生成绩效。
7. 质量、规则、覆盖等硬门真实关闭并完成版本冻结后，才可由用户手动正式运行。

`rules/trading-calendar-2026.yaml` 是版本化规则输入，不能替代2016—2025完整历史日历。V4的2026扩展截止时点是历史配置，不自动延长到今天。

## 换源范围

仅改变采集适配与字段映射，保持股息口径、门槛、排序、成交时间、现金顺序和收益算法。不同源冲突记录双方值、来源层级及选择理由，不能简单平均。不得因新源方便而改用复权价、当日字段代替20日字段，或用单银行产品代替正式双银行中位数。

`scripts/` 保存了旧采集/转换方法，部分默认路径指向未随仓库提供的历史产物。先读 `--help` 和输入代码，将新产物以新run_id接入；不要猜测旧文件内容，不默认安装付费接口或Tushare。
