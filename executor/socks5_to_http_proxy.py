"""
Local HTTP proxy bridge for authenticated SOCKS5 proxies.
Used as a workaround for Playwright proxy limitations.
"""

import logging
import socket
import threading
import time
from typing import Iterable
from urllib.parse import urlparse

import socks

logger = logging.getLogger("executor.socks5_to_http_proxy")


class SOCKS5ToHTTPProxy:
    """Local HTTP proxy server that tunnels traffic through SOCKS5."""

    def __init__(self, socks5_proxy_str: str):
        parsed = urlparse(socks5_proxy_str if "://" in socks5_proxy_str else f"socks5://{socks5_proxy_str}")
        self.socks5_host = parsed.hostname
        self.socks5_port = parsed.port or 1080
        self.socks5_user = parsed.username
        self.socks5_pass = parsed.password
        self.local_port: int | None = None
        self.server_thread: threading.Thread | None = None
        self.server_socket: socket.socket | None = None
        self.last_used_at = time.monotonic()

    def touch(self) -> None:
        self.last_used_at = time.monotonic()

    def start(self) -> int:
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(("127.0.0.1", 0))
        server_socket.listen(5)
        server_socket.settimeout(1.0)
        self.server_socket = server_socket
        self.local_port = server_socket.getsockname()[1]
        self.server_thread = threading.Thread(target=self._run_server, daemon=True)
        self.server_thread.start()
        self.touch()
        logger.debug(
            "Started local HTTP proxy on port %s for SOCKS5 %s:%s",
            self.local_port,
            self.socks5_host,
            self.socks5_port,
        )
        return int(self.local_port)

    def _run_server(self) -> None:
        server_socket = self.server_socket
        if server_socket is None:
            logger.error("Local HTTP proxy server socket is not initialized")
            return

        while self.server_socket is server_socket:
            try:
                client_socket, _addr = server_socket.accept()
                thread = threading.Thread(
                    target=self._handle_client,
                    args=(client_socket,),
                    daemon=True,
                )
                thread.start()
            except socket.timeout:
                continue
            except Exception as exc:
                if self.server_socket is server_socket:
                    logger.error("Error accepting connection: %s", exc)
                break

    def _handle_client(self, client_socket: socket.socket) -> None:
        try:
            data = client_socket.recv(4096)
            if not data:
                return

            request_line = data.split(b"\n", 1)[0].decode("utf-8", errors="ignore")
            method, path, _ = request_line.split(" ", 2)

            if method == "CONNECT":
                self._handle_connect(client_socket, path)
            else:
                self._handle_http(client_socket, data, method, path)
        except Exception as exc:
            logger.error("Error handling client: %s", exc)
        finally:
            try:
                client_socket.close()
            except Exception:
                pass

    def _create_socks_socket(self, timeout_seconds: int) -> socks.socksocket:
        sock = socks.socksocket()
        sock.set_proxy(
            socks.SOCKS5,
            self.socks5_host,
            self.socks5_port,
            username=self.socks5_user,
            password=self.socks5_pass,
        )
        sock.settimeout(timeout_seconds)
        return sock

    def _handle_connect(self, client_socket: socket.socket, path: str) -> None:
        upstream = None
        try:
            host, port_str = path.split(":", 1)
            port = int(port_str)
            logger.debug("CONNECT request to %s:%s", host, port)

            upstream = self._create_socks_socket(timeout_seconds=30)
            try:
                upstream.connect((host, port))
            except Exception as connect_error:
                logger.error("Failed to connect to %s:%s via SOCKS5: %s", host, port, connect_error)
                try:
                    client_socket.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                except Exception:
                    pass
                return

            try:
                client_socket.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            except Exception as send_error:
                logger.error("Failed to send 200 OK to client: %s", send_error)
                return

            self._relay_data(client_socket, upstream)
        except Exception as exc:
            logger.error("Error in CONNECT to %s: %s", path, exc, exc_info=True)
            try:
                client_socket.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except Exception:
                pass
        finally:
            for sock in (upstream, client_socket):
                try:
                    if sock:
                        sock.close()
                except Exception:
                    pass

    def _handle_http(self, client_socket: socket.socket, request_data: bytes, method: str, path: str) -> None:
        upstream = None
        is_non_critical = False
        host = ""
        try:
            headers: dict[str, str] = {}
            lines = request_data.decode("utf-8", errors="ignore").split("\r\n")
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip()] = value.strip()

            host = headers.get("Host", "")
            if not host:
                client_socket.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return

            if ":" in host:
                host, port_str = host.split(":", 1)
                port = int(port_str)
            else:
                port = 80

            host_lower = host.lower()
            path_lower = path.lower()
            is_connectivity_check = "connectivitycheck.gstatic.com" in host_lower or "generate_204" in path_lower
            is_google_time_sync = "google.com" in host_lower and "time" in (host_lower + path_lower)
            is_non_critical = is_connectivity_check or is_google_time_sync

            if is_connectivity_check:
                timeout_seconds = 15
            elif is_google_time_sync:
                timeout_seconds = 120
            else:
                timeout_seconds = 60

            upstream = self._create_socks_socket(timeout_seconds=timeout_seconds)
            upstream.connect((host, port))
            upstream.sendall(request_data)

            response = upstream.recv(8192)
            while response:
                client_socket.sendall(response)
                response = upstream.recv(8192)
        except Exception as exc:
            if not is_non_critical:
                logger.error("Error in HTTP %s to %s: %s", method, path, exc)
            else:
                logger.debug("Background connectivity/time request timed out (non-critical): %s%s", host, path)
            try:
                client_socket.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except Exception:
                pass
        finally:
            try:
                if upstream:
                    upstream.close()
            except Exception:
                pass

    def _relay_data(self, client: socket.socket, server: socket.socket) -> None:
        client.settimeout(120)
        server.settimeout(120)

        stop_event = threading.Event()
        last_activity = {"ts": time.monotonic()}
        idle_timeout_seconds = 45

        def forward(source: socket.socket, destination: socket.socket, direction: str) -> None:
            try:
                while not stop_event.is_set():
                    try:
                        source.settimeout(1.0)
                        data = source.recv(4096)
                        if not data:
                            break
                        last_activity["ts"] = time.monotonic()
                        destination.sendall(data)
                    except socket.timeout:
                        continue
                    except Exception as exc:
                        logger.debug("Forward %s error: %s", direction, exc)
                        break
            except Exception as exc:
                logger.debug("Forward %s thread error: %s", direction, exc)
            finally:
                stop_event.set()

        t1 = threading.Thread(target=forward, args=(client, server, "client->server"), daemon=True)
        t2 = threading.Thread(target=forward, args=(server, client, "server->client"), daemon=True)
        t1.start()
        t2.start()

        while not stop_event.wait(timeout=1):
            if time.monotonic() - last_activity["ts"] > idle_timeout_seconds:
                logger.debug("Closing idle SOCKS5 relay after %.1fs without traffic", idle_timeout_seconds)
                stop_event.set()
                break

        stop_event.set()
        for sock in (client, server):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

        t1.join(timeout=2)
        t2.join(timeout=2)

    def stop(self) -> None:
        server_socket = self.server_socket
        self.server_socket = None
        if server_socket:
            try:
                server_socket.close()
            except Exception:
                pass

        if self.server_thread and self.server_thread.is_alive() and threading.current_thread() is not self.server_thread:
            self.server_thread.join(timeout=1)


_active_proxies: dict[str, SOCKS5ToHTTPProxy] = {}
_proxy_ref_counts: dict[str, int] = {}
_proxy_lock = threading.Lock()


def _normalize_socks5_proxy(socks5_proxy_str: str) -> str:
    normalized_proxy = socks5_proxy_str.strip()
    if "://" not in normalized_proxy:
        normalized_proxy = f"socks5://{normalized_proxy}"
    return normalized_proxy


def get_local_http_proxy_for_socks5(socks5_proxy_str: str) -> int | None:
    if not socks5_proxy_str or not socks5_proxy_str.strip():
        return None

    try:
        normalized_proxy = _normalize_socks5_proxy(socks5_proxy_str)
        with _proxy_lock:
            if normalized_proxy not in _active_proxies:
                proxy = SOCKS5ToHTTPProxy(normalized_proxy)
                port = proxy.start()
                if not port:
                    logger.error("Failed to start local HTTP proxy for SOCKS5: %s", normalized_proxy)
                    return None
                _active_proxies[normalized_proxy] = proxy
                _proxy_ref_counts[normalized_proxy] = 0
                logger.info("Created local HTTP proxy on port %s for SOCKS5: %s", port, normalized_proxy)

            proxy = _active_proxies[normalized_proxy]
            proxy.touch()
            _proxy_ref_counts[normalized_proxy] = _proxy_ref_counts.get(normalized_proxy, 0) + 1
            return proxy.local_port
    except Exception as exc:
        logger.error("Error creating/getting local HTTP proxy for SOCKS5 %s: %s", socks5_proxy_str, exc)
        return None


def release_local_http_proxy_for_socks5(socks5_proxy_str: str) -> None:
    if not socks5_proxy_str or not socks5_proxy_str.strip():
        return

    normalized_proxy = _normalize_socks5_proxy(socks5_proxy_str)
    proxy_to_stop: SOCKS5ToHTTPProxy | None = None

    with _proxy_lock:
        if normalized_proxy not in _active_proxies:
            return

        current_refs = max(0, _proxy_ref_counts.get(normalized_proxy, 0) - 1)
        if current_refs == 0:
            proxy_to_stop = _active_proxies.pop(normalized_proxy, None)
            _proxy_ref_counts.pop(normalized_proxy, None)
        else:
            _proxy_ref_counts[normalized_proxy] = current_refs
            _active_proxies[normalized_proxy].touch()

    if proxy_to_stop:
        proxy_to_stop.stop()
        logger.info("Stopped local HTTP proxy for SOCKS5: %s", normalized_proxy)


def cleanup_idle_local_http_proxies(active_proxy_strings: Iterable[str] | None = None, idle_seconds: int = 300) -> int:
    active_proxy_keys = {
        _normalize_socks5_proxy(proxy_str)
        for proxy_str in (active_proxy_strings or [])
        if proxy_str and proxy_str.strip()
    }
    now = time.monotonic()
    to_stop: list[tuple[str, SOCKS5ToHTTPProxy]] = []

    with _proxy_lock:
        for proxy_key, proxy in list(_active_proxies.items()):
            refs = _proxy_ref_counts.get(proxy_key, 0)
            idle_for = now - proxy.last_used_at
            if refs > 0 or proxy_key in active_proxy_keys or idle_for < idle_seconds:
                continue
            to_stop.append((proxy_key, _active_proxies.pop(proxy_key)))
            _proxy_ref_counts.pop(proxy_key, None)

    for proxy_key, proxy in to_stop:
        proxy.stop()
        logger.info("Cleaned up idle local HTTP proxy for SOCKS5: %s", proxy_key)

    return len(to_stop)


def get_local_http_proxy_stats() -> dict[str, int]:
    with _proxy_lock:
        return {
            "active_proxies": len(_active_proxies),
            "leased_proxies": sum(1 for refs in _proxy_ref_counts.values() if refs > 0),
        }
