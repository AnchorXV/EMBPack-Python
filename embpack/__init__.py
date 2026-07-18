# embpack/__init__.py — version and public re-exports

__version__ = "3.3.0"

from embpack.models import EMB, EMBFile, EMBLoadError
from embpack.logger import log

__all__ = [
    "__version__",
    "EMB",
    "EMBFile",
    "EMBLoadError",
    "log",
]
