# 评测目录

实现期在此保存：

- `golden-cases/`：至少 30 个 point-in-time 黄金样本，原始数据与预期硬门结果。
- `rubrics/`：事实、引用、反方、规则、风险、可操作性人工量表。
- `regression/`：每次 adapter/模型/规则升级的对比结果。
- `baselines/`：沪深300、候选池等权、确定性筛选、人工基线。
- `incidents/`：时间穿越、账本不闭合、错证券、伪引用等复盘。

## 最低发布门

| 检查 | 门槛 |
|---|---:|
| JSON Schema | 100% |
| 时间穿越 | 0 |
| 错证券/错市场 | 0 |
| 强结论引用覆盖 | ≥95% |
| 引用支持 claim | ≥95% 人工抽样 |
| 账户闭合 | 100% |
| 硬风险违规 | 0 |
| UNKNOWN 正确保留 | 100% 关键样本 |

所有结果按 `code_commit + upstream_commits + data_snapshot + rules + scoring + model + prompt` 唯一标识。测试集揭晓后调整参数视为新实验。
