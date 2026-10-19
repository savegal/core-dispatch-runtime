#!/usr/bin/env python
"""Start the API with Windows subprocess support and an early port reservation."""
import asyncio
import errno
import logging
import socket
import sys


def _reserve_server_socket(host: str, port: int) -> socket.socket:
    """Keep the port reserved until Uvicorn takes ownership of this same socket."""
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            # SO_REUSEADDR on Windows can let a second process hijack the port.
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((host, port))
        server_socket.listen(2048)
        server_socket.set_inheritable(True)
        return server_socket
    except BaseException:
        server_socket.close()
        raise


def run_server(host: str = "127.0.0.1", port: int = 8000) -> int:
    # Do this before importing main:app or starting its DB/scheduler lifecycle.
    try:
        server_socket = _reserve_server_socket(host, port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
            message = (
                f"API address {host}:{port} is already in use. "
                "If the API is already running, use its existing window. "
                "To restart it, press Ctrl+C in that window, then run python run_server.py again."
            )
        else:
            message = f"Cannot bind API address {host}:{port}: {exc}"
        print(f"ERROR: {message}", file=sys.stderr)
        return 1

    with server_socket:
        import uvicorn
        from config.settings import get_settings

        settings = get_settings()
        config = uvicorn.Config(
            "main:app",
            host=host,
            port=server_socket.getsockname()[1],
            reload=bool(settings.DEBUG or str(settings.ENV).lower() in ("dev", "development")),
        )
        server = uvicorn.Server(config)
        logging.getLogger("uvicorn.error").info(
            "Uvicorn running on http://%s:%d (Press CTRL+C to quit)", host, config.port,
        )
        if config.should_reload:
            from uvicorn.supervisors import ChangeReload

            ChangeReload(config, target=server.run, sockets=[server_socket]).run()
        elif config.workers > 1:
            from uvicorn.supervisors import Multiprocess

            Multiprocess(config, target=server.run, sockets=[server_socket]).run()
        else:
            server.run(sockets=[server_socket])
            if not server.started:
                return 3  # Uvicorn's startup-failure exit code.
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        print("Windows ProactorEventLoopPolicy enabled for subprocess support")
    raise SystemExit(run_server())
