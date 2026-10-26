"""Cross-platform test helper.

AutoGpuAffinity is a Windows program: several modules import ``winreg``,
``win32api`` or ``wmi`` at module scope.  On Linux those modules cannot be
imported at all, which used to turn the whole test run into a collection
error instead of a clear "not applicable here" result.

Tests use :func:`require` to convert that import failure into a *skip*:
nothing is mocked or faked, so a Windows runner still executes every test
for real (``python -m unittest discover -s tests`` on windows-latest CI).
"""

import functools
import sys
import unittest

WINDOWS_ONLY_MODULES = ('winreg', 'win32api', 'win32con', 'win32gui', 'win32process', 'wmi', 'pythoncom')

IS_WINDOWS = sys.platform == 'win32'

def _available(name):
    """True when ``name`` can be imported.

    ``find_spec`` raises ``ValueError`` for already-imported modules that carry
    no ``__spec__`` (frozen/extension stubs), so fall back to a real import.
    """
    import importlib.util
    if name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        try:
            __import__(name)
        except ImportError:
            return False
        return True

def _missing_windows_modules():
    return [name for name in WINDOWS_ONLY_MODULES if not _available(name)]

def require(*modules):
    """Import ``modules`` or raise ``unittest.SkipTest`` naming what is missing."""
    try:
        return tuple(__import__(name) for name in modules)
    except ImportError as error:
        missing = [name for name in modules if name not in sys.modules] or [str(error)]
        raise unittest.SkipTest(
            'Windows-only dependency unavailable on %s: %s'
            % (sys.platform, ', '.join(missing))
        ) from error

def windows_only(reason):
    """Class/method decorator: skip the whole unit unless running on Windows."""
    return unittest.skipUnless(IS_WINDOWS and not _missing_windows_modules(), reason)

def skip_without_windows_api(target):
    """Module-level guard: skip every test in this file when Windows APIs are absent."""
    missing = _missing_windows_modules()
    if not IS_WINDOWS or missing:
        reason = 'requires Windows (%s)' % (
            'missing ' + ', '.join(missing) if missing else sys.platform
        )
        if isinstance(target, type) and issubclass(target, unittest.TestCase):
            return unittest.skip(reason)(target)
        return unittest.skip(reason)(target)
    return target
