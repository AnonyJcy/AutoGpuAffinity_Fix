"""Console output safety.

The program prints Chinese text. When stdout/stderr is redirected, or the
console uses a legacy code page (cp1252, cp437, ...), Python's default text
encoder cannot represent those characters and every ``print`` raises
``UnicodeEncodeError``.  Non-Chinese Windows installations hit this on real
usage, and hosted CI runners do too.

``harden()`` switches the standard streams to UTF-8 where possible and sets an
error handler that degrades gracefully instead of raising, so the program keeps
working even on a console that genuinely cannot show the characters. It runs on
import, so modules that print during import are covered too, and ``main`` calls
it again for the ``__main__`` path.
"""

import sys

# Replacement errors surface as '?' rather than a traceback; that keeps output
# readable on an ANSI-only console without aborting a benchmark run.
ERRORS = 'replace'


def _configure(stream):
    if stream is None:
        return False
    encoding = (getattr(stream, 'encoding', '') or '').lower().replace('-', '')
    if encoding == 'utf8':
        return True
    reconfigure = getattr(stream, 'reconfigure', None)
    if reconfigure is None:
        return False
    try:
        reconfigure(encoding='utf-8', errors=ERRORS)
        return True
    except (ValueError, OSError, LookupError):
        # A stream that is already attached to a closed/odd descriptor keeps its
        # original settings; the error handler below still protects it.
        return False


def harden():
    """Make stdout/stderr tolerate characters the console code page lacks."""
    configured = [_configure(sys.stdout), _configure(sys.stderr)]
    if configured[0] and configured[1]:
        return True
    # Fallback for interpreters/streams where reconfigure() is unavailable: a
    # non-raising error handler keeps printing working.
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name, None)
        if stream is None or getattr(stream, 'reconfigure', None) is not None:
            continue
        try:
            stream.errors = ERRORS
        except (AttributeError, TypeError, ValueError):
            pass
    return False


harden()
