#!/usr/bin/env python3
"""Provide the limited ps interface required inside kit verification sandboxes."""

import ctypes
import os
import sys
import time


class ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_tvsec", ctypes.c_uint64),
        ("start_tvusec", ctypes.c_uint64),
    ]


def load_libproc():
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libproc.proc_listpids.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_listpids.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_pidinfo.restype = ctypes.c_int
    return libproc


def process_start(info):
    value = time.localtime(info.start_tvsec)
    return (
        f"{time.strftime('%a %b', value)} {value.tm_mday} "
        f"{time.strftime('%H:%M:%S %Y', value)}"
    )


def linux_process_start(pid):
    with open(f"/proc/{pid}/stat", encoding="utf-8") as stream:
        tail = stream.read().rpartition(")")[2].split()
    if len(tail) < 20 or not tail[19].isdigit():
        raise OSError("Linux process record is malformed")
    return f"linux-start-{tail[19]}"


def process_table():
    if sys.platform == "linux":
        for pid in sorted(
            int(name) for name in os.listdir("/proc") if name.isdigit()
        ):
            if pid <= 1:
                continue
            try:
                pgid = os.getpgid(pid)
                started = linux_process_start(pid)
            except (OSError, ProcessLookupError, ValueError):
                continue
            if pgid > 1:
                print(pid, pgid, started)
        return
    libproc = load_libproc()
    needed = libproc.proc_listpids(1, 0, None, 0)
    if needed <= 0:
        raise SystemExit(2)
    capacity = needed + 4096
    pids = (ctypes.c_int * ((capacity + 3) // 4))()
    used = libproc.proc_listpids(1, 0, pids, ctypes.sizeof(pids))
    if used <= 0 or used >= ctypes.sizeof(pids):
        raise SystemExit(2)
    rows = []
    for pid in pids[: used // 4]:
        if pid <= 1:
            continue
        info = ProcBsdInfo()
        size = libproc.proc_pidinfo(
            pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
        if size == ctypes.sizeof(info) and info.pid == pid and info.pgid > 1:
            rows.append((pid, info.pgid, process_start(info)))
    for pid, pgid, started in sorted(rows):
        print(pid, pgid, started)


if sys.argv[1:] == ["-axo", "pid=,pgid=,lstart="]:
    process_table()
elif "-o" in sys.argv and "-p" in sys.argv:
    try:
        output = sys.argv[sys.argv.index("-o") + 1]
        pid = int(sys.argv[sys.argv.index("-p") + 1])
    except (ValueError, IndexError):
        raise SystemExit(2)
    if output == "pgid=":
        print(os.getpgid(pid))
    elif output == "lstart=":
        if sys.platform == "linux":
            try:
                print(linux_process_start(pid))
            except (OSError, ProcessLookupError, ValueError):
                raise SystemExit(1)
        else:
            info = ProcBsdInfo()
            libproc = load_libproc()
            size = libproc.proc_pidinfo(
                pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)
            )
            if size != ctypes.sizeof(info) or info.pid != pid:
                raise SystemExit(1)
            print(process_start(info))
    else:
        raise SystemExit(2)
else:
    raise SystemExit(2)
