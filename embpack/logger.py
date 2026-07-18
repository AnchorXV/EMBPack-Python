# logger.py — Logger class for embpack

import sys
from embpack.colors import C


class Logger:
    silent:  bool = False
    verbose: bool = False
    _errors: int  = 0
    _warns:  int  = 0

    def error(self, msg: str) -> None:
        self._errors += 1
        print(C.error(msg), file=sys.stderr)

    def warn(self, msg: str) -> None:
        self._warns += 1
        if not self.silent:
            print(C.warn(msg))

    def ok(self, msg: str) -> None:
        if not self.silent:
            print(C.ok(msg))

    def info(self, msg: str) -> None:
        if not self.silent:
            print(C.info(msg))

    def detail(self, msg: str) -> None:
        if self.verbose and not self.silent:
            print(C.detail(msg))

    def dry(self, msg: str) -> None:
        if not self.silent:
            print(f"{C.BLUE}{C.BOLD}[DRY]{C.RESET}   {C.BLUE}{msg}{C.RESET}")

    @property
    def has_errors(self) -> bool:
        return self._errors > 0

    @property
    def has_warns(self) -> bool:
        return self._warns > 0


# Module-level singleton — imported by other modules
log = Logger()
