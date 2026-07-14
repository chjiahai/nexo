"""Nexo CLI entry point.

Usage:
    nexo            # health check
    nexo bot        # connect the WeCom AI bot and serve
    nexo health     # check process liveness via the heartbeat file
"""

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] == "hello":
        print("Hello from nexo!")
        return 0

    if argv[0] == "bot":
        return _bot()

    if argv[0] == "health":
        return _health()

    print(f"unknown command: {argv[0]!r}", file=sys.stderr)
    return 2


def _bot() -> int:
    import asyncio

    from nexo.api.wecom import run as run_wecom
    from nexo.observability import configure

    # Set up stdlib logging before anything else runs, so every subsequent log
    # line is captured.
    configure()

    try:
        asyncio.run(run_wecom())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 — print a clean error, not a traceback
        print(f"nexo bot failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _health() -> int:
    """Liveness probe: 0 if the heartbeat is fresh, 1 otherwise.

    Used as the Docker healthcheck. Unlike `nexo hello` (which only proves the
    package imports), this reflects whether the WebSocket is actually connected.
    """
    from nexo.observability import check_heartbeat

    if check_heartbeat():
        print("healthy")
        return 0
    print("unhealthy: heartbeat stale or missing", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
