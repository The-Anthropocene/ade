# id: ade.host
# what: the platform layer: file lock, UTF-8 text with LF line endings, the running interpreter, folder links; OS differences live only here
# cmd: -
# reads: -
# writes: -
import os
import shutil
import signal
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

# Fills {python} in commands: `python3` is not a command on Windows, and this is the interpreter running ade.
PYTHON = sys.executable


def read(p, errors="strict"):
    return Path(p).read_text(encoding="utf-8", errors=errors)


def write(p, text):
    # Generated files are byte-identical on every machine, so `ade graph --check` agrees across them.
    Path(p).write_text(text, encoding="utf-8", newline="\n")


def opentext(p, mode="r"):
    return open(p, mode, encoding="utf-8", newline="")


@contextmanager
def lock(path, note=None):
    """Exclusive lock for the block, held on a sibling lock file: Windows locks are mandatory, so locking
    the data file itself would make every unlocked reader fail while a writer holds it. `note`, if given, is
    printed once when the lock is already held and this call has to wait."""
    with open(path, "a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            told = False
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # gives up after ~10s; keep waiting
                    break
                except OSError:
                    if note and not told:
                        print(note, flush=True)
                        told = True
            unlock = lambda: (f.seek(0), msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if note:
                    print(note, flush=True)
                fcntl.flock(f, fcntl.LOCK_EX)
            unlock = lambda: fcntl.flock(f, fcntl.LOCK_UN)
        try:
            yield
        finally:
            unlock()


def which(argv):
    # Windows finds `npx` only as npx.cmd; resolving through PATHEXT lets one command line work everywhere.
    found = shutil.which(argv[0]) if os.name == "nt" and argv else None
    return [found, *argv[1:]] if found else argv


def job_new(proc):
    """Windows: put a started process in a kill-on-close Job Object so its whole tree can be ended later, and only
    this tree. Returns the job handle, or None off Windows or when setup fails (callers then fall back)."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class Basic(ctypes.Structure):
        _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                    ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD),
                    ("f", ctypes.c_ssize_t), ("g", wintypes.DWORD), ("h", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in "abcdef"]

    class Extended(ctypes.Structure):
        _fields_ = [("Basic", Basic), ("Io", IoCounters), ("p", ctypes.c_size_t), ("j", ctypes.c_size_t),
                    ("pm", ctypes.c_size_t), ("pk", ctypes.c_size_t)]

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    try:
        info = Extended()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # ExtendedLimitInformation
                and k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle)))):
            return job
    except (OSError, AttributeError, TypeError, ValueError):
        pass  # a process without a real handle (a test double): fall back like any failed setup
    k32.CloseHandle(job)
    return None


def job_end(job):
    """Terminate every process left in the job, then close it. A closed job (kill-on-close) also ends them."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.TerminateJobObject(job, 1)
    k32.CloseHandle(job)


def end_tree(proc, job, force):
    """End the engine's process tree: the job on Windows (taskkill when there is none and force is set), else its group."""
    if os.name == "nt":
        if job:
            job_end(job)
        elif force:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def substitute(argv, **values):
    """Fill {name} tokens after splitting, so a Windows interpreter path's backslashes and spaces survive."""
    out = []
    for tok in argv:
        v = values.get(tok[1:-1]) if tok.startswith("{") and tok.endswith("}") else None
        out += v if isinstance(v, list) else [v] if v is not None else [tok]
    return out


def link_dir(src, dst):
    """dst becomes a link to the folder src. Windows makes a symlink only in developer mode, so it falls back to a
    junction, which any user may make."""
    try:
        os.symlink(src, dst, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)], check=True, capture_output=True)


def is_link(p):
    # A junction is a reparse point that islink misses; os.path.isjunction only arrived in Python 3.12.
    if os.name == "nt":
        try:
            return bool(os.lstat(p).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except OSError:
            return False
    return os.path.islink(p)


def unlink_dir(p):
    # The link only, never what it points at; on Windows a folder link (symlink or junction) goes with rmdir.
    if is_link(p):
        (os.rmdir if os.name == "nt" else os.unlink)(p)
