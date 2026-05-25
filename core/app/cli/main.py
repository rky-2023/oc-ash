"""Click entrypoint for the `oc` command.

Registered as a console script in pyproject.toml:
  [project.scripts]
  oc = "app.cli.main:cli"

After `pip install -e .`, the `oc` command is on PATH inside the venv.
"""

from __future__ import annotations

import click

from app.cli.attest_cmds import attest
from app.cli.audit_cmds import audit


@click.group()
@click.version_option(version="0.1.0", prog_name="oc")
def cli() -> None:
    """openclaw operator CLI."""


cli.add_command(audit)
cli.add_command(attest)


if __name__ == "__main__":
    cli()
