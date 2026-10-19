"""Startup must reserve the port before importing the production application."""
import errno
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

import run_server


pytestmark = pytest.mark.unit


def test_occupied_port_exits_without_loading_application():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        script = (
            "import sys, run_server; "
            "result = run_server.run_server(port=int(sys.argv[1])); "
            "assert 'main' not in sys.modules; "
            "assert 'db.database' not in sys.modules; "
            "assert 'config.settings' not in sys.modules; "
            "sys.exit(result)"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(port)],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, timeout=10,
        )
    assert result.returncode == 1
    assert f"127.0.0.1:{port}" in result.stderr
    assert "already in use" in result.stderr
    assert "Ctrl+C" in result.stderr
    assert "Traceback" not in result.stderr
    assert "application startup" not in result.stderr


@pytest.mark.parametrize("reload,workers", [(False, 1), (True, 1), (False, 2)])
def test_reserved_socket_is_passed_to_server_and_closed(monkeypatch, reload, workers):
    import config.settings
    import uvicorn
    import uvicorn.supervisors

    monkeypatch.setattr(config.settings, "get_settings", lambda: SimpleNamespace(DEBUG=reload, ENV="production"))
    monkeypatch.setenv("WEB_CONCURRENCY", str(workers))
    calls = []

    def server_run(server, sockets=None):
        sock = sockets[0]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as contender:
            with pytest.raises(OSError):
                contender.bind(sock.getsockname())
        calls.append(sock)
        server.started = True

    class Supervisor:
        def __init__(self, config, target, sockets):
            assert config.should_reload == reload
            assert config.workers == workers
            self.target, self.sockets = target, sockets

        def run(self):
            self.target(sockets=self.sockets)

    monkeypatch.setattr(uvicorn.Server, "run", server_run)
    monkeypatch.setattr(uvicorn.supervisors, "ChangeReload", Supervisor)
    monkeypatch.setattr(uvicorn.supervisors, "Multiprocess", Supervisor)
    assert run_server.run_server(port=0) == 0
    assert len(calls) == 1
    assert calls[0].fileno() == -1


def test_bind_permission_error_is_not_reported_as_existing_server(monkeypatch, capsys):
    def denied(*args):
        raise PermissionError(errno.EACCES, "Access denied")

    monkeypatch.setattr(run_server, "_reserve_server_socket", denied)
    assert run_server.run_server() == 1
    error = capsys.readouterr().err
    assert "Cannot bind API address" in error
    assert "already in use" not in error


def test_startup_failure_closes_reserved_socket(monkeypatch):
    import config.settings
    import uvicorn

    monkeypatch.setattr(config.settings, "get_settings", lambda: SimpleNamespace(DEBUG=False, ENV="production"))
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    captured = []

    def failed_start(server, sockets=None):
        captured.extend(sockets)
        raise RuntimeError("startup failed")

    monkeypatch.setattr(uvicorn.Server, "run", failed_start)
    with pytest.raises(RuntimeError, match="startup failed"):
        run_server.run_server(port=0)
    assert len(captured) == 1
    assert captured[0].fileno() == -1


def test_free_port_serves_http_through_reserved_socket(monkeypatch):
    import config.settings
    import uvicorn

    async def app(scope, receive, send):
        assert scope["type"] == "http"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"startup-ok"})

    real_config = uvicorn.Config
    real_run = uvicorn.Server.run
    outcomes = []
    clients = []

    def test_config(app_name, **kwargs):
        assert app_name == "main:app"
        return real_config(app, lifespan="off", log_config=None, **kwargs)

    def serve(server, sockets=None):
        port = sockets[0].getsockname()[1]

        def request():
            try:
                with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                    outcomes.append((response.status, response.read()))
            except Exception as exc:
                outcomes.append(exc)
            finally:
                server.should_exit = True

        client = threading.Thread(target=request, daemon=True)
        clients.append(client)
        client.start()
        try:
            return real_run(server, sockets=sockets)
        finally:
            client.join(timeout=6)

    monkeypatch.setattr(config.settings, "get_settings", lambda: SimpleNamespace(DEBUG=False, ENV="production"))
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setattr(uvicorn, "Config", test_config)
    monkeypatch.setattr(uvicorn.Server, "run", serve)
    assert run_server.run_server(port=0) == 0
    assert all(not client.is_alive() for client in clients)
    assert outcomes == [(200, b"startup-ok")]
