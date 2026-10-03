import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from app.cli.research_import import ResearchImportError, import_research_card
from app.core.settings import PROJECT_ROOT, Settings


def test_research_import_is_append_only_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "card.json"
    source.write_text(
        (PROJECT_ROOT / "templates/research-card.example.json").read_text("utf-8"), "utf-8"
    )
    settings = Settings(
        state_dir=tmp_path / "state",
        research_dir=tmp_path / "data" / "research",
    )
    first = import_research_card(settings, source)
    second = import_research_card(settings, source)
    assert first == second
    assert len(list((settings.research_dir / "300476").glob("*.json"))) == 1
    audit_lines = (
        (settings.state_dir / "research-import-audit.jsonl").read_text("utf-8").splitlines()
    )
    assert len(audit_lines) == 1
    audit_schema = json.loads(
        (PROJECT_ROOT / "schemas" / "audit-event.schema.json").read_text("utf-8")
    )
    Draft202012Validator(audit_schema, format_checker=FormatChecker()).validate(
        json.loads(audit_lines[0])
    )


def test_research_import_rejects_future_time(tmp_path: Path) -> None:
    payload = json.loads((PROJECT_ROOT / "templates/research-card.example.json").read_text("utf-8"))
    payload["as_of"] = "2099-01-01T15:00:00+08:00"
    source = tmp_path / "future.json"
    source.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    settings = Settings(
        state_dir=tmp_path / "state",
        research_dir=tmp_path / "data" / "research",
    )
    with pytest.raises(ResearchImportError, match="时间穿越"):
        import_research_card(settings, source)
