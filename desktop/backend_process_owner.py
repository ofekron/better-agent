from __future__ import annotations

import os
import signal
import subprocess
import threading

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class BackendProcessOwner:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self._process = process
        self._job = self._create_windows_job(process) if os.name == "nt" else None
        self._closed = False
        self._lock = threading.Lock()

    @staticmethod
    def spawn_kwargs() -> dict[str, object]:
        if os.name != "nt":
            return {"start_new_session": True}
        flags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        return {"creationflags": flags}

    def signal(self, requested_signal: signal.Signals | None) -> None:
        if requested_signal is None and os.name != "nt":
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            return
        if requested_signal is None:
            self._terminate_windows_job()
            return
        if self._process.poll() is None:
            self._process.send_signal(requested_signal)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if os.name != "nt":
                try:
                    os.killpg(self._process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                return
            self._terminate_windows_job()
            self._close_windows_job()

    def _terminate_windows_job(self) -> None:
        if self._job:
            import ctypes

            ctypes.windll.kernel32.TerminateJobObject(self._job, 1)

    def _close_windows_job(self) -> None:
        if self._job:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._job)
            self._job = None

    @staticmethod
    def _create_windows_job(process: subprocess.Popen[str]):
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            9,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise ctypes.WinError(error)
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise ctypes.WinError(error)
        return job
