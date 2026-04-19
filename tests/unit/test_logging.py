"""Unit tests for sb_stack.logging.configure_logging."""

from __future__ import annotations

import json
import logging
from io import StringIO
from pathlib import Path

import pytest
import structlog

from sb_stack.logging import configure_logging, get_logger
from sb_stack.settings import Settings


@pytest.fixture
def _reset_structlog() -> None:
    structlog.reset_defaults()


def test_json_renderer_emits_structured_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        data_dir=tmp_path,
        log_format="json",
        log_level="info",
        log_to_file=False,
        log_to_stdout=True,
    )
    configure_logging(settings, process_name="test")
    log = get_logger("sb_stack.test")

    log.info("example_event", answer=42, subsystem="foo")

    out = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(out)
    assert payload["event"] == "example_event"
    assert payload["answer"] == 42
    assert payload["subsystem"] == "foo"
    assert payload["level"] == "info"
    assert "timestamp" in payload


def test_log_to_file_creates_rotating_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data"
    settings = Settings(
        data_dir=data_dir,
        log_format="json",
        log_level="info",
        log_to_file=True,
        log_to_stdout=False,
    )
    configure_logging(settings, process_name="sb-sync")

    root = logging.getLogger()
    buf = StringIO()
    # Emit via stdlib so the file handler catches it without needing actual
    # structlog-level output. File handler is attached to the root logger.
    root.info("probe")

    log_file = data_dir / "logs" / "sb-sync.log"
    assert log_file.exists()
    # Flush handlers
    for h in root.handlers:
        h.flush()
    assert "probe" in log_file.read_text(encoding="utf-8")
    buf.close()


def test_invalid_log_level_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    # Bypass the Settings validator by constructing a non-literal path:
    # configure_logging's own _level_int guards against bad values.
    settings = Settings(
        data_dir=tmp_path,
        log_level="info",
        log_to_file=False,
        log_to_stdout=False,
    )
    # Force an invalid level onto the frozen-ish settings via model_copy
    bad = settings.model_copy(update={"log_level": "screamy"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid log level"):
        configure_logging(bad, process_name="test")
