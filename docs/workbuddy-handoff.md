# WorkBuddy 接手说明

## 交付范围

用户要求迁移到 GitHub 的是项目及回测方案方法，不是数据备份。本仓库不含原行情、财报、PDF、研究卡、数据库、交易账本、回测绩效、日志、NAS资料、凭据或浏览器会话。原电脑上的这些文件仍保留。

保留代码、版本化经济规则、架构、数据接口、审计方法、测试与虚构契约模板。配置中的固定样本股票、历史版本号和原始证据路径是方法/版本定义，不代表那些数据已提供。原方法中引用的实测覆盖和历史状态不是本仓库的数据保证，目标环境必须重新验证。

## 阅读顺序

1. `PROJECT_INSTRUCTIONS.md` 与 `AGENTS.md`。
2. `docs/backtest-methodology.md`。
3. `config/dividend-hurdle-backtest-v4.yaml` 及其引用的 V1 基线。
4. `docs/data-integration-contract.md` 与 `schemas/backtest-dataset-manifest.schema.json`。
5. `apps/api/app/backtests/`、`apps/api/tests/test_backtest_*.py`。

WorkBuddy 接替开发维护工具；无需 Codex 常驻。AI 可以理解任务、实现 adapter、检查例外并提出建议；计算、历史回放、资金、税费、账本与绩效必须由确定性程序完成。这里不配置模型API、不启用自动任务，也不推断腾讯侧数据源已满足本项目契约。

## 接入后的第一项任务

可把以下内容作为 WorkBuddy 首条任务：

> 请读取本仓库 AGENTS.md、PROJECT_INSTRUCTIONS.md、docs/workbuddy-handoff.md、docs/backtest-methodology.md 和 docs/data-integration-contract.md。先运行无数据工程校验，输出当前策略、输入缺口和新数据源字段映射方案；在得到具体数据源后实现隔离 adapter。保持 V4 经济规则、600875全窗口排除、DISABLED + MANUAL_ONLY。不要下载旧电脑数据、伪造历史输入、调整策略参数、启用定时任务、连接实盘或让模型计算/改写账本。先验证来源与点时覆盖，再导入，通过真实数据门后才讨论正式回测。

## 验证预期

- `pnpm check` 应不需要原项目数据；虚构样例只存在测试临时目录或契约模板。
- 启动后 `/health` 正常；回测列表空、正式readiness不通过、旧样本报告404属于预期，不得为展示填假收益。
- `pnpm backup` 后 `pnpm recovery:check` 可验证新环境的空库恢复；必须清楚区分空库与真实账户恢复。
- 保持30个唯一黄金案例为 AUTOMATED；前瞻验证状态 NOT_STARTED。
- 不自动启动24小时soak或任何调度，不把一次构建算作正式研究/收益验证。

旧文档中有关 Codex、个人工具和发布偏好的描述是原工程背景：未安装的 Skill/MCP 必须重新映射，私有手机站的发布凭据和权限不包含在仓库中。接手不授权扩大共享或部署到公网。

## 本次导出变化

- 新增无数据README、回测方法总览、数据接入契约与本说明。
- `.gitignore` 整目录排除 data/state/reports/backups/logs/secrets 与缓存。
- 不复制旧结果报告、数据缺口实测报告、旧21GB迁移规划、个性化cnequity配置及运行数据。
- 原数据库恢复机制保留；缺数据的历史报告测试改成隔离机制测试，拒绝依赖原电脑的收益数字。
- V1–V4经济规则、引擎、Schema和锁文件保持原值。策略关联的旧证据不提供，不静默改写策略哈希。

本次可复核的工程调整见 [导出变更记录](source-export-changelog.md)；验证结果见 [结构化审查](source-export-review.json)。
