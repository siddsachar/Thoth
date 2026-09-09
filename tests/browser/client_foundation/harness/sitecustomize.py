"""Child-only offline and real-data guards for the Phase 2 browser fixture.

Retains the reviewed Phase 1 guards without modifying historical evidence.
This is bounded Python-process enforcement, not an OS sandbox.
"""
import ipaddress
import os
from pathlib import Path
import socket
import sys
from urllib.parse import unquote, urlsplit


def _loopback(host):
    if host is None or host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (TypeError, ValueError):
        return False


_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo
_sendto = socket.socket.sendto
_allowed_port = int(os.environ.get("P2_ALLOWED_PORT", "0"))


def _allowed(address):
    return (isinstance(address, tuple) and _loopback(address[0])
            and _allowed_port > 0 and address[1] == _allowed_port)


def _guard_connect(self, address):
    fallback = getattr(socket, "_fallback_socketpair", None)
    if fallback is not None and sys._getframe(1).f_code is fallback.__code__:
        return _connect(self, address)
    if _allowed(address):
        return _connect(self, address)
    raise OSError("Phase 2 fixture blocked outbound connection")


def _guard_connect_ex(self, address):
    return _connect_ex(self, address) if _allowed(address) else 10013


def _guard_getaddrinfo(host, *args, **kwargs):
    if not _loopback(host):
        raise OSError("Phase 2 fixture blocked external DNS")
    return _getaddrinfo(host, *args, **kwargs)


def _guard_sendto(self, data, *args):
    if not _allowed(args[-1] if args else None):
        raise OSError("Phase 2 fixture blocked outbound datagram")
    return _sendto(self, data, *args)


socket.socket.connect = _guard_connect
socket.socket.connect_ex = _guard_connect_ex
socket.getaddrinfo = _guard_getaddrinfo
socket.socket.sendto = _guard_sendto
_real_roots = [(Path.home() / name).resolve() for name in (".row-bot", ".thoth")]
_real_roots.append((Path.home() / "Documents" / "Row-Bot").resolve())


def _audit(event, args):
    single = {"open", "sqlite3.connect", "os.mkdir", "os.remove", "os.rmdir", "os.listdir",
              "os.scandir", "os.truncate", "os.chmod", "os.utime"}
    pairs = {"os.rename", "os.link", "os.symlink", "shutil.copyfile", "shutil.copymode", "shutil.copystat"}
    if event not in single | pairs or not args:
        return
    for candidate in args[:2] if event in pairs else args[:1]:
        if not isinstance(candidate, (str, bytes)):
            continue
        raw = os.fsdecode(candidate)
        if event == "sqlite3.connect" and raw.startswith("file:"):
            raw = unquote(urlsplit(raw).path)
            if os.name == "nt" and raw.startswith("/") and raw[2:3] == ":":
                raw = raw[1:]
        try:
            path = Path(raw).resolve()
        except (ValueError, OSError):
            continue
        if any(path == root or root in path.parents for root in _real_roots):
            raise PermissionError("Phase 2 forbids access to real application data")


sys.addaudithook(_audit)
