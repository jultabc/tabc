"""Read argument boundaries from the OS; never reconstruct argv from ps prose."""
import ctypes
import os
import sys

MAX_BYTES = 1024 * 1024


def parse_darwin_arguments(raw):
    """KERN_PROCARGS2: argc, executable path, padding, argv, then environment.

    Stop at argc. Environment data must never be returned or logged.
    """
    width = ctypes.sizeof(ctypes.c_int)
    if len(raw) < width:
        return None
    argc = int.from_bytes(raw[:width], sys.byteorder, signed=True)
    if not 0 < argc <= 4096:
        return None
    pos = raw.find(b"\0", width)
    if pos < 0:
        return None
    while pos < len(raw) and raw[pos] == 0:
        pos += 1
    argv = []
    for _ in range(argc):
        end = raw.find(b"\0", pos)
        if end < 0:
            return None
        argv.append(os.fsdecode(raw[pos:end]))
        pos = end + 1
    return argv


def native_arguments(pid):
    pid = int(pid)
    if pid <= 0:
        return None
    if sys.platform == "darwin":
        # SDK sys/sysctl.h: CTL_KERN=1, KERN_PROCARGS2=49.
        libc = ctypes.CDLL(None, use_errno=True)
        libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                               ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                               ctypes.c_void_p, ctypes.c_size_t]
        libc.sysctl.restype = ctypes.c_int
        query = (ctypes.c_int * 3)(1, 49, pid)
        size = ctypes.c_size_t()
        if libc.sysctl(query, 3, None, ctypes.byref(size), None, 0) != 0:
            return None
        if not 0 < size.value <= MAX_BYTES:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(query, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        if size.value > len(buf):
            return None
        return parse_darwin_arguments(buf.raw[:size.value])
    if sys.platform.startswith("linux"):
        with open(f"/proc/{pid}/cmdline", "rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if not raw or len(raw) > MAX_BYTES or not raw.endswith(b"\0"):
            return None
        return [os.fsdecode(arg) for arg in raw[:-1].split(b"\0")]
    return None
