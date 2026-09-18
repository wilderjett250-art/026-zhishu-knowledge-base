from __future__ import annotations

from pathlib import Path
from typing import BinaryIO


class WindowsFileLock:
    """A one-byte non-blocking lock whose ownership ends with the process handle."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: BinaryIO | None = None

    def acquire(self) -> bool:
        import msvcrt

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        import msvcrt

        self.handle.seek(0)
        msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        self.handle.close()
        self.handle = None

    def __enter__(self) -> WindowsFileLock:
        if not self.acquire():
            raise RuntimeError("本地资源已由另一个 PKAS 进程持有。")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
