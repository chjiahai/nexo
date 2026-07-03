"""Nexo CLI entry point.

Usage:
    nexo            # health check
    nexo bot        # connect the WeCom AI bot and serve
"""

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] == "hello":
        print("Hello from nexo!")
        return 0

    if argv[0] == "bot":
        return _bot()

    print(f"unknown command: {argv[0]!r}", file=sys.stderr)
    return 2


def _bot() -> int:
    import asyncio

    from nexo.api.wecom import run as run_wecom

    try:
        asyncio.run(run_wecom())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 — print a clean error, not a traceback
        print(f"nexo bot failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
