import ctypes
from ctypes import wintypes
from pathlib import Path

from pkas.config import Settings


class LocalSecretError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    value = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return value, buffer


def _secret_path(settings: Settings, name: str) -> Path:
    if not name.replace("_", "").isalnum():
        raise LocalSecretError("密钥名称不合法。")
    return settings.data_root / "secrets" / f"{name}.dpapi"


def protect_user_bytes(value: bytes, *, description: str = "PKAS protected data") -> bytes:
    """Protect arbitrary bytes for the current Windows user without logging content."""
    if not value:
        raise LocalSecretError("受保护内容不能为空。")
    if not hasattr(ctypes, "windll"):
        raise LocalSecretError("当前系统不支持 Windows DPAPI。")
    source, source_buffer = _blob(value)
    _ = source_buffer
    protected = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source),
        description,
        None,
        None,
        None,
        0,
        ctypes.byref(protected),
    )
    if not ok:
        raise LocalSecretError("Windows DPAPI 加密失败。")
    try:
        return ctypes.string_at(protected.pbData, protected.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(protected.pbData)


def unprotect_user_bytes(value: bytes) -> bytes:
    """Unprotect bytes previously protected for the current Windows user."""
    if not value:
        raise LocalSecretError("受保护内容不能为空。")
    if not hasattr(ctypes, "windll"):
        raise LocalSecretError("当前系统不支持 Windows DPAPI。")
    source, source_buffer = _blob(value)
    _ = source_buffer
    clear = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(clear),
    )
    if not ok:
        raise LocalSecretError("Windows DPAPI 解密失败，数据可能属于其他用户。")
    try:
        return ctypes.string_at(clear.pbData, clear.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(clear.pbData)


def save_user_secret(settings: Settings, name: str, value: str) -> Path:
    if not value.strip():
        raise LocalSecretError("密钥不能为空。")
    encrypted = protect_user_bytes(value.encode(), description="PKAS local secret")
    path = _secret_path(settings, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(encrypted)
    temporary.replace(path)
    return path


def load_user_secret(settings: Settings, name: str) -> str | None:
    path = _secret_path(settings, name)
    if not path.is_file():
        return None
    if not hasattr(ctypes, "windll"):
        return None
    return unprotect_user_bytes(path.read_bytes()).decode()
