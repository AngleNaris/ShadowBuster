"""单实例守卫：已有实例在跑时，_another_instance_running 必须能探测到。"""
import uuid

from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalServer

import main


app = QCoreApplication.instance() or QCoreApplication([])


def unique_key():
    # macOS Qt 拒绝带斜杠的名字（Unix 下实际套接字位于 /tmp/<name>）
    return f"sb-test-{uuid.uuid4().hex}"


def test_no_server_means_single(monkeypatch):
    key = unique_key()
    QLocalServer.removeServer(key)
    monkeypatch.setattr(main, "_SINGLE_INSTANCE_KEY", key)
    assert main._another_instance_running() is False


def test_existing_server_detected(monkeypatch):
    key = unique_key()
    QLocalServer.removeServer(key)
    monkeypatch.setattr(main, "_SINGLE_INSTANCE_KEY", key)
    server = QLocalServer()
    assert server.listen(key), server.errorString()
    try:
        assert main._another_instance_running() is True
        # raise 消息已送达；服务端接受连接、收到数据各需要事件循环转一圈
        QCoreApplication.processEvents()
        assert server.hasPendingConnections()
        conn = server.nextPendingConnection()
        conn.waitForReadyRead(500)
        assert conn.readAll() == b"raise\n"
    finally:
        server.close()
        QLocalServer.removeServer(key)


def test_cleanup_before_listen_is_always_safe(monkeypatch):
    """main() 在 listen 前总是先 removeServer：无论是否存在残留条目，
    清理后 listen 必须成功（本 Qt 版本的 listen 自身也会清陈旧条目，
    这里的 removeServer 是双保险）。"""
    key = unique_key()
    QLocalServer.removeServer(key)
    with open(f"/tmp/{key}", "wb") as f:   # 造一个残留条目（可能被 Qt 自动清理）
        f.write(b"junk")
    monkeypatch.setattr(main, "_SINGLE_INSTANCE_KEY", key)
    QLocalServer.removeServer(key)          # main() 的清理动作
    server = QLocalServer()
    assert server.listen(key)
    server.close()
    QLocalServer.removeServer(key)
