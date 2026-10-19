"""A stalled download must not hold the database (48-hour soak, 2026-10-19).

FastAPI ends ``yield`` dependencies after the response body is sent; the download route had already queried,
so its transaction (SQLite: BEGIN IMMEDIATE, the write lock) stayed open while the file streamed. A client
that stopped reading stopped every write in the service for over 20 minutes. Measured before the fix: a write
during the stall failed with HTTP 500 "database is locked" after about 39 s.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx

from autoppt_api.app import create_app
from autoppt_api.config import Settings
from autoppt_api.security import sign_url_token


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_writes_go_on_while_a_client_stalls_mid_download(tmp_path):
    import uvicorn

    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}", storage_dir=tmp_path / "storage",
                        dev_echo_codes=True, login_codes_per_hour=1000)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    client = httpx.Client(base_url=base, timeout=60)
    code = client.post("/api/auth/code", json={"email": "u@example.com"}).json()["dev_code"]
    headers = {"Authorization": "Bearer " + client.post("/api/auth/login", json={"email": "u@example.com", "code": code}).json()["access_token"]}
    data = bytes(range(256)) * (40 * 1024 * 4)  # 40 MB: far more than the socket buffers hold
    init = client.post("/api/uploads", json={"filename": "big.bin", "size": len(data)}, headers=headers).json()
    for offset in range(0, len(data), init["chunk_size"]):
        client.put(init["upload_url"], params={"offset": offset}, content=data[offset:offset + init["chunk_size"]]).raise_for_status()
    sha = client.post(f"/api/uploads/{init['upload_id']}/complete", headers=headers).json()["sha256"]
    token = sign_url_token(settings.secret_key, purpose="download", ref=f"{sha}|big.bin", ttl_s=600)

    def timed_write(email: str) -> tuple[int, float]:
        started = time.monotonic()
        reply = httpx.post(f"{base}/api/auth/code", json={"email": email}, timeout=60)
        return reply.status_code, time.monotonic() - started

    _, baseline = timed_write("baseline@example.com")  # new local connections are slow on some Windows hosts
    stalled = socket.create_connection(("127.0.0.1", port))
    stalled.sendall(f"GET /api/files/{token} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    stalled.recv(65536)  # read the start of the response, then stop reading
    try:
        status, took = timed_write("other@example.com")
        assert status == 200 and took < baseline + 5, (status, took, baseline)
    finally:
        stalled.close()
        server.should_exit = True
