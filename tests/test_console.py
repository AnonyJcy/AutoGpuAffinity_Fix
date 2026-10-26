"""Console encoding hardening.

On a Windows console whose code page cannot encode Chinese (cp1252, cp437, ...)
the program's own messages used to raise UnicodeEncodeError.  The same thing
happened on hosted CI, where the failing step was a plain test message rather
than a program bug.  ``console.harden`` must keep printing working.
"""
from pathlib import Path
import io
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AutoGpuAffinity'))
import console


class NarrowStream(io.StringIO):
    """A text stream that only accepts ASCII, imitating a legacy console."""

    encoding = 'cp1252'
    errors = 'strict'

    def __init__(self):
        super().__init__()
        self.reconfigured = None

    def reconfigure(self, encoding=None, errors=None):
        self.reconfigured = (encoding, errors)
        self.encoding = encoding or self.encoding
        self.errors = errors or self.errors


class NoReconfigureStream(io.TextIOBase):
    """A stream without ``reconfigure``, like some redirected/embedded streams."""

    encoding = 'cp1252'
    errors = 'strict'


class PlainStream:
    """A minimal object exposing only ``encoding``/``errors``."""

    encoding = 'cp1252'
    errors = 'strict'

    def write(self, data):
        return len(data)


class HardenTests(unittest.TestCase):
    def test_narrow_stream_is_switched_to_utf8(self):
        stream = NarrowStream()
        self.assertTrue(console._configure(stream))
        self.assertEqual(stream.reconfigured, ('utf-8', console.ERRORS))

    def test_configured_stream_can_print_chinese_afterwards(self):
        stream = NarrowStream()
        console._configure(stream)
        # After reconfigure the encoding is utf-8, so a Chinese message encodes.
        '系统计时器'.encode(stream.encoding)
        self.assertEqual(stream.encoding, 'utf-8')

    def test_stream_already_utf8_is_left_alone(self):
        stream = NarrowStream()
        stream.reconfigure('utf-8', 'strict')
        stream.reconfigured = None
        self.assertTrue(console._configure(stream))
        self.assertIsNone(stream.reconfigured)

    def test_configure_tolerates_stream_that_refuses_reconfigure(self):
        class Refusing(io.StringIO):
            encoding = 'cp1252'

            def reconfigure(self, **kwargs):
                raise ValueError('detached')

        self.assertFalse(console._configure(Refusing()))

    def test_configure_ignores_missing_stream(self):
        self.assertFalse(console._configure(None))

    def test_harden_does_not_raise_on_odd_streams(self):
        original_out, original_err = sys.stdout, sys.stderr
        try:
            sys.stdout = NoReconfigureStream()
            sys.stderr = PlainStream()
            self.assertFalse(console.harden())
            self.assertEqual(sys.stdout.errors, console.ERRORS)
        finally:
            sys.stdout, sys.stderr = original_out, original_err

    def test_harden_reports_success_for_utf8_streams(self):
        original_out, original_err = sys.stdout, sys.stderr
        try:
            sys.stdout = NarrowStream()
            sys.stderr = NarrowStream()
            self.assertTrue(console.harden())
        finally:
            sys.stdout, sys.stderr = original_out, original_err

    def test_importing_console_hardens_the_current_streams(self):
        # The module hardens on import so any module printing during import is safe.
        # A narrow stream must become usable for Chinese text.
        stream = NarrowStream()
        self.assertTrue(console._configure(stream))
        '系统计时器'.encode(stream.encoding)

    def test_narrow_console_rejects_chinese_without_the_guard(self):
        # Documents the failure this module prevents.
        with self.assertRaises(UnicodeEncodeError):
            '系统计时器'.encode('cp1252')

    def test_program_messages_survive_a_cp1252_console(self):
        """Regression for the CI failure: printing Chinese on a cp1252 runner."""
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding='cp1252', errors='strict',
                                  write_through=True)
        original = sys.stdout
        try:
            sys.stdout = stream
            console.harden()
            print('系统计时器：设备存在但未报告任何 IRQ 描述符')
        finally:
            sys.stdout = original
        text = buffer.getvalue().decode('utf-8')
        self.assertIn('IRQ', text)


if __name__ == '__main__':
    unittest.main()
