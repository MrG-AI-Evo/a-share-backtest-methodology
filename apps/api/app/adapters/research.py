from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from app.core.settings import Settings
from app.models import AdapterHealth

REQUIRED_ROLES = {
    "MARKET",
    "FUNDAMENTAL",
    "NEWS",
    "SENTIMENT",
    "POLICY",
    "HOT_MONEY",
    "UNLOCK_REDUCTION",
}


class ResearchAdapterError(RuntimeError):
    pass


def _validator(settings: Settings, filename: str) -> Draft202012Validator:
    schema = json.loads((settings.schema_dir / filename).read_text("utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchAdapterError(f"无法读取上游适配结果: {exc}") from exc
    if not isinstance(payload, dict):
        raise ResearchAdapterError("上游适配结果根节点必须是对象")
    return payload


def _evidence_ids(bundle: dict[str, Any]) -> set[str]:
    evidence = bundle.get("evidence")
    if not isinstance(evidence, list):
        return set()
    return {
        str(item["evidence_id"])
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
    }


def _all_claims(payload: dict[str, Any]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    roles = payload.get("roles")
    if isinstance(roles, list):
        for role in roles:
            if isinstance(role, dict) and isinstance(role.get("claims"), list):
                claims.extend(item for item in role["claims"] if isinstance(item, dict))
    for key in ("bull_case", "bear_case", "risk_debate", "manager_synthesis"):
        value = payload.get(key)
        if isinstance(value, list):
            claims.extend(item for item in value if isinstance(item, dict))
    return claims


class TradingAgentsAdapter:
    """Validate manually exported role output; never invokes an LLM in V1."""

    version = "trading-agents-manual-bridge-v1"

    def __init__(self, settings: Settings) -> None:
        self._bundle_validator = _validator(settings, "evidence-bundle.schema.json")
        self._result_validator = _validator(settings, "trading-agents-result.schema.json")

    def import_result(self, bundle_path: Path, result_path: Path) -> dict[str, Any]:
        bundle = _load(bundle_path)
        result = _load(result_path)
        for name, validator, payload in (
            ("evidence bundle", self._bundle_validator, bundle),
            ("TradingAgents result", self._result_validator, result),
        ):
            errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
            if errors:
                raise ResearchAdapterError(f"{name} Schema 失败: {errors[0].message}")
        if result["bundle_id"] != bundle["bundle_id"] or result["symbol"] != bundle["symbol"]:
            raise ResearchAdapterError("TradingAgents 结果与证据包标的/版本不一致")
        roles = {str(role["role"]) for role in result["roles"]}
        if roles != REQUIRED_ROLES:
            raise ResearchAdapterError("TradingAgents 必须恰好包含 7 个固定分析角色")
        known = _evidence_ids(bundle)
        for claim in _all_claims(result):
            referenced = {
                str(item)
                for key in ("evidence_ids", "counter_evidence_ids")
                for item in claim.get(key, [])
            }
            unknown = referenced - known
            if unknown:
                raise ResearchAdapterError(
                    f"claim {claim.get('claim_id')} 引用了未知证据: {sorted(unknown)}"
                )
            if claim.get("claim_type") == "FACT" and not claim.get("evidence_ids"):
                raise ResearchAdapterError("FACT claim 不能没有证据")
        return result


class UZIAdapter:
    """Enforce Top 3–5/manual-only deep diligence and evidence delta accounting."""

    version = "uzi-manual-bridge-v1"

    def __init__(self, settings: Settings) -> None:
        self._bundle_validator = _validator(settings, "evidence-bundle.schema.json")
        self._result_validator = _validator(settings, "uzi-due-diligence-result.schema.json")

    def import_result(self, bundle_path: Path, result_path: Path) -> dict[str, Any]:
        bundle = _load(bundle_path)
        result = _load(result_path)
        for name, validator, payload in (
            ("evidence bundle", self._bundle_validator, bundle),
            ("UZI result", self._result_validator, result),
        ):
            errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
            if errors:
                raise ResearchAdapterError(f"{name} Schema 失败: {errors[0].message}")
        if result["bundle_id"] != bundle["bundle_id"] or result["symbol"] != bundle["symbol"]:
            raise ResearchAdapterError("UZI 结果与证据包标的/版本不一致")
        if result.get("manual_trigger") is not True or not 1 <= int(result["candidate_rank"]) <= 5:
            raise ResearchAdapterError("UZI 仅允许人工触发 Top 3–5 候选")
        evidence_ids = _evidence_ids(bundle)
        classified = {
            str(item)
            for key in (
                "new_evidence_ids",
                "duplicate_evidence_ids",
                "conflicting_evidence_ids",
            )
            for item in result.get(key, [])
        }
        referenced = {
            str(item)
            for finding in result.get("findings", [])
            for item in finding.get("evidence_ids", [])
        }
        if not referenced <= evidence_ids | classified:
            raise ResearchAdapterError("UZI finding 包含未进入证据差量表的引用")
        return result


def research_adapter_health() -> list[AdapterHealth]:
    return [
        AdapterHealth(
            adapter_id="a-share-skill",
            role="MARKET_TECHNICAL_PAPER_REFERENCE",
            installed=False,
            enabled=True,
            mode="MANUAL_BRIDGE",
            notes=["工具层契约已冻结；V1 核心账本仍由本项目确定性代码负责"],
        ),
        AdapterHealth(
            adapter_id="TradingAgents-astock",
            role="PRIMARY_RESEARCH_FRAMEWORK",
            installed=False,
            enabled=True,
            mode="MANUAL_BRIDGE",
            notes=["七角色结果只通过结构化文件导入；Web/FastAPI 不调用模型 API"],
        ),
        AdapterHealth(
            adapter_id="UZI-Skill",
            role="TOP_3_TO_5_DUE_DILIGENCE_ONLY",
            installed=False,
            enabled=True,
            mode="MANUAL_BRIDGE",
            notes=["candidate_rank 必须 1–5 且 manual_trigger=true"],
        ),
        AdapterHealth(
            adapter_id="Tushare",
            role="FUTURE_DATA_ENHANCEMENT",
            installed=False,
            enabled=False,
            mode="DISABLED",
            notes=["V1 不安装、不购买；仅保留未来评估边界"],
        ),
    ]
