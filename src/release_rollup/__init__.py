"""Roll published release folders up into zips."""
from .core import (DEFAULT_MARKERS, Fail, apply, make_trash, plan, scan, verify)

__all__ = ['DEFAULT_MARKERS', 'Fail', 'apply', 'make_trash', 'plan', 'scan', 'verify']

try:
    import os as _os
    with open(_os.path.join(_os.path.dirname(__file__), 'VERSION')) as _f:
        __version__ = _f.read().strip()
except OSError:
    __version__ = '0.1.1'
