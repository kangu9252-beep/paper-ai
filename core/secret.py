"""도서관 비밀번호를 Windows 계정에 묶어 암호화(DPAPI). 다른 PC/계정에서는 풀 수 없음."""
import base64
import ctypes
from ctypes import wintypes


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


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
    src, _keep = _blob(text.encode("utf-8"))
    out = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("암호화 실패")
    return "dpapi:" + base64.b64encode(_out(out)).decode()


def unprotect(token: str) -> str:
    if not token or not token.startswith("dpapi:"):
        return token or ""
    src, _keep = _blob(base64.b64decode(token[6:]))
    out = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        return ""
    return _out(out).decode("utf-8")
