"""Unit tests for :mod:`libcob_py.termio`.

Exercises the pure-Python port of the C runtime ``libcob/termio.c`` - the basic
non-curses ``ACCEPT`` / ``DISPLAY`` fallback (the plain stream path used when
the SCREEN SECTION / curses subsystem is unavailable, and the
graceful-degradation target for :mod:`libcob_py.screenio`).  Per the AAP
coverage gate (sections 0.6.5 / 0.7.1, >=80% line coverage) the suite covers:

* ``DISPLAY`` of alphanumeric, NUMERIC-DISPLAY (plain and pretty, signed and
  unsigned, leading and trailing sign, comma decimal point), NUMERIC-EDITED
  (verbatim byte-dump), COMP-1 / COMP-2 (float / double), POINTER, and BINARY
  (the ``bin_digits`` digit-count override path),
* stream routing (stdout vs stderr, ``to_stderr`` and the
  ``cob_screen_initialized`` redirect) and the ``WITH NO ADVANCING`` newline
  suppression,
* ``ACCEPT`` into alphanumeric and numeric receivers, the field-size cap for a
  NUMERIC-DISPLAY receiver, and the end-of-input / empty-line behaviour,
* ``cob_init_termio`` buffer allocation,
* the defensive guards (OMITTED operand, zero-size numeric, the text-stream
  emit fallback, a non-positive ``varcnt``).

Standard library only - the runtime under test introduces ZERO third-party
dependencies; ``pytest`` is a development-only test framework (AAP 0.5 / 0.7.1).

``libcob_py.termio`` performs the USAGE -> zoned-digits value decode by
delegating to the data-movement runtime through ``common._lazy_move``.  The
sibling ``libcob_py.move`` is built in parallel and its exact output is not the
subject under test here, so each test installs a small, **standard-library-only**
controlled ``_lazy_move`` double (the established pattern - see
``tests/libcob_py/test_numeric.py``).  This keeps the assertions deterministic
and focused on ``termio``'s own formatting / layout / routing logic regardless
of whether the real ``move`` module exists yet.
"""
import io
import struct

import pytest

# Obtain the parallel-built runtime lazily so collection degrades to a clean
# SKIP (never a hard error) when the package is not yet importable.
libcob_py = pytest.importorskip("libcob_py")
common = pytest.importorskip("libcob_py.common")
termio = pytest.importorskip("libcob_py.termio")


# ===========================================================================
# Field factory + the controlled ``common._lazy_move`` double.
#
# The double reproduces just enough ``cob_move`` behaviour for the field pairs
# ``termio`` actually produces / consumes: a NUMERIC-DISPLAY or ALPHANUMERIC
# receiver fed from a NUMERIC-DISPLAY / NUMERIC-BINARY / ALPHANUMERIC source.
# It zero-fills the magnitude to the receiver's digit count and places the
# SEPARATE sign per the receiver flags, exactly the contract ``termio`` relies
# on (a real ``cob_move`` produces the identical zoned bytes).
# ===========================================================================
def _mk(data=b"", type=common.COB_TYPE_UNKNOWN, digits=0, scale=0, flags=0,
        size=None):
    """Build a :class:`libcob_py.common.cob_field` from pre-encoded bytes."""
    payload = bytes(data)
    attr = common.cob_field_attr(type=type, digits=digits, scale=scale,
                                 flags=flags, pic=None)
    fsize = len(payload) if size is None else size
    if len(payload) < fsize:
        payload = payload + b"\x00" * (fsize - len(payload))
    return common.cob_field(size=fsize, data=bytearray(payload), attr=attr)


def _src_value(src):
    """Decode the logical (signed, unscaled) integer value of *src*."""
    ftype = src.attr.type
    if ftype == common.COB_TYPE_ALPHANUMERIC:
        text = bytes(src.data[:src.size]).decode("latin-1").strip()
        negative = text.startswith("-")
        digits = "".join(c for c in text if c.isdigit())
        value = int(digits) if digits else 0
        return -value if negative else value
    if ftype == common.COB_TYPE_NUMERIC_DISPLAY:
        data = bytes(src.data[:src.size])
        sign = 1
        if src.attr.flags & common.COB_FLAG_SIGN_SEPARATE:
            if src.attr.flags & common.COB_FLAG_SIGN_LEADING:
                sign_byte, digit_bytes = data[0:1], data[1:]
            else:
                sign_byte, digit_bytes = data[-1:], data[:-1]
            if sign_byte == b"-":
                sign = -1
        else:
            digit_bytes = data
        magnitude = "".join(chr(b) for b in digit_bytes if 0x30 <= b <= 0x39)
        return sign * (int(magnitude) if magnitude else 0)
    if ftype == common.COB_TYPE_NUMERIC_BINARY:
        signed = bool(src.attr.flags & common.COB_FLAG_HAVE_SIGN)
        return int.from_bytes(bytes(src.data[:src.size]), "big", signed=signed)
    return 0


def _fake_lazy_move(src, dst):
    """Deterministic stand-in for ``move.cob_move`` (NUMERIC-DISPLAY / ALNUM)."""
    value = _src_value(src)
    dtype = dst.attr.type
    if dtype == common.COB_TYPE_NUMERIC_DISPLAY:
        digits = dst.attr.digits
        magnitude = str(abs(value)).zfill(digits)[-digits:] if digits else ""
        sign_byte = b"-" if value < 0 else b"+"
        if dst.attr.flags & common.COB_FLAG_SIGN_SEPARATE:
            if dst.attr.flags & common.COB_FLAG_SIGN_LEADING:
                out = sign_byte + magnitude.encode("latin-1")
            else:
                out = magnitude.encode("latin-1") + sign_byte
        else:
            out = magnitude.encode("latin-1")
        out = out[:dst.size].ljust(dst.size, b"0")
        dst.data[:dst.size] = bytearray(out)
    elif dtype == common.COB_TYPE_ALPHANUMERIC:
        raw = bytes(src.data[:src.size])
        out = raw[:dst.size].ljust(dst.size, b" ")
        dst.data[:dst.size] = bytearray(out)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Install the controlled MOVE double and a clean module/screen baseline."""
    monkeypatch.setattr(common, "_lazy_move", _fake_lazy_move)
    monkeypatch.setattr(common, "cob_current_module", None)
    if hasattr(common, "cob_screen_initialized"):
        monkeypatch.delattr(common, "cob_screen_initialized", raising=False)


# ===========================================================================
# DISPLAY - alphanumeric and edited (no MOVE involved)
# ===========================================================================
class TestDisplayText:
    def test_alphanumeric(self, capsys):
        termio.cob_display(0, 1, 1, _mk(b"HELLO", common.COB_TYPE_ALPHANUMERIC))
        captured = capsys.readouterr()
        assert captured.out == "HELLO\n"
        assert captured.err == ""

    def test_numeric_edited_is_byte_dumped(self, capsys):
        # NUMERIC-EDITED (0x24) is NOT numeric under COB_FIELD_IS_NUMERIC
        # (0x24 & 0x10 == 0); it already holds its formatted text, so it is
        # emitted verbatim through the alphanumeric path.
        termio.cob_display(
            0, 1, 1, _mk(b"$1,234.56", common.COB_TYPE_NUMERIC_EDITED,
                         digits=6, scale=2))
        assert capsys.readouterr().out == "$1,234.56\n"

    def test_omitted_operand_contributes_nothing(self, capsys):
        # A field modelling an OMITTED argument (data is None) is skipped.
        omitted = common.cob_field(size=0, data=None,
                                   attr=common.cob_field_attr(
                                       type=common.COB_TYPE_ALPHANUMERIC))
        termio.cob_display(0, 1, 1, omitted)
        assert capsys.readouterr().out == "\n"

    def test_display_alnum_guards_none_data(self):
        # The alphanumeric formatter itself tolerates a None-data field,
        # returning empty bytes (defensive guard).
        omitted = common.cob_field(size=0, data=None,
                                   attr=common.cob_field_attr(
                                       type=common.COB_TYPE_ALPHANUMERIC))
        assert termio._display_alnum(omitted) == b""


# ===========================================================================
# DISPLAY - NUMERIC-DISPLAY plain (non-pretty) and pretty layouts
# ===========================================================================
class TestDisplayNumeric:
    def _plain_module(self, monkeypatch):
        monkeypatch.setattr(common, "cob_current_module",
                            common.cob_module(flag_pretty_display=0))

    def test_pretty_signed_negative_with_scale(self, capsys):
        # PIC S9(3)V9(2), value -12.34 -> fixed leading sign + decimal point.
        f = _mk(b"-01234", common.COB_TYPE_NUMERIC_DISPLAY, digits=5, scale=2,
                flags=common.COB_FLAG_HAVE_SIGN | common.COB_FLAG_SIGN_SEPARATE
                | common.COB_FLAG_SIGN_LEADING, size=6)
        termio.cob_display(0, 1, 1, f)
        assert capsys.readouterr().out == "-012.34\n"

    def test_pretty_unsigned_with_scale(self, capsys):
        f = _mk(b"1234", common.COB_TYPE_NUMERIC_DISPLAY, digits=4, scale=2)
        termio.cob_display(0, 1, 1, f)
        assert capsys.readouterr().out == "12.34\n"

    def test_pretty_signed_scale_zero(self, capsys):
        f = _mk(b"+00042", common.COB_TYPE_NUMERIC_DISPLAY, digits=5, scale=0,
                flags=common.COB_FLAG_HAVE_SIGN | common.COB_FLAG_SIGN_SEPARATE
                | common.COB_FLAG_SIGN_LEADING, size=6)
        termio.cob_display(0, 1, 1, f)
        assert capsys.readouterr().out == "+00042\n"

    def test_pretty_honours_module_decimal_comma(self, capsys, monkeypatch):
        monkeypatch.setattr(common, "cob_current_module",
                            common.cob_module(decimal_point=ord(","),
                                              flag_pretty_display=1))
        f = _mk(b"1234", common.COB_TYPE_NUMERIC_DISPLAY, digits=4, scale=2)
        termio.cob_display(0, 1, 1, f)
        assert capsys.readouterr().out == "12,34\n"

    def test_plain_trailing_sign(self, capsys, monkeypatch):
        self._plain_module(monkeypatch)
        f = _mk(b"012-", common.COB_TYPE_NUMERIC_DISPLAY, digits=3, scale=0,
                flags=common.COB_FLAG_HAVE_SIGN | common.COB_FLAG_SIGN_SEPARATE,
                size=4)
        termio.cob_display(0, 1, 1, f)
        # Plain path emits zoned digits + a SEPARATE sign, NO decimal point.
        assert capsys.readouterr().out == "012-\n"

    def test_plain_leading_sign(self, capsys, monkeypatch):
        self._plain_module(monkeypatch)
        f = _mk(b"-012", common.COB_TYPE_NUMERIC_DISPLAY, digits=3, scale=0,
                flags=common.COB_FLAG_HAVE_SIGN | common.COB_FLAG_SIGN_SEPARATE
                | common.COB_FLAG_SIGN_LEADING, size=4)
        termio.cob_display(0, 1, 1, f)
        assert capsys.readouterr().out == "-012\n"

    def test_plain_unsigned(self, capsys, monkeypatch):
        self._plain_module(monkeypatch)
        f = _mk(b"012", common.COB_TYPE_NUMERIC_DISPLAY, digits=3, scale=0)
        termio.cob_display(0, 1, 1, f)
        assert capsys.readouterr().out == "012\n"

    def test_plain_zero_size_emits_nothing(self, capsys, monkeypatch):
        self._plain_module(monkeypatch)
        f = _mk(b"", common.COB_TYPE_NUMERIC_DISPLAY, digits=0, scale=0, size=0)
        termio.cob_display(0, 1, 1, f)
        assert capsys.readouterr().out == "\n"  # only the trailing newline

    def test_pretty_zero_size_emits_nothing(self, capsys):
        # Pretty path (default module) also short-circuits on a zero-size field.
        f = _mk(b"", common.COB_TYPE_NUMERIC_DISPLAY, digits=0, scale=0, size=0)
        termio.cob_display(0, 1, 1, f)
        assert capsys.readouterr().out == "\n"


# ===========================================================================
# DISPLAY - COMP-1 / COMP-2 / POINTER / BINARY
# ===========================================================================
class TestDisplayBinaryFloatPointer:
    def test_double(self, capsys):
        termio.cob_display(
            0, 1, 1, _mk(struct.pack("=d", 3.5), common.COB_TYPE_NUMERIC_DOUBLE))
        assert capsys.readouterr().out == ("%-.18f" % 3.5) + "\n"

    def test_float(self, capsys):
        termio.cob_display(
            0, 1, 1, _mk(struct.pack("=f", 1.5), common.COB_TYPE_NUMERIC_FLOAT))
        assert capsys.readouterr().out == ("%-.18f" % 1.5) + "\n"

    def test_pointer_msb_first(self, capsys):
        # Pointer value 0x0102 stored native-endian; display is MSB-first.
        raw = (0x0102).to_bytes(8, "little")
        f = _mk(raw, common.COB_TYPE_ALPHANUMERIC,
                flags=common.COB_FLAG_IS_POINTER, size=8)
        termio.cob_display(0, 1, 1, f)
        assert capsys.readouterr().out == "0x0000000000000102\n"

    def test_real_binary_branch_overrides_digits(self, capsys):
        # REAL_BINARY forces the plain BINARY path even with pretty display on:
        # a 2-byte item shows bin_digits[2] = 5 digits; value 100 with a sign
        # is emitted with a leading SEPARATE sign ("+00100").
        f = _mk((100).to_bytes(2, "big"), common.COB_TYPE_NUMERIC_BINARY,
                digits=3,
                flags=common.COB_FLAG_HAVE_SIGN | common.COB_FLAG_REAL_BINARY,
                size=2)
        termio.cob_display(0, 1, 1, f)
        assert capsys.readouterr().out == "+00100\n"

    def test_binary_non_pretty_branch(self, capsys, monkeypatch):
        # Plain BINARY (not REAL_BINARY) takes the same override path when the
        # module disables pretty display: 1-byte -> bin_digits[1] = 3 digits.
        monkeypatch.setattr(common, "cob_current_module",
                            common.cob_module(flag_pretty_display=0))
        f = _mk((7).to_bytes(1, "big"), common.COB_TYPE_NUMERIC_BINARY,
                digits=2, flags=0, size=1)
        termio.cob_display(0, 1, 1, f)
        assert capsys.readouterr().out == "007\n"


# ===========================================================================
# DISPLAY - stream routing and newline handling
# ===========================================================================
class TestDisplayRouting:
    def test_to_stderr_routes_to_stderr(self, capsys):
        termio.cob_display(1, 1, 1, _mk(b"ERR", common.COB_TYPE_ALPHANUMERIC))
        captured = capsys.readouterr()
        assert captured.err == "ERR\n"
        assert captured.out == ""

    def test_no_advancing_suppresses_newline(self, capsys):
        termio.cob_display(0, 0, 1, _mk(b"AB", common.COB_TYPE_ALPHANUMERIC))
        assert capsys.readouterr().out == "AB"

    def test_varcnt_limits_displayed_fields(self, capsys):
        a = _mk(b"AA", common.COB_TYPE_ALPHANUMERIC)
        b = _mk(b"BB", common.COB_TYPE_ALPHANUMERIC)
        c = _mk(b"CC", common.COB_TYPE_ALPHANUMERIC)
        termio.cob_display(0, 1, 2, a, b, c)  # varcnt=2 -> only AA, BB
        assert capsys.readouterr().out == "AABB\n"

    def test_varcnt_zero_emits_only_newline(self, capsys):
        termio.cob_display(0, 1, 0)
        assert capsys.readouterr().out == "\n"

    def test_negative_varcnt_clamped_to_zero(self, capsys):
        termio.cob_display(0, 1, -1, _mk(b"X", common.COB_TYPE_ALPHANUMERIC))
        assert capsys.readouterr().out == "\n"

    def test_screen_initialized_routes_to_stderr(self, capsys, monkeypatch):
        monkeypatch.setattr(common, "cob_screen_initialized", True,
                            raising=False)
        termio.cob_display(0, 1, 1, _mk(b"X", common.COB_TYPE_ALPHANUMERIC))
        captured = capsys.readouterr()
        assert captured.err == "X\n"
        assert captured.out == ""

    def test_text_stream_fallback_without_buffer(self, monkeypatch):
        # When the target stream has no binary ``buffer`` (e.g. an io.StringIO),
        # termio decodes Latin-1 and writes text.  Patch sys.stdout in the test
        # body so the substitution sticks under pytest's capture.
        sink = io.StringIO()
        monkeypatch.setattr("sys.stdout", sink)
        termio.cob_display(0, 1, 1, _mk(b"PLAIN", common.COB_TYPE_ALPHANUMERIC))
        assert sink.getvalue() == "PLAIN\n"


# ===========================================================================
# ACCEPT
# ===========================================================================
class TestAccept:
    def test_into_alphanumeric(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("HELLO\n"))
        f = _mk(b"", common.COB_TYPE_ALPHANUMERIC, size=10)
        termio.cob_accept(f)
        assert bytes(f.data) == b"HELLO     "

    def test_into_numeric_display(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("12345\n"))
        f = _mk(b"", common.COB_TYPE_NUMERIC_DISPLAY, digits=5, size=5)
        termio.cob_accept(f)
        assert bytes(f.data) == b"12345"

    def test_numeric_display_caps_input_to_field_size(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("1234567\n"))
        f = _mk(b"", common.COB_TYPE_NUMERIC_DISPLAY, digits=5, size=5)
        termio.cob_accept(f)
        assert bytes(f.data) == b"12345"

    def test_eof_yields_single_space(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        f = _mk(b"", common.COB_TYPE_ALPHANUMERIC, size=3)
        termio.cob_accept(f)
        assert bytes(f.data) == b"   "

    def test_empty_line(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
        f = _mk(b"", common.COB_TYPE_ALPHANUMERIC, size=3)
        termio.cob_accept(f)
        assert bytes(f.data) == b"   "

    def test_no_trailing_newline_is_kept(self, monkeypatch):
        # A final line lacking a newline keeps all of its characters.
        monkeypatch.setattr("sys.stdin", io.StringIO("ABCDE"))
        f = _mk(b"", common.COB_TYPE_ALPHANUMERIC, size=5)
        termio.cob_accept(f)
        assert bytes(f.data) == b"ABCDE"


# ===========================================================================
# Initialisation
# ===========================================================================
class TestInit:
    def test_allocates_medium_buffer(self):
        termio._term_buff = None
        try:
            termio.cob_init_termio()
            assert isinstance(termio._term_buff, bytearray)
            assert len(termio._term_buff) == common.COB_MEDIUM_BUFF
        finally:
            termio._term_buff = None

    def test_init_is_discovered_in_runtime_order(self):
        # cob_init_termio runs after fileio and before call (AAP 0.5.3).
        order = common.COB_INIT_ORDER
        assert "termio" in order
        assert order.index("fileio") < order.index("termio") < order.index("call")
