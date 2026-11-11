# -*- coding: utf-8 -*-
"""Windows 进程与可执行文件版本信息。"""

from __future__ import annotations

import ctypes
from pathlib import Path
from ctypes import wintypes


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_PROCESS_ID_COUNT = 4096


class _VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [
        ("dwSignature", wintypes.DWORD),
        ("dwStrucVersion", wintypes.DWORD),
        ("dwFileVersionMS", wintypes.DWORD),
        ("dwFileVersionLS", wintypes.DWORD),
        ("dwProductVersionMS", wintypes.DWORD),
        ("dwProductVersionLS", wintypes.DWORD),
        ("dwFileFlagsMask", wintypes.DWORD),
        ("dwFileFlags", wintypes.DWORD),
        ("dwFileOS", wintypes.DWORD),
        ("dwFileType", wintypes.DWORD),
        ("dwFileSubtype", wintypes.DWORD),
        ("dwFileDateMS", wintypes.DWORD),
        ("dwFileDateLS", wintypes.DWORD),
    ]


def get_process_path(pid: int) -> str:
    """按 PID 读取可执行文件路径；失败返回空字符串。"""

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        int(pid),
    )
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(2048)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            buf,
            ctypes.byref(size),
        ):
            return ""
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def get_process_ids_by_image_name(image_name: str) -> list[int]:
    """按映像名枚举进程，用于启动前防重入检查。"""

    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    size = wintypes.DWORD(MAX_PROCESS_ID_COUNT * ctypes.sizeof(wintypes.DWORD))
    buffer = ctypes.create_string_buffer(size.value)
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(buffer, size, ctypes.byref(needed)):
        return []

    count = min(
        needed.value // ctypes.sizeof(wintypes.DWORD),
        MAX_PROCESS_ID_COUNT,
    )
    pids = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))
    target = image_name.lower()
    result: list[int] = []
    for index in range(count):
        pid = pids[index]
        if not pid:
            continue
        path = get_process_path(pid)
        if path and Path(path).name.lower() == target:
            result.append(pid)
    return result


def get_file_version(path: str) -> str:
    """读取 exe/dll 的 VS_FIXEDFILEINFO 版本号。"""

    if not path:
        return ""
    version = ctypes.windll.version
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return ""
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, data):
        return ""
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(
        data,
        "\\",
        ctypes.byref(pointer),
        ctypes.byref(length),
    ) or not pointer:
        return ""
    info = ctypes.cast(
        pointer,
        ctypes.POINTER(_VS_FIXEDFILEINFO),
    ).contents
    return ".".join(
        str(value)
        for value in (
            (info.dwFileVersionMS >> 16) & 0xFFFF,
            info.dwFileVersionMS & 0xFFFF,
            (info.dwFileVersionLS >> 16) & 0xFFFF,
            info.dwFileVersionLS & 0xFFFF,
        )
    )


def get_process_version(pid: int) -> str:
    return get_file_version(get_process_path(pid))
