"""Smoke tests for the oc CLI."""

from __future__ import annotations

from click.testing import CliRunner

from app.cli.main import cli


def test_oc_help_lists_audit() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "audit" in result.output


def test_oc_audit_help_lists_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["audit", "--help"])
    assert result.exit_code == 0
    for sub in ("tail", "replay", "verify"):
        assert sub in result.output


def test_oc_version() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_oc_audit_verify_bad_date() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["audit", "verify", "--date", "not-a-date"])
    assert result.exit_code == 2
    assert "Invalid date" in result.output
