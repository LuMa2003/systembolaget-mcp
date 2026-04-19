"""Smoke tests for the `sb-stack` CLI surface.

Verifies that `sb-stack --help` renders every subcommand promised in
docs/06_module_layout.md §CLI entrypoint. This is the Step 1 acceptance test.
"""

from __future__ import annotations

from typer.testing import CliRunner

from sb_stack.cli.main import app

runner = CliRunner()


def test_help_lists_all_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    output = result.output

    expected = [
        "migrate",
        "sync",
        "sync-scheduler",
        "runs",
        "run-info",
        "mcp",
        "embed-server",
        "bootstrap",
        "extract-key",
        "doctor",
        "shell",
    ]
    for cmd in expected:
        assert cmd in output, f"{cmd!r} missing from --help output"


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "sb-stack" in result.output


def test_subcommand_stub_exits_2() -> None:
    # Stubbed commands exit with code 2 and say "not implemented yet".
    # `bootstrap` is still a stub (Step 1); `migrate` was wired up in Step 2.
    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code == 2
    assert "not implemented" in (result.output + (result.stderr or ""))
