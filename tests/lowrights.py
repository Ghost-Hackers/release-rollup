"""Run Python as the current Windows user without administrator rights.

GitHub's Windows runner runs the tests as an elevated administrator, which lists a folder
through a deny entry. The child gets a restricted copy of our own token: Administrators is
deny-only and every privilege is gone except bypass traverse checking.
"""
import sys

if sys.platform == 'win32':
    import ctypes
    import msvcrt
    import os
    import subprocess
    import tempfile
    from ctypes import wintypes

    TOKEN_ASSIGN_PRIMARY, TOKEN_DUPLICATE, TOKEN_QUERY = 0x1, 0x2, 0x8
    DISABLE_MAX_PRIVILEGE = 0x1
    STARTF_USESTDHANDLES = 0x100
    ADMINISTRATORS = 'S-1-5-32-544'

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [('Sid', ctypes.c_void_p), ('Attributes', wintypes.DWORD)]

    class StartupInfo(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('lpReserved', wintypes.LPWSTR), ('lpDesktop', wintypes.LPWSTR),
                    ('lpTitle', wintypes.LPWSTR), ('dwX', wintypes.DWORD), ('dwY', wintypes.DWORD),
                    ('dwXSize', wintypes.DWORD), ('dwYSize', wintypes.DWORD), ('dwXCountChars', wintypes.DWORD),
                    ('dwYCountChars', wintypes.DWORD), ('dwFillAttribute', wintypes.DWORD),
                    ('dwFlags', wintypes.DWORD), ('wShowWindow', wintypes.WORD), ('cbReserved2', wintypes.WORD),
                    ('lpReserved2', ctypes.c_void_p), ('hStdInput', wintypes.HANDLE),
                    ('hStdOutput', wintypes.HANDLE), ('hStdError', wintypes.HANDLE)]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [('hProcess', wintypes.HANDLE), ('hThread', wintypes.HANDLE),
                    ('dwProcessId', wintypes.DWORD), ('dwThreadId', wintypes.DWORD)]

    advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    PHANDLE = ctypes.POINTER(wintypes.HANDLE)
    advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, PHANDLE)
    advapi32.ConvertStringSidToSidW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
    advapi32.CreateRestrictedToken.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SidAndAttributes), wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, PHANDLE)
    advapi32.CreateProcessAsUserW.argtypes = (
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
        wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(StartupInfo),
        ctypes.POINTER(ProcessInformation))
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, wintypes.LPDWORD)
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)

    def check(ok: int) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def restricted_token() -> wintypes.HANDLE:
        """Our own primary token with Administrators deny-only and no privileges but bypass traverse."""
        token, admins, low = wintypes.HANDLE(), ctypes.c_void_p(), wintypes.HANDLE()
        # A restricted copy of the caller's token needs TOKEN_QUERY, TOKEN_DUPLICATE and TOKEN_ASSIGN_PRIMARY.
        check(advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_ASSIGN_PRIMARY | TOKEN_DUPLICATE
                                        | TOKEN_QUERY, ctypes.byref(token)))
        try:
            check(advapi32.ConvertStringSidToSidW(ADMINISTRATORS, ctypes.byref(admins)))
            try:
                check(advapi32.CreateRestrictedToken(token, DISABLE_MAX_PRIVILEGE, 1,
                                                     ctypes.byref(SidAndAttributes(admins, 0)), 0, None, 0, None,
                                                     ctypes.byref(low)))
            finally:
                kernel32.LocalFree(admins)
        finally:
            kernel32.CloseHandle(token)
        return low

    def run(argv: list[str], timeout: int = 120) -> tuple[int, str]:
        """Run Python with argv under restricted_token(). Returns the exit code and stdout with stderr."""
        with tempfile.TemporaryFile() as out, open(os.devnull, 'rb') as null:
            hin, hout = msvcrt.get_osfhandle(null.fileno()), msvcrt.get_osfhandle(out.fileno())
            os.set_handle_inheritable(hin, True)
            os.set_handle_inheritable(hout, True)
            si = StartupInfo(cb=ctypes.sizeof(StartupInfo), dwFlags=STARTF_USESTDHANDLES,
                             hStdInput=hin, hStdOutput=hout, hStdError=hout)
            pi = ProcessInformation()
            cmd = ctypes.create_unicode_buffer(subprocess.list2cmdline([sys.executable, *argv]))
            low = restricted_token()
            try:
                # The child inherits our environment, which carries coverage.py's settings for it.
                check(advapi32.CreateProcessAsUserW(low, sys.executable, cmd, None, None, True, 0, None, None,
                                                    ctypes.byref(si), ctypes.byref(pi)))
            finally:
                kernel32.CloseHandle(low)
            code = wintypes.DWORD()
            try:
                if kernel32.WaitForSingleObject(pi.hProcess, timeout * 1000) != 0:
                    kernel32.TerminateProcess(pi.hProcess, 1)
                    raise TimeoutError('%s did not finish in %d seconds' % (cmd.value, timeout))
                check(kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code)))
            finally:
                kernel32.CloseHandle(pi.hProcess)
                kernel32.CloseHandle(pi.hThread)
            out.seek(0)
            return code.value, out.read().decode(errors='replace')

    def grant(path: str) -> None:
        """Give this user full control of path and everything under it."""
        user = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, text=True, check=True)
        sid = user.stdout.strip().split(',')[-1].strip('"')
        subprocess.run(['icacls', path, '/grant', '*%s:(OI)(CI)F' % sid, '/t'], capture_output=True, check=True)
