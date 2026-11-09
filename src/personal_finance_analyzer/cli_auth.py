"""`pfa auth`: helpers for the login a server beyond loopback requires (`api/auth.py`)."""

from __future__ import annotations

import sys

import typer

from personal_finance_analyzer.api.auth import MIN_PASSWORD_LENGTH, hash_password

auth_app = typer.Typer(help="Helpers for the web login (see docs/DOCKER.md).", no_args_is_help=True)


@auth_app.command(name="hash-password")
def hash_password_cmd(
    stdin: bool = typer.Option(
        False, "--stdin", help="Read the password from standard input instead of prompting."
    ),
) -> None:
    """Print a PFA_PASSWORD_HASH value for a password, so the plaintext never has to be stored.

    Put the printed line in a Docker secret (PFA_PASSWORD_HASH_FILE) or an environment file.
    """
    if stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        # The prompts go to stderr so `pfa auth hash-password > file` captures the hash alone.
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True, err=True)
    if len(password) < MIN_PASSWORD_LENGTH:
        typer.echo(f"The password must be at least {MIN_PASSWORD_LENGTH} characters.", err=True)
        raise typer.Exit(code=1)
    typer.echo(hash_password(password))
