from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from app.core.settings import PROJECT_ROOT

LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"))


def validate_project(root: Path = PROJECT_ROOT) -> list[str]:
    results: list[str] = []
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "schemas").glob("*.schema.json")):
        payload = _load_json(path)
        if not isinstance(payload, dict):
            raise RuntimeError(f"Schema 根节点必须为对象: {path}")
        Draft202012Validator.check_schema(payload)
        schemas[path.name] = payload
    results.append(f"JSON Schema 元校验: {len(schemas)} 个通过")

    yaml_paths = [
        *sorted((root / "config").glob("*.yaml")),
        *sorted((root / "rules").glob("*.yaml")),
        *sorted((root / "evals").glob("*.yaml")),
    ]
    yaml_payloads: dict[str, dict[str, Any]] = {}
    for path in yaml_paths:
        payload = yaml.safe_load(path.read_text("utf-8"))
        if not isinstance(payload, dict) or not payload:
            raise RuntimeError(f"YAML 根节点为空或类型错误: {path}")
        yaml_payloads[str(path.relative_to(root))] = payload
    weights = yaml_payloads["config/screening-v1.yaml"].get("weights")
    weight_total = (
        sum(float(value) for value in weights.values()) if isinstance(weights, dict) else 0
    )
    if not isinstance(weights, dict) or abs(weight_total - 1) > 1e-9:
        raise RuntimeError("screening-v1 权重之和必须为 1")
    upstream = yaml_payloads["config/upstream-manifest.yaml"]
    if upstream.get("runtime_policy") != "V1_MANUAL_BRIDGE_NO_LLM_API":
        raise RuntimeError("上游运行策略偏离 V1 禁止 LLM API 的边界")
    golden_cases = yaml_payloads["evals/golden-cases.yaml"].get("cases")
    if not isinstance(golden_cases, list) or len(golden_cases) != 30:
        raise RuntimeError("黄金样本清单必须恰好包含 30 个案例")
    golden_ids = [str(item.get("id")) for item in golden_cases if isinstance(item, dict)]
    if len(golden_ids) != 30 or len(set(golden_ids)) != 30:
        raise RuntimeError("黄金样本 ID 必须完整且唯一")
    results.append(f"策略、规则、评测 YAML: {len(yaml_paths)} 个通过")

    research_schema = schemas["research-card.schema.json"]
    research_template = _load_json(root / "templates" / "research-card.example.json")
    errors = sorted(
        Draft202012Validator(
            research_schema,
            format_checker=FormatChecker(),
        ).iter_errors(research_template),
        key=lambda error: list(error.path),
    )
    if errors:
        raise RuntimeError(f"研究卡模板不符合 Schema: {errors[0].message}")
    results.append("研究卡示例契约: 通过")

    backtest_v1 = yaml_payloads["config/dividend-hurdle-backtest-v1.yaml"]
    errors = sorted(
        Draft202012Validator(
            schemas["dividend-hurdle-backtest-policy.schema.json"],
            format_checker=FormatChecker(),
        ).iter_errors(backtest_v1),
        key=lambda error: list(error.path),
    )
    if errors:
        raise RuntimeError(f"回测 v1 策略不符合 Schema: {errors[0].message}")
    backtest_v2 = yaml_payloads["config/dividend-hurdle-backtest-v2.yaml"]
    errors = sorted(
        Draft202012Validator(
            schemas["dividend-hurdle-backtest-run-policy.schema.json"],
            format_checker=FormatChecker(),
        ).iter_errors(backtest_v2),
        key=lambda error: list(error.path),
    )
    if errors:
        raise RuntimeError(f"回测 v2 策略不符合 Schema: {errors[0].message}")
    backtest_v3 = yaml_payloads["config/dividend-hurdle-backtest-v3.yaml"]
    errors = sorted(
        Draft202012Validator(
            schemas["dividend-hurdle-backtest-run-policy.schema.json"],
            format_checker=FormatChecker(),
        ).iter_errors(backtest_v3),
        key=lambda error: list(error.path),
    )
    if errors:
        raise RuntimeError(f"回测 v3 策略不符合 Schema: {errors[0].message}")
    backtest_v4 = yaml_payloads["config/dividend-hurdle-backtest-v4.yaml"]
    errors = sorted(
        Draft202012Validator(
            schemas["dividend-hurdle-backtest-run-policy.schema.json"],
            format_checker=FormatChecker(),
        ).iter_errors(backtest_v4),
        key=lambda error: list(error.path),
    )
    if errors:
        raise RuntimeError(f"回测 v4 策略不符合 Schema: {errors[0].message}")
    deterministic_runtime = yaml_payloads["config/deterministic-runtime-v1.yaml"]
    errors = sorted(
        Draft202012Validator(
            schemas["deterministic-runtime-config.schema.json"],
            format_checker=FormatChecker(),
        ).iter_errors(deterministic_runtime),
        key=lambda error: list(error.path),
    )
    if errors:
        raise RuntimeError(f"确定性运行时配置不符合 Schema: {errors[0].message}")
    ledger_template = _load_json(root / "templates" / "backtest-ledger-snapshot.example.json")
    errors = sorted(
        Draft202012Validator(
            schemas["backtest-ledger-snapshot.schema.json"],
            format_checker=FormatChecker(),
        ).iter_errors(ledger_template),
        key=lambda error: list(error.path),
    )
    if errors:
        raise RuntimeError(f"回测账本示例不符合 Schema: {errors[0].message}")
    results.append("回测 v1/v2/v3/v4 策略、确定性运行时与账本示例契约: 通过")

    artifact_checks = (
        (root / "data" / "research", "*/*.json", "research-card.schema.json"),
        (root / "data" / "screening", "*/*.json", "screening-run.schema.json"),
        (
            root / "data" / "screening-invalidations",
            "*/*.json",
            "screening-invalidation.schema.json",
        ),
        (root / "data" / "evidence", "*/*.json", "evidence-bundle.schema.json"),
        (root / "data" / "corporate-actions", "*/*.json", "corporate-action.schema.json"),
        (root / "data" / "audit", "*.json", "audit-event.schema.json"),
        (
            root / "data" / "backtests" / "manifests",
            "*.json",
            "backtest-dataset-manifest.schema.json",
        ),
        (root / "data" / "runtime-runs", "*/*.json", "deterministic-task-run.schema.json"),
        (root / "data" / "exception-packages", "*.json", "exception-evidence-package.schema.json"),
    )
    artifact_count = 0
    for directory, pattern, schema_name in artifact_checks:
        validator = Draft202012Validator(schemas[schema_name], format_checker=FormatChecker())
        for path in sorted(directory.glob(pattern)):
            payload = _load_json(path)
            errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
            if errors:
                where = ".".join(str(value) for value in errors[0].path) or "root"
                raise RuntimeError(
                    f"产物不符合 {schema_name}: {path.relative_to(root)} {where}: "
                    f"{errors[0].message}"
                )
            artifact_count += 1
    results.append(f"版本化数据产物 Schema: {artifact_count} 个通过")

    broken: list[str] = []
    markdown_files = [root / "README.md", root / "PROJECT_INSTRUCTIONS.md", root / "AGENTS.md"]
    markdown_files.extend(sorted((root / "docs").glob("*.md")))
    for document in markdown_files:
        for target in LINK_PATTERN.findall(document.read_text("utf-8")):
            clean = target.strip().split("#", 1)[0]
            if not clean or clean.startswith(("http://", "https://", "mailto:")):
                continue
            if not (document.parent / clean).resolve().exists():
                broken.append(f"{document.relative_to(root)} -> {target}")
    if broken:
        raise RuntimeError("发现无效本地文档链接:\n" + "\n".join(broken))
    results.append(f"Markdown 本地链接: {len(markdown_files)} 份文档通过")
    return results


def main() -> None:
    for line in validate_project():
        print(f"✓ {line}")


if __name__ == "__main__":
    main()
