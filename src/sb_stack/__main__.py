"""Enable `python -m sb_stack` as an alias for the `sb-stack` CLI."""

from sb_stack.cli.main import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
