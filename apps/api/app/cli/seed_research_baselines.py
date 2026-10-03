from __future__ import annotations

from typing import TypedDict

from app.cli.research_import import import_research_payload
from app.core.settings import Settings
from app.repositories.evidence import EvidenceBundleRepository


class BaselineSecurity(TypedDict):
    symbol: str
    name: str
    exchange: str
    sector: str


SECURITIES: tuple[BaselineSecurity, ...] = (
    {"symbol": "600519", "name": "贵州茅台", "exchange": "SSE", "sector": "食品饮料"},
    {"symbol": "300750", "name": "宁德时代", "exchange": "SZSE", "sector": "电力设备"},
    {"symbol": "601318", "name": "中国平安", "exchange": "SSE", "sector": "保险"},
    {"symbol": "600036", "name": "招商银行", "exchange": "SSE", "sector": "银行"},
    {"symbol": "601088", "name": "中国神华", "exchange": "SSE", "sector": "煤炭"},
    {"symbol": "600900", "name": "长江电力", "exchange": "SSE", "sector": "公用事业"},
    {"symbol": "688981", "name": "中芯国际", "exchange": "SSE", "sector": "半导体"},
    {"symbol": "002594", "name": "比亚迪", "exchange": "SZSE", "sector": "汽车"},
    {"symbol": "300760", "name": "迈瑞医疗", "exchange": "SZSE", "sector": "医疗器械"},
    {"symbol": "600941", "name": "中国移动", "exchange": "SSE", "sector": "通信"},
)

AS_OF = "2026-08-25T15:00:00+08:00"
CREATED_AT = "2026-08-25T16:00:00+08:00"


def _disclosure_url(security: BaselineSecurity) -> str:
    if security["exchange"] == "SSE":
        return "https://www.sse.com.cn/disclosure/listedinfo/regular/"
    return f"https://www.cninfo.com.cn/new/fulltextSearch?keyWord={security['symbol']}"


def _market_url(security: BaselineSecurity) -> str:
    prefix = "sh" if security["exchange"] == "SSE" else "sz"
    return f"https://qt.gtimg.cn/q={prefix}{security['symbol']}"


def build_baseline(security: BaselineSecurity) -> dict[str, object]:
    symbol = security["symbol"]
    filing_id = f"ev-{symbol}-filing-index"
    market_id = f"ev-{symbol}-market-close"
    return {
        "schema_version": "1.0.0",
        "record_type": "CONTRACT_BASELINE",
        "research_id": f"baseline-20260825-{symbol}",
        "symbol": symbol,
        "name": security["name"],
        "exchange": security["exchange"],
        "as_of": AS_OF,
        "created_at": CREATED_AT,
        "horizon": "20_trading_days",
        "status": "谨慎",
        "score": 45,
        "confidence": 0.9,
        "summary": (
            "仅完成法定披露入口与公开行情可用性基线核验；"
            "关键财务、估值和催化尚未逐项交叉验证，不进入模拟盘。"
        ),
        "core_thesis": [
            {
                "text": "当前证据包只足以确认研究对象与正式数据入口，不足以形成可执行投资结论。",
                "evidence_ids": [filing_id, market_id],
                "confidence": 0.96,
            }
        ],
        "bull_case": [
            {
                "text": "法定披露入口可追溯，可继续提取定期报告和公司公告。",
                "evidence_ids": [filing_id],
                "confidence": 0.98,
            },
            {
                "text": "公开行情入口可用于后续确定性量价复算与新鲜度检查。",
                "evidence_ids": [market_id],
                "confidence": 0.95,
            },
        ],
        "bear_case": [
            {
                "text": "最新财务报表、关键经营指标和估值尚未从法定披露中逐项提取。",
                "evidence_ids": [filing_id],
                "confidence": 0.99,
            },
            {
                "text": "单一公开行情入口不能替代独立二源交叉验证，也不能支撑交易判断。",
                "evidence_ids": [market_id],
                "confidence": 0.99,
            },
        ],
        "catalysts": [],
        "risk_flags": [
            {
                "text": "研究域缺失：财务、估值、公告事件、政策和行业风险尚未完成点时核验。",
                "severity": "HIGH",
                "evidence_ids": [filing_id, market_id],
            }
        ],
        "invalidation_conditions": [
            {
                "condition": "任一代码、名称、交易所或数据时间戳无法由独立来源复核",
                "observable": "证据冲突、未知字段或新鲜度超限",
                "action": "标记排除并停止进入筛选漏斗后续阶段",
            }
        ],
        "confirmed_facts": [
            {
                "text": f"研究对象标识为 {security['name']}（{symbol}，{security['exchange']}）。",
                "as_of": AS_OF,
                "evidence_ids": [filing_id, market_id],
            }
        ],
        "ai_inferences": [
            {
                "text": "因关键研究域尚未覆盖，维持‘谨慎’比给出方向性结论更符合证据强度。",
                "confidence": 0.97,
                "supporting_evidence_ids": [filing_id],
                "counter_evidence_ids": [market_id],
            }
        ],
        "unverified_items": [
            {
                "text": "最新定期报告的收入、利润、现金流、资产负债和审计意见",
                "impact": "CRITICAL",
                "verification_action": "从法定披露 PDF 提取并与结构化字段复核",
                "deadline": None,
            },
            {
                "text": "估值、行业比较、催化剂与逻辑失效阈值",
                "impact": "HIGH",
                "verification_action": "完成 TradingAgents 式七角色研究后再评估",
                "deadline": None,
            },
        ],
        "evidence": [
            {
                "evidence_id": filing_id,
                "title": f"{security['name']}法定定期报告查询入口",
                "source_id": f"{security['exchange']}:official-disclosure",
                "source_uri": _disclosure_url(security),
                "published_at": None,
                "as_of": AS_OF,
                "retrieved_at": CREATED_AT,
                "quality": "A",
                "excerpt": "法定披露查询入口；本基线未把未提取的财务字段视为事实。",
            },
            {
                "evidence_id": market_id,
                "title": f"{security['name']}腾讯公开行情入口",
                "source_id": "Tencent:public-quote",
                "source_uri": _market_url(security),
                "published_at": None,
                "as_of": AS_OF,
                "retrieved_at": CREATED_AT,
                "quality": "B",
                "excerpt": "用于对象标识和行情可用性核验；不把行情接口当作财务事实源。",
            },
        ],
        "data_freshness": {
            "filing_index": {
                "as_of": AS_OF,
                "retrieved_at": CREATED_AT,
                "stale": False,
                "source_ids": [f"{security['exchange']}:official-disclosure"],
                "notes": "入口已核验；具体定期报告字段仍列为未验证。",
            },
            "market": {
                "as_of": AS_OF,
                "retrieved_at": CREATED_AT,
                "stale": False,
                "source_ids": ["Tencent:public-quote"],
                "notes": "收盘点时基线；后续研究必须独立二源复核。",
            },
        },
        "model_notes": (
            f"跨行业研究契约基线：{security['sector']}。未调用任何 LLM API，未给出买卖建议。"
        ),
        "scoring_version": "baseline-readiness-2026-08-25-v1",
        "ruleset_version": "a-share-rules-2026-08-25-v1",
        "supersedes_research_id": None,
        "valid_until": "2026-08-26T09:15:00+08:00",
    }


def build_evidence_bundle(security: BaselineSecurity) -> dict[str, object]:
    symbol = security["symbol"]
    return {
        "schema_version": "1.0.0",
        "bundle_id": f"bundle-baseline-20260825-{symbol}",
        "symbol": symbol,
        "name": security["name"],
        "exchange": security["exchange"],
        "as_of": AS_OF,
        "data_cutoff": AS_OF,
        "created_at": CREATED_AT,
        "evidence": [
            {
                "evidence_id": f"ev-{symbol}-filing-index",
                "domain": "FILING",
                "statement": "法定定期报告查询入口已核验；具体财务字段尚未提取。",
                "source_id": f"{security['exchange']}:official-disclosure",
                "source_tier": "P0",
                "source_uri": _disclosure_url(security),
                "published_at": None,
                "as_of": AS_OF,
                "retrieved_at": CREATED_AT,
                "quality": "A",
                "independent_event_id": None,
            },
            {
                "evidence_id": f"ev-{symbol}-market-close",
                "domain": "MARKET",
                "statement": "腾讯公开行情入口已用于对象标识与收盘时点可用性核验。",
                "source_id": "Tencent:public-quote",
                "source_tier": "P2",
                "source_uri": _market_url(security),
                "published_at": None,
                "as_of": AS_OF,
                "retrieved_at": CREATED_AT,
                "quality": "B",
                "independent_event_id": None,
            },
        ],
        "conflicts": [],
        "missing_domains": ["FUNDAMENTAL", "NEWS", "POLICY", "SENTIMENT"],
    }


def main() -> None:
    settings = Settings()
    evidence_repository = EvidenceBundleRepository(settings)
    for security in SECURITIES:
        evidence_target = evidence_repository.import_draft(build_evidence_bundle(security))
        print(evidence_target)
        payload = build_baseline(security)
        target = import_research_payload(settings, payload)
        print(target)


if __name__ == "__main__":
    main()
