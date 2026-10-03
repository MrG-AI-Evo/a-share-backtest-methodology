from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from time import monotonic
from typing import Any, TypedDict
from uuid import uuid4

import httpx

from app.core.settings import get_settings
from app.core.time import iso_now


class Probe(TypedDict):
    at: str
    endpoints: dict[str, int | str]
    semantic_failures: list[str]


ENDPOINTS = (
    "/health",
    "/api/v1/system",
    "/api/v1/pipeline",
    "/api/v1/dashboard",
    "/api/v1/paper",
    "/api/v1/backtests/readiness",
    "/api/v1/backtests",
)


def _mapping(value: object, label: str) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(value, dict):
        return None, [f"{label} 必须是 JSON 对象"]
    return value, []


def _envelope_data(payload: object, endpoint: str) -> tuple[dict[str, Any] | None, list[str]]:
    root, failures = _mapping(payload, endpoint)
    if root is None:
        return None, failures
    data, data_failures = _mapping(root.get("data"), f"{endpoint}.data")
    return data, [*failures, *data_failures]


def _semantic_failures(endpoint: str, payload: object) -> list[str]:
    if endpoint == "/health":
        root, failures = _mapping(payload, endpoint)
        if root is None:
            return failures
        expected = {
            "status": "ok",
            "real_trading_enabled": False,
            "llm_api_enabled": False,
        }
        return [
            *failures,
            *[
                f"{endpoint}.{field} 期望 {value!r}，实际 {root.get(field)!r}"
                for field, value in expected.items()
                if root.get(field) is not value and root.get(field) != value
            ],
        ]

    if endpoint == "/api/v1/backtests":
        root, failures = _mapping(payload, endpoint)
        runs = root.get("data") if root is not None else None
        if not isinstance(runs, list):
            failures.append(f"{endpoint}.data 必须为数组")
            return failures
        for index, run in enumerate(runs):
            if not isinstance(run, dict):
                failures.append(f"{endpoint}.data[{index}] 必须为对象")
                continue
            status = run.get("status")
            performance_available = run.get("performance_available")
            if status == "BLOCKED" and performance_available is not False:
                failures.append(f"{endpoint}.data[{index}] 阻断运行不得提供绩效")
            if status == "COMPLETE" and performance_available is not True:
                failures.append(f"{endpoint}.data[{index}] 完成运行必须提供绩效")
        return failures

    data, failures = _envelope_data(payload, endpoint)
    if data is None:
        return failures
    if endpoint == "/api/v1/system":
        expected = {
            "live_broker_enabled": False,
            "automatic_trading_enabled": False,
            "llm_api_enabled": False,
            "ledger_reconciled": True,
        }
        failures.extend(
            f"{endpoint}.data.{field} 期望 {value!r}，实际 {data.get(field)!r}"
            for field, value in expected.items()
            if data.get(field) is not value
        )
        if data.get("ledger_reconciliation_errors") != []:
            failures.append(f"{endpoint}.data.ledger_reconciliation_errors 必须为空")
        if not data.get("latest_screening_run_id"):
            failures.append(f"{endpoint}.data.latest_screening_run_id 不能为空")
    elif endpoint == "/api/v1/pipeline":
        if data.get("llm_api_enabled") is not False:
            failures.append(f"{endpoint}.data.llm_api_enabled 必须为 false")
        if data.get("uzi_daily_full_market_enabled") is not False:
            failures.append(f"{endpoint}.data.uzi_daily_full_market_enabled 必须为 false")
        screening = data.get("latest_screening")
        if not isinstance(screening, dict):
            failures.append(f"{endpoint}.data.latest_screening 必须存在")
        else:
            hard_filter = screening.get("hard_filter")
            factor_filter = screening.get("factor_filter")
            if not isinstance(hard_filter, list) or not 1 <= len(hard_filter) <= 300:
                failures.append(f"{endpoint}.data.latest_screening.hard_filter 数量必须为 1–300")
            if not isinstance(factor_filter, list) or not 1 <= len(factor_filter) <= 30:
                failures.append(f"{endpoint}.data.latest_screening.factor_filter 数量必须为 1–30")
    elif endpoint == "/api/v1/backtests/readiness":
        expected = {
            "base_policy_sha256_verified": True,
            "engineering_ready": True,
        }
        failures.extend(
            f"{endpoint}.data.{field} 期望 {value!r}，实际 {data.get(field)!r}"
            for field, value in expected.items()
            if data.get(field) is not value
        )
        primary = data.get("primary_window")
        if not isinstance(primary, dict):
            failures.append(f"{endpoint}.data.primary_window 必须存在")
        else:
            if primary.get("start_date") != "2016-01-04":
                failures.append(f"{endpoint}.data.primary_window.start_date 必须为 2016-01-04")
            if primary.get("end_date") != "2025-12-31":
                failures.append(f"{endpoint}.data.primary_window.end_date 必须为 2025-12-31")
            if primary.get("included_in_primary_performance") is not True:
                failures.append(
                    f"{endpoint}.data.primary_window.included_in_primary_performance 必须为 true"
                )
        extension = data.get("extension_window")
        if not isinstance(extension, dict):
            failures.append(f"{endpoint}.data.extension_window 必须存在")
        else:
            if extension.get("start_date") != "2026-01-01":
                failures.append(f"{endpoint}.data.extension_window.start_date 必须为 2026-01-01")
            if extension.get("included_in_primary_performance") is not False:
                failures.append(
                    f"{endpoint}.data.extension_window.included_in_primary_performance 必须为 false"
                )
        safety = data.get("safety")
        if not isinstance(safety, dict):
            failures.append(f"{endpoint}.data.safety 必须存在")
        else:
            expected_safety = {
                "real_broker_connected": False,
                "real_orders_enabled": False,
                "llm_api_enabled": False,
                "synthetic_raw_data_allowed_in_formal_run": False,
                "primary_and_extension_results_separated": True,
            }
            failures.extend(
                f"{endpoint}.data.safety.{field} 期望 {value!r}，实际 {safety.get(field)!r}"
                for field, value in expected_safety.items()
                if safety.get(field) is not value
            )
        blockers = data.get("blockers")
        executable = data.get("formal_backtest_executable")
        if not isinstance(blockers, list):
            failures.append(f"{endpoint}.data.blockers 必须为数组")
        elif executable is False and not blockers:
            failures.append(f"{endpoint}.data 正式回测不可执行时必须给出 blocker")
        elif executable is True and blockers:
            failures.append(f"{endpoint}.data 正式回测可执行时 blocker 必须为空")
    return failures


def _prepare_evidence(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError("同一 run_id 的 soak 证据已存在，禁止覆盖")


def _append_probe(path: Path, probe: Probe) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(probe, ensure_ascii=False, sort_keys=True))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


async def run_soak(
    base_url: str,
    duration_seconds: float,
    interval_seconds: float,
    evidence_path: Path | None = None,
    web_base_url: str | None = None,
    web_paths: tuple[str, ...] = (),
) -> dict[str, object]:
    started = monotonic()
    started_at = iso_now()
    finished_at = started_at
    probe_count = 0
    status_failures = 0
    semantic_failure_count = 0
    if evidence_path is not None:
        await asyncio.to_thread(_prepare_evidence, evidence_path)
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            probe: Probe = {"at": iso_now(), "endpoints": {}, "semantic_failures": []}
            for endpoint in ENDPOINTS:
                try:
                    response = await client.get(f"{base_url.rstrip('/')}{endpoint}")
                    probe["endpoints"][endpoint] = response.status_code
                    if response.status_code == 200:
                        try:
                            payload = response.json()
                        except ValueError as exc:
                            probe["semantic_failures"].append(
                                f"{endpoint} 返回的不是有效 JSON: {exc}"
                            )
                        else:
                            probe["semantic_failures"].extend(_semantic_failures(endpoint, payload))
                except httpx.HTTPError as exc:
                    probe["endpoints"][endpoint] = f"ERROR:{exc}"
            if web_base_url is not None:
                for path in web_paths:
                    endpoint_key = f"WEB:{path}"
                    try:
                        response = await client.get(f"{web_base_url.rstrip('/')}{path}")
                        probe["endpoints"][endpoint_key] = response.status_code
                    except httpx.HTTPError as exc:
                        probe["endpoints"][endpoint_key] = f"ERROR:{exc}"
            probe_failures = sum(status != 200 for status in probe["endpoints"].values())
            status_failures += probe_failures
            semantic_failure_count += len(probe["semantic_failures"])
            probe_count += 1
            finished_at = probe["at"]
            if evidence_path is not None:
                await asyncio.to_thread(_append_probe, evidence_path, probe)
            elapsed = monotonic() - started
            if elapsed >= duration_seconds:
                break
            await asyncio.sleep(min(interval_seconds, max(0, duration_seconds - elapsed)))
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "duration": str(timedelta(seconds=round(monotonic() - started))),
        "duration_seconds_target": duration_seconds,
        "interval_seconds": interval_seconds,
        "probe_count": probe_count,
        "status_failure_count": status_failures,
        "semantic_failure_count": semantic_failure_count,
        "failure_count": status_failures + semantic_failure_count,
        "passed": status_failures == 0 and semantic_failure_count == 0,
        "interrupted": False,
        "evidence_path": str(evidence_path) if evidence_path else None,
    }


def _write_summary(path: Path, payload: dict[str, object]) -> None:
    if path.exists():
        raise RuntimeError("同一 run_id 的 soak 摘要已存在，禁止覆盖")
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _interrupted_summary(
    evidence_path: Path,
    run_id: str,
    summary_path: Path,
    duration_seconds: float,
    interval_seconds: float,
) -> dict[str, object]:
    probes = [
        json.loads(line) for line in evidence_path.read_text("utf-8").splitlines() if line.strip()
    ]
    status_failures = sum(
        status != 200 for probe in probes for status in probe.get("endpoints", {}).values()
    )
    semantic_failure_count = sum(len(probe.get("semantic_failures", [])) for probe in probes)
    return {
        "run_id": run_id,
        "started_at": probes[0]["at"] if probes else None,
        "finished_at": probes[-1]["at"] if probes else None,
        "duration": "INTERRUPTED",
        "duration_seconds_target": duration_seconds,
        "interval_seconds": interval_seconds,
        "probe_count": len(probes),
        "status_failure_count": status_failures,
        "semantic_failure_count": semantic_failure_count,
        "failure_count": status_failures + semantic_failure_count,
        "passed": False,
        "interrupted": True,
        "evidence_path": str(evidence_path),
        "summary_path": str(summary_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="本地 API 稳定性探针；正式门禁为 24 小时")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--web-base-url", default="http://127.0.0.1:3000")
    parser.add_argument(
        "--backtest-run-id",
        default=None,
        help="加入综合 soak 的回测详情页 run_id；正式综合验收必须提供",
    )
    parser.add_argument("--duration-seconds", type=float, default=86_400)
    parser.add_argument("--interval-seconds", type=float, default=60)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    if args.duration_seconds <= 0 or not 0 < args.interval_seconds <= 60:
        parser.error("duration 必须为正，interval 必须在 (0, 60] 秒")
    if args.duration_seconds >= 86_400 and not args.backtest_run_id:
        parser.error("正式综合 24 小时 soak 必须提供 --backtest-run-id 以覆盖详情页")
    settings = get_settings()
    compact_now = iso_now()[:19].replace(":", "").replace("-", "")
    run_id = args.run_id or f"{compact_now}-soak-{uuid4().hex[:6]}"
    output_dir = args.output_dir or settings.state_dir / "soak"
    evidence_path = output_dir / f"{run_id}.jsonl"
    summary_path = output_dir / f"{run_id}.summary.json"
    web_paths: tuple[str, ...] = ("/", "/backtests")
    if args.backtest_run_id:
        web_paths = (*web_paths, f"/backtests/{args.backtest_run_id}")
    try:
        result = asyncio.run(
            run_soak(
                args.base_url,
                args.duration_seconds,
                args.interval_seconds,
                evidence_path,
                args.web_base_url,
                web_paths,
            )
        )
    except KeyboardInterrupt:
        result = _interrupted_summary(
            evidence_path,
            run_id,
            summary_path,
            args.duration_seconds,
            args.interval_seconds,
        )
        _write_summary(summary_path, result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        raise SystemExit(130) from None
    result["run_id"] = run_id
    result["summary_path"] = str(summary_path)
    _write_summary(summary_path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
