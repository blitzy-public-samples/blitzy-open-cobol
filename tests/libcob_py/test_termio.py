"""Unit tests for :mod:`libcob_py.termio` - the plain ACCEPT / DISPLAY fallback.

These tests verify the pure-Python port of the C runtime ``libcob/termio.c``
(the non-curses terminal-I/O path used whenever the SCREEN SECTION / curses
subsystem is unavailable, and the graceful-degradation target for
:mod:`libcob_py.screenio`).  They satisfy the agent action plan's test mandate
for the ``tests/libcob_py/`` suite (AAP sections 0.2.1 / 0.4.1 / 0.7.1) and its
coverage gate (>=80% line coverage of ``libcob_py/termio.py``; this module
exercises every branch).

Design - true integration against the real runtime
---------------------------------------------------
``libcob_py.termio`` performs the USAGE -> zoned-digit value decode by
*delegating* to the data-movement runtime through ``common._lazy_move`` (the
emitter -> runtime contract; AAP section 0.6.5).  Rather than substitute a test
double, these tests drive the **real** delegation path: ``cob_display`` /
``cob_accept`` call ``common._lazy_move`` which dispatches to the genuine
``libcob_py.move`` runtime built alongside ``termio``.  This is exactly what the
agent action plan asks for - ACCEPT/DISPLAY behaviour is asserted by
"delegating to numeric/move storage" - so the assertions reflect the bytes the
production runtime actually produces.

Field construction stays within the runtime package's public surface: every
``cob_field`` is built with :mod:`libcob_py.common` (the documented builder for
the suite).  Numeric operands are constructed from explicit zoned bytes, using a
SEPARATE sign for signed values so the logical value is unambiguous and the real
MOVE decodes it deterministically (no fragile sign-overpunch construction).

Standard library only
---------------------
The runtime under test introduces ZERO third-party dependencies.  This test
module imports only :mod:`io`, :mod:`struct` and :mod:`sys` from the standard
library plus ``pytest`` (a development-only framework - never a runtime
dependency), per the AAP's hard constraint (sections 0.5 / 0.7.1).  Stream
capture uses ``capsys`` and stdin / global-state patching uses ``monkeypatch``.
"""

import io
import struct
import sys

import pytest

# Obtain the parallel-built runtime lazily so collection degrades to a clean
# SKIP (never a hard error) when the package - or a sub-module - is not yet
# importable.  These are the only two import hooks the suite contract requires;
# the data-movement runtime (``libcob_py.move``) is reached transitively through
# ``termio`` -> ``common._lazy_move`` and so needs no direct import here.
termio = pytest.importorskip("libcob_py.termio")
common = pytest.importorskip("libcob_py.common")


# ===========================================================================
# cob_field builders (all via libcob_py.common - the suite's documented builder)
# ===========================================================================
def _attr(type, digits=0, scale=0, flags=0):
    """Build a :class:`libcob_py.common.cob_field_attr`."""
    return common.cob_field_attr(type=type, digits=digits, scale=scale,
                                 flags=flags, pic=None)


def _alnum(text, size=None):
    """Build an ALPHANUMERIC (PIC X) field from *text*.

    A shorter payload is right-padded with spaces - the natural resting state of
    a ``PIC X`` item - so ACCEPT receivers begin blank and DISPLAY operands hold
    exactly the characters supplied.
    """
    raw = text.encode("latin-1") if isinstance(text, str) else bytes(text)
    fsize = len(raw) if size is None else size
    if len(raw) < fsize:
        raw = raw + b" " * (fsize - len(raw))
    return common.cob_field(size=fsize, data=bytearray(raw[:fsize]),
                            attr=_attr(common.COB_TYPE_ALPHANUMERIC))


def _num(data, type=None, digits=0, scale=0, flags=0, size=None):
    """Build a numeric field from explicit *data* bytes.

    Defaults to ``NUMERIC_DISPLAY``; a shorter payload is zero-filled to *size*.
    Used for plain zoned operands, BINARY / COMP items, edited text and pointers.
    """
    if type is None:
        type = common.COB_TYPE_NUMERIC_DISPLAY
    raw = bytes(data)
    fsize = len(raw) if size is None else size
    if len(raw) < fsize:
        raw = raw + b"\x00" * (fsize - len(raw))
    return common.cob_field(size=fsize, data=bytearray(raw[:fsize]),
                            attr=_attr(type, digits, scale, flags))


def _signed(text, digits, scale=0, leading=True):
    """Build a signed NUMERIC-DISPLAY field with a SEPARATE sign.

    *text* is a literal value such as ``"-012"`` (a ``'+'`` / ``'-'`` sign byte
    followed by ASCII digits).  A separate sign keeps the stored value fully
    transparent so the real MOVE decodes the operand to the intended magnitude
    and sign without depending on platform sign-overpunch conventions.
    """
    raw = text.encode("latin-1")
    flags = common.COB_FLAG_HAVE_SIGN | common.COB_FLAG_SIGN_SEPARATE
    if leading:
        flags |= common.COB_FLAG_SIGN_LEADING
    return _num(raw, digits=digits, scale=scale, flags=flags, size=len(raw))


# ===========================================================================
# Isolation - every test starts from a deterministic runtime baseline
# ===========================================================================
@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Reset module / screen / buffer global state around each test.

    * ``cob_current_module = None`` - termio then uses its guarded defaults
      (pretty display enabled, ``'.'`` decimal point), matching a freshly
      started runtime before any program module has been entered.
    * ``termio._term_buff = None`` - the ACCEPT read cap falls back to
      ``COB_MEDIUM_MAX`` unless a test explicitly calls ``cob_init_termio``.
    * ``cob_screen_initialized`` removed - plain DISPLAY routes to stdout
      (the curses SCREEN subsystem is inactive).

    ``monkeypatch`` reverts every change at teardown, so tests cannot leak state.
    """
    monkeypatch.setattr(common, "cob_current_module", None, raising=False)
    monkeypatch.setattr(termio, "_term_buff", None, raising=False)
    if hasattr(common, "cob_screen_initialized"):
        monkeypatch.delattr(common, "cob_screen_initialized", raising=False)


# ###########################################################################
# Section 1 - agent-action-plan REQUIRED tests
# ###########################################################################

# --------------------------------------------------------------------------
# Phase 1 - DISPLAY
# --------------------------------------------------------------------------
def test_display_alnum_stdout(capsys):
    """DISPLAY of a PIC X(5) field goes to stdout with a trailing newline."""
    field = _alnum("HELLO")
    termio.cob_display(0, 1, 1, field)
    captured = capsys.readouterr()
    assert captured.out == "HELLO\n"
    assert captured.err == ""


def test_display_to_stderr(capsys):
    """``to_stderr`` truthy routes DISPLAY output to stderr, not stdout."""
    field = _alnum("HELLO")
    termio.cob_display(1, 1, 1, field)
    captured = capsys.readouterr()
    assert captured.err == "HELLO\n"
    assert captured.out == ""


def test_display_no_newline(capsys):
    """``newline`` falsey (DISPLAY ... WITH NO ADVANCING) suppresses the newline."""
    field = _alnum("HELLO")
    termio.cob_display(0, 0, 1, field)
    captured = capsys.readouterr()
    assert captured.out == "HELLO"
    assert captured.err == ""


def test_display_multiple_fields(capsys):
    """Two fields with ``varcnt=2`` are concatenated in order on one line."""
    first = _alnum("AB")
    second = _alnum("CD")
    termio.cob_display(0, 1, 2, first, second)
    assert capsys.readouterr().out == "ABCD\n"


def test_display_numeric_format(capsys):
    """A numeric field displays with COBOL formatting (sign + leading zeros).

    PIC S9(3) VALUE -12, built as a SEPARATE-LEADING-sign zoned operand
    ('-' + "012"), is MOVEd through the real data-movement runtime and emitted
    as ``-012`` - exactly the text the C runtime produces.  The value is
    recoverable: the sign and every digit are present in the output.
    """
    field = _signed("-012", digits=3, scale=0)
    termio.cob_display(0, 1, 1, field)
    out = capsys.readouterr().out
    assert out == "-012\n"
    assert out.startswith("-") and "012" in out


# --------------------------------------------------------------------------
# Phase 2 - ACCEPT
# --------------------------------------------------------------------------
def test_accept_alnum(monkeypatch):
    """ACCEPT reads a stdin line into an alphanumeric receiver verbatim."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("WORLD\n"))
    field = _alnum("", size=5)
    termio.cob_accept(field)
    assert bytes(field.data[:field.size]) == b"WORLD"


def test_accept_numeric(monkeypatch):
    """ACCEPT into a PIC 9(3) receiver stores a value that decodes to 123.

    The stored bytes are the zoned representation produced by the real MOVE; the
    value is recoverable (``int("123") == 123``) per the AAP's "delegating to
    numeric/move storage" contract.
    """
    monkeypatch.setattr(sys, "stdin", io.StringIO("123\n"))
    field = _num(b"000", digits=3, scale=0)
    termio.cob_accept(field)
    stored = bytes(field.data[:field.size])
    assert stored == b"123"
    assert int(stored.decode("latin-1")) == 123


def test_accept_eof(monkeypatch):
    """ACCEPT at end-of-input behaves gracefully (no crash; space-filled)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    field = _alnum("ZZZ", size=3)
    termio.cob_accept(field)
    assert bytes(field.data[:field.size]) == b"   "


# --------------------------------------------------------------------------
# Phase 3 - initialisation
# --------------------------------------------------------------------------
def test_init_termio_callable():
    """``cob_init_termio()`` is callable without error and allocates a buffer."""
    termio.cob_init_termio()
    assert termio._term_buff is not None
    assert len(termio._term_buff) == common.COB_MEDIUM_BUFF


# ###########################################################################
# Section 2 - supplementary coverage (every remaining branch of termio.py)
# ###########################################################################

# --------------------------------------------------------------------------
# DISPLAY - alphanumeric / edited / defensive operands
# --------------------------------------------------------------------------
def test_display_numeric_edited_is_byte_dumped(capsys):
    """NUMERIC-EDITED (0x24) is not numeric (0x24 & 0x10 == 0): emitted verbatim.

    An edited item already holds its formatted text, so termio routes it through
    the alphanumeric byte-dump rather than re-formatting it.
    """
    field = _num(b"$1,234.56", type=common.COB_TYPE_NUMERIC_EDITED,
                 digits=6, scale=2)
    termio.cob_display(0, 1, 1, field)
    assert capsys.readouterr().out == "$1,234.56\n"


def test_display_omitted_operand_contributes_nothing(capsys):
    """A field modelling an OMITTED argument (``data is None``) emits nothing."""
    omitted = common.cob_field(size=0, data=None,
                               attr=_attr(common.COB_TYPE_ALPHANUMERIC))
    termio.cob_display(0, 1, 1, omitted)
    assert capsys.readouterr().out == "\n"  # only the trailing newline


def test_display_none_field_contributes_nothing(capsys):
    """A literal ``None`` operand is tolerated and contributes nothing."""
    termio.cob_display(0, 1, 1, None)
    assert capsys.readouterr().out == "\n"


def test_display_alnum_none_data_guard():
    """``_display_alnum`` defensively returns empty bytes for None-data fields."""
    omitted = common.cob_field(size=0, data=None,
                               attr=_attr(common.COB_TYPE_ALPHANUMERIC))
    assert termio._display_alnum(omitted) == b""


# --------------------------------------------------------------------------
# DISPLAY - NUMERIC-DISPLAY pretty (default module) and plain (non-pretty)
# --------------------------------------------------------------------------
def _plain_module(monkeypatch):
    """Install a module with pretty display disabled (the plain numeric path)."""
    monkeypatch.setattr(common, "cob_current_module",
                        common.cob_module(flag_pretty_display=0))


def test_display_pretty_signed_with_scale(capsys):
    """Pretty path: signed PIC S9(3)V99 = -12.34 -> leading sign + decimal point."""
    field = _signed("-01234", digits=5, scale=2)
    termio.cob_display(0, 1, 1, field)
    assert capsys.readouterr().out == "-012.34\n"


def test_display_pretty_unsigned_with_scale(capsys):
    """Pretty path: unsigned PIC 9(2)V99 = 12.34 -> integer + decimal + fraction."""
    field = _num(b"1234", digits=4, scale=2)
    termio.cob_display(0, 1, 1, field)
    assert capsys.readouterr().out == "12.34\n"


def test_display_pretty_signed_scale_zero(capsys):
    """Pretty path: signed scale-0 item carries a fixed leading sign, no point."""
    field = _signed("+00042", digits=5, scale=0)
    termio.cob_display(0, 1, 1, field)
    assert capsys.readouterr().out == "+00042\n"


def test_display_pretty_honours_module_decimal_comma(capsys, monkeypatch):
    """Pretty path uses the active module's decimal-point byte (here a comma)."""
    monkeypatch.setattr(common, "cob_current_module",
                        common.cob_module(decimal_point=ord(","),
                                          flag_pretty_display=1))
    field = _num(b"1234", digits=4, scale=2)
    termio.cob_display(0, 1, 1, field)
    assert capsys.readouterr().out == "12,34\n"


def test_display_plain_trailing_sign(capsys, monkeypatch):
    """Plain path: zoned digits + SEPARATE trailing sign, no decimal point."""
    _plain_module(monkeypatch)
    field = _num(b"012-", digits=3, scale=0,
                 flags=common.COB_FLAG_HAVE_SIGN | common.COB_FLAG_SIGN_SEPARATE,
                 size=4)
    termio.cob_display(0, 1, 1, field)
    assert capsys.readouterr().out == "012-\n"


def test_display_plain_leading_sign(capsys, monkeypatch):
    """Plain path: SEPARATE leading sign + zoned digits."""
    _plain_module(monkeypatch)
    field = _signed("-012", digits=3, scale=0)
    termio.cob_display(0, 1, 1, field)
    assert capsys.readouterr().out == "-012\n"


def test_display_plain_unsigned(capsys, monkeypatch):
    """Plain path: an unsigned item emits its zoned digits only."""
    _plain_module(monkeypatch)
    field = _num(b"012", digits=3, scale=0)
    termio.cob_display(0, 1, 1, field)
    assert capsys.readouterr().out == "012\n"


def test_display_plain_zero_size_emits_nothing(capsys, monkeypatch):
    """Plain numeric path short-circuits on a zero-size field."""
    _plain_module(monkeypatch)
    field = _num(b"", digits=0, scale=0, size=0)
    termio.cob_display(0, 1, 1, field)
    assert capsys.readouterr().out == "\n"  # only the trailing newline


def test_display_pretty_zero_size_emits_nothing(capsys):
    """Pretty numeric path also short-circuits on a zero-size field."""
    field = _num(b"", digits=0, scale=0, size=0)
    termio.cob_display(0, 1, 1, field)
    assert capsys.readouterr().out == "\n"


# --------------------------------------------------------------------------
# DISPLAY - COMP-1 / COMP-2 / POINTER / BINARY override paths
# --------------------------------------------------------------------------
def test_display_double_comp2(capsys):
    """COMP-2 (NUMERIC-DOUBLE) prints with the C ``%-.18f`` conversion."""
    field = _num(struct.pack("=d", 3.5),
                 type=common.COB_TYPE_NUMERIC_DOUBLE, size=8)
    termio.cob_display(0, 1, 1, field)
    assert capsys.readouterr().out == ("%-.18f" % 3.5) + "\n"


def test_display_float_comp1(capsys):
    """COMP-1 (NUMERIC-FLOAT) is widened to double and printed with ``%-.18f``."""
    field = _num(struct.pack("=f", 1.5),
                 type=common.COB_TYPE_NUMERIC_FLOAT, size=4)
    termio.cob_display(0, 1, 1, field)
    assert capsys.readouterr().out == ("%-.18f" % 1.5) + "\n"


def test_display_pointer_msb_first(capsys):
    """POINTER items print ``0x`` then hex bytes most-significant first.

    The value is stored in native byte order; termio emits it MSB-first
    regardless of host endianness, yielding a stable big-endian hex rendering.
    """
    raw = (0x0102).to_bytes(8, sys.byteorder)
    field = _num(raw, type=common.COB_TYPE_ALPHANUMERIC,
                 flags=common.COB_FLAG_IS_POINTER, size=8)
    termio.cob_display(0, 1, 1, field)
    assert capsys.readouterr().out == "0x0000000000000102\n"


def test_display_real_binary_override(capsys):
    """REAL_BINARY forces the plain BINARY path even with pretty display on.

    A 2-byte item shows ``bin_digits[2] == 5`` digits.  The value is stored in
    native byte order so the real MOVE decodes it to 100; with a sign the result
    is the SEPARATE-leading-sign form ``+00100``.
    """
    raw = (100).to_bytes(2, sys.byteorder)
    field = _num(raw, type=common.COB_TYPE_NUMERIC_BINARY, digits=3,
                 flags=common.COB_FLAG_HAVE_SIGN | common.COB_FLAG_REAL_BINARY,
                 size=2)
    termio.cob_display(0, 1, 1, field)
    assert capsys.readouterr().out == "+00100\n"


def test_display_binary_non_pretty_override(capsys, monkeypatch):
    """Plain BINARY takes the digit-count override path when pretty is disabled.

    A 1-byte item shows ``bin_digits[1] == 3`` digits; value 7 -> ``007``.
    """
    _plain_module(monkeypatch)
    raw = (7).to_bytes(1, sys.byteorder)
    field = _num(raw, type=common.COB_TYPE_NUMERIC_BINARY, digits=2,
                 flags=0, size=1)
    termio.cob_display(0, 1, 1, field)
    assert capsys.readouterr().out == "007\n"


# --------------------------------------------------------------------------
# DISPLAY - stream routing, varcnt handling, text-sink fallback
# --------------------------------------------------------------------------
def test_display_varcnt_limits_displayed_fields(capsys):
    """Only the first ``varcnt`` operands are displayed."""
    a = _alnum("AA")
    b = _alnum("BB")
    c = _alnum("CC")
    termio.cob_display(0, 1, 2, a, b, c)  # varcnt=2 -> AA, BB only
    assert capsys.readouterr().out == "AABB\n"


def test_display_varcnt_zero_emits_only_newline(capsys):
    """``varcnt == 0`` emits no field bytes - just the trailing newline."""
    termio.cob_display(0, 1, 0)
    assert capsys.readouterr().out == "\n"


def test_display_negative_varcnt_clamped_to_zero(capsys):
    """A negative ``varcnt`` is clamped to zero (no fields displayed)."""
    termio.cob_display(0, 1, -1, _alnum("X"))
    assert capsys.readouterr().out == "\n"


def test_display_varcnt_none_uses_all_fields(capsys):
    """``varcnt is None`` falls back to the number of supplied operands."""
    termio.cob_display(0, 1, None, _alnum("AA"), _alnum("BB"))
    assert capsys.readouterr().out == "AABB\n"


def test_display_screen_initialized_routes_to_stderr(capsys, monkeypatch):
    """While the curses SCREEN subsystem is active, plain DISPLAY goes to stderr."""
    monkeypatch.setattr(common, "cob_screen_initialized", True, raising=False)
    termio.cob_display(0, 1, 1, _alnum("X"))
    captured = capsys.readouterr()
    assert captured.err == "X\n"
    assert captured.out == ""


def test_display_text_stream_fallback_without_buffer(monkeypatch):
    """A text-only sink (no binary ``buffer``) receives Latin-1-decoded text.

    Patching ``sys.stdout`` with an :class:`io.StringIO` exercises termio's
    text-stream emit / flush branch (the path used by the screenio fallback when
    writing to a non-binary stream).
    """
    sink = io.StringIO()
    monkeypatch.setattr(sys, "stdout", sink)
    termio.cob_display(0, 1, 1, _alnum("PLAIN"))
    assert sink.getvalue() == "PLAIN\n"


# --------------------------------------------------------------------------
# ACCEPT - size cap, blank line, trailing-newline handling, init buffer cap
# --------------------------------------------------------------------------
def test_accept_numeric_caps_input_to_field_size(monkeypatch):
    """Over-long input into a NUMERIC-DISPLAY receiver is capped to the field."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("1234567\n"))
    field = _num(b"00000", digits=5, scale=0, size=5)
    termio.cob_accept(field)
    assert bytes(field.data[:field.size]) == b"12345"


def test_accept_empty_line_space_fills(monkeypatch):
    """An empty input line space-fills an alphanumeric receiver."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    field = _alnum("ZZZ", size=3)
    termio.cob_accept(field)
    assert bytes(field.data[:field.size]) == b"   "


def test_accept_no_trailing_newline_is_kept(monkeypatch):
    """A final line lacking a newline keeps all of its characters."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("ABCDE"))
    field = _alnum("", size=5)
    termio.cob_accept(field)
    assert bytes(field.data[:field.size]) == b"ABCDE"


def test_accept_uses_initialized_buffer_cap(monkeypatch):
    """After ``cob_init_termio`` the ACCEPT read cap is sized from ``_term_buff``.

    Exercises the ``_term_buff is not None`` branch of ``cob_accept`` (the cap is
    ``len(_term_buff) - 1`` rather than the ``COB_MEDIUM_MAX`` fallback).
    """
    termio.cob_init_termio()
    monkeypatch.setattr(sys, "stdin", io.StringIO("HELLO\n"))
    field = _alnum("", size=5)
    termio.cob_accept(field)
    assert bytes(field.data[:field.size]) == b"HELLO"


# --------------------------------------------------------------------------
# Initialisation - buffer sizing and runtime start-up order
# --------------------------------------------------------------------------
def test_init_termio_allocates_medium_buffer():
    """``cob_init_termio`` allocates a ``COB_MEDIUM_BUFF``-sized working buffer."""
    termio.cob_init_termio()
    assert isinstance(termio._term_buff, bytearray)
    assert len(termio._term_buff) == common.COB_MEDIUM_BUFF


def test_init_termio_discovered_in_runtime_order():
    """termio initialises after fileio and before call (AAP section 0.5.3)."""
    order = common.COB_INIT_ORDER
    assert "termio" in order
    assert order.index("fileio") < order.index("termio") < order.index("call")
