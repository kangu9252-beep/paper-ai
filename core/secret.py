"""키·비밀번호 암호화. Windows는 계정에 묶는 DPAPI, 인터넷 서버(리눅스)는 서버 비밀값으로 Fernet."""
import base64
import ctypes
import os
import sys

if sys.platform == "win32":
    from ctypes import wintypes
else:  # 서버(리눅스)
    wintypes = None


def _fernet():
    from cryptography.fernet import Fernet
    import hashlib

    sec = os.environ.get("SECRET_KEY")
    if not sec:
        from . import db

        f = db.DATA / ".secret"
        if not f.exists():
            f.write_text(base64.urlsafe_b64encode(os.urandom(32)).decode())
        sec = f.read_text().strip()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(sec.encode()).digest()))


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(b: bytes):
    buf = ctypes.create_string_buffer(b, len(b))
    return _Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _out(blob):
    data = ctypes.string_at(blob.pbData, blob.cbData)
    ctypes.windll.kernel32.LocalFree(blob.pbData)
    return data


def protect(text: str) -> str:
    if not text:
        return ""
    if sys.platform != "win32":
        return "fernet:" + _fernet().encrypt(text.encode("utf-8")).decode()
    src, _keep = _blob(text.encode("utf-8"))
    out = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("암호화 실패")
    return "dpapi:" + base64.b64encode(_out(out)).decode()


def unprotect(token: str) -> str:
    if token and token.startswith("fernet:"):
        try:
            return _fernet().decrypt(token[7:].encode()).decode("utf-8")
        except Exception:
            return ""
    if not token or not token.startswith("dpapi:") or sys.platform != "win32":
        return token if token and not token.startswith("dpapi:") else ""
    src, _keep = _blob(base64.b64decode(token[6:]))
    out = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        return ""
    return _out(out).decode("utf-8")
