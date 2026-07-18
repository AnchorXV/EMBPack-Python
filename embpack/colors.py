# colors.py — ANSI color helpers and Windows console initialization

import sys


def _enable_win_ansi() -> None:
    """Enable ANSI escape codes on Windows via SetConsoleMode."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        for handle_id in (6, 7):   # stdout / stderr handles
            handle = kernel32.GetStdHandle(-10 - handle_id)
            if handle:
                mode = ctypes.c_ulong()
                if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                    kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


_enable_win_ansi()


class C:
    """ANSI colour helpers."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    RED     = "\033[91m"
    YELLOW  = "\033[93m"
    GREEN   = "\033[92m"
    CYAN    = "\033[96m"
    BLUE    = "\033[94m"
    GREY    = "\033[90m"
    WHITE   = "\033[97m"

    @staticmethod
    def error(msg: str) -> str:
        return f"{C.RED}{C.BOLD}[ERROR]{C.RESET} {C.RED}{msg}{C.RESET}"

    @staticmethod
    def warn(msg: str) -> str:
        return f"{C.YELLOW}{C.BOLD}[WARN]{C.RESET}  {C.YELLOW}{msg}{C.RESET}"

    @staticmethod
    def ok(msg: str) -> str:
        return f"{C.GREEN}{C.BOLD}[OK]{C.RESET}    {C.GREEN}{msg}{C.RESET}"

    @staticmethod
    def info(msg: str) -> str:
        return f"{C.CYAN}[INFO]{C.RESET}  {msg}"

    @staticmethod
    def detail(msg: str) -> str:
        return f"{C.GREY}        {msg}{C.RESET}"
