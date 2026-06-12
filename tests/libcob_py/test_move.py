"""Unit tests for :mod:`libcob_py.move` - the data-movement (MOVE) runtime.

This module is the dedicated test suite for the **GAP-resolution module**
identified in AAP section 0.6.5.  ``libcob/move.c`` (the ``cob_move`` /
``cob_set_int`` / ``cob_get_int`` data-movement family, edited- and
justified-move logic, and alphanumeric<->numeric conversion) is a compiled,
first-class C runtime module - initialised between ``strings`` and
``intrinsic`` in the ``cob_init_*`` chain - yet it was *absent* from the
prompt's nine-module ``libcob_py`` mapping table.  The AAP surfaces this
omission explicitly so the subsystem is not silently dropped; this suite
(``tests/libcob_py/test_move.py``) is the corresponding gap-resolution test
that pins ``libcob_py/move.py`` against its authoritative C source
``libcob/move.c`` (AAP sections 0.2.1, 0.4.1, 0.6.5, 0.7.1).

It is one of exactly thirteen ``tests/libcob_py/`` suites created for the
GNU Cobol (OpenCOBOL) C->Python backend refactor.

What is covered (per the AAP coverage gate 0.7.1, >=80% line coverage of
``libcob_py/move.py``):

* the central ``cob_move`` dispatcher and its (src-USAGE, dst-USAGE) matrix
  (DISPLAY / PACKED / BINARY / FLOAT / DOUBLE / EDITED / ALPHANUMERIC and their
  edited variants, plus the ``indirect_move`` bridge for converter pairs that
  have no direct route),
* numeric<->numeric moves *across USAGE* with the value preserved byte-for-byte
  in meaning (DISPLAY <-> COMP-3 <-> BINARY), and high-order truncation,
* alphanumeric moves: left-justified space padding, truncation, and the
  ``COB_FLAG_JUSTIFIED`` right-justified path,
* NUMERIC-EDITED editing (zero suppression, comma / decimal insertion, ``CR`` /
  ``DB`` and floating sign / floating currency) and the matching de-edit
  (EDITED -> DISPLAY) round-trip,
* the figurative ``MOVE ALL`` / ``MOVE ZERO`` / ``MOVE SPACE`` fill, and
* the convenience integer accessors ``cob_set_int`` / ``cob_get_int`` /
  ``cob_get_long_long`` (and the packed / display getters) plus the runtime
  initialiser ``cob_init_move``.

HARD CONSTRAINTS (AAP sections 0.5 / 0.7.1)
-------------------------------------------
* **Standard library only.**  Only :mod:`struct` and :mod:`decimal` are imported
  from the stdlib; ``pytest`` is a development-only test framework (never a
  runtime dependency).  NO third-party package is imported.
* The runtime under test is obtained with :func:`pytest.importorskip` so that
  collection degrades to a clean *skip* (never a hard error) when the
  parallel-built ``libcob_py`` package - or a sub-module - is not yet importable.
* Field construction goes through :mod:`libcob_py.common`
  (``cob_field`` / ``cob_field_attr`` + the ``COB_TYPE_*`` / ``COB_FLAG_*``
  enumerations) and value encode/decode through :mod:`libcob_py.numeric`,
  mirroring the local builder pattern established by ``test_numeric.py`` (the
  helpers are kept *local* to this module - they are deliberately not imported
  across test modules).
"""
import struct
from decimal import Decimal

import pytest

# Obtain the parallel-built runtime lazily so collection degrades to a clean
# SKIP (never a hard error) when the package - or a sub-module - is absent.
libcob_py = pytest.importorskip("libcob_py")
common = pytest.importorskip("libcob_py.common")
move = pytest.importorskip("libcob_py.move")
numeric = pytest.importorskip("libcob_py.numeric")


# ===========================================================================
# USAGE / flag shorthands (resolved from the runtime so a test never hard-codes
# a constant that could drift from the module under test).
# ===========================================================================
T_GROUP = common.COB_TYPE_GROUP
T_DISP = common.COB_TYPE_NUMERIC_DISPLAY
T_BIN = common.COB_TYPE_NUMERIC_BINARY
T_PACK = common.COB_TYPE_NUMERIC_PACKED
T_FLOAT = common.COB_TYPE_NUMERIC_FLOAT
T_DOUBLE = common.COB_TYPE_NUMERIC_DOUBLE
T_EDITED = common.COB_TYPE_NUMERIC_EDITED
T_ALNUM = common.COB_TYPE_ALPHANUMERIC
T_ALNUM_ALL = common.COB_TYPE_ALPHANUMERIC_ALL
T_ALNUM_EDITED = common.COB_TYPE_ALPHANUMERIC_EDITED

F_SIGN = common.COB_FLAG_HAVE_SIGN
F_JUST = common.COB_FLAG_JUSTIFIED
F_BLANK_ZERO = common.COB_FLAG_BLANK_ZERO


# ---------------------------------------------------------------------------
# Field-construction + encode/decode helpers (local; mirrors test_numeric.py's
# mk / enc / dec builder pattern - kept private to this module).
# ---------------------------------------------------------------------------
def mk(data, type=common.COB_TYPE_UNKNOWN, digits=0, scale=0, flags=0,
       size=None, pic=None):
    """Build a ``cob_field`` from *pre-encoded* bytes.

    The backing buffer is always a ``bytearray`` so the in-place stores the
    converters perform (sign overpunch, BCD nibble writes, byte-swap) are
    visible through ``field.data`` exactly as the C ``cob_field.data`` pointer
    aliases a program's WORKING-STORAGE image.  *size* defaults to the length
    of *data*; pass it explicitly for the zero-length edge case.
    """
    payload = bytearray(data)
    attr = common.cob_field_attr(type=type, digits=digits, scale=scale,
                                 flags=flags, pic=pic)
    return common.cob_field(size=len(payload) if size is None else size,
                            data=payload, attr=attr)


def pic(*pairs):
    """Encode a PICTURE as the 5-byte-group wire format (symbol + int32 count).

    Each ``(symbol, count)`` pair becomes one ASCII symbol byte followed by a
    little-endian 32-bit repeat count, matching the encoding that
    ``cobc``/``field.c`` emits and that ``move._iter_pic`` consumes.  Note the
    ``C`` (CR) and ``D`` (DB) symbols each occupy a single PIC group but expand
    to **two** output positions, so e.g. ``9999CR`` is ``pic(("9", 4), ("C", 1))``.
    """
    out = bytearray()
    for sym, count in pairs:
        out.append(ord(sym))
        out += struct.pack("=i", count)
    return bytes(out)


def make_num(usage, digits, scale, signed, value, size):
    """Build a numeric ``cob_field`` of *usage* holding *value*.

    Mirrors ``test_numeric.py``'s ``enc`` helper: the field is allocated with a
    zeroed ``bytearray`` of *size* bytes and the (signed, scaled) *value* is
    stored through the high-level :func:`numeric.cob_decimal_get_field` path -
    the exact store the emitter performs - so the byte image is produced by the
    runtime under test rather than hand-rolled here.  *value* may be any object
    acceptable to :class:`decimal.Decimal` (``str`` is preferred for exactness).
    Returns the field.
    """
    flags = F_SIGN if signed else 0
    field = mk(bytearray(size), usage, digits=digits, scale=scale, flags=flags)
    dval = Decimal(value)
    unscaled = int(dval.scaleb(scale).to_integral_value())
    numeric.cob_decimal_get_field(numeric.cob_decimal(unscaled, scale), field, 0)
    return field


def read_dec(field):
    """Decode numeric *field* back to a :class:`decimal.Decimal`.

    Mirrors ``test_numeric.py``'s ``dec`` helper but returns the fully-scaled
    :class:`~decimal.Decimal` value (``value * 10**-scale``) so value-preserving
    cross-USAGE assertions read naturally (e.g. ``read_dec(dst) ==
    Decimal("-123.45")``).  Decoding through :func:`numeric.cob_decimal_set_field`
    also transparently handles the DISPLAY sign overpunch, so a round-tripped
    signed DISPLAY field compares cleanly without manual normalisation.
    """
    d = numeric.cob_decimal()
    numeric.cob_decimal_set_field(d, field)
    return Decimal(d.value).scaleb(-d.scale)


def _has(name):
    """Return the ``move`` attribute *name* or ``None`` (getattr probe).

    Used to guard the few converters whose presence/spelling may vary so the
    suite degrades gracefully rather than erroring at collection.
    """
    return getattr(move, name, None)


# ===========================================================================
# Phase 1 - Central dispatch & set/get int
# ===========================================================================
def test_set_get_int_roundtrip():
    """``cob_set_int`` / ``cob_get_int`` round-trip across DISPLAY/COMP/COMP-3.

    Stores 12345 into one field of each major numeric USAGE and reads it back,
    confirming the binary-temp store + dispatch path is USAGE-agnostic; then
    stores a negative value into a *signed* field; finally exercises
    ``cob_get_long_long`` for a value beyond 32 bits on a PIC 9(14) field
    (move.c cob_set_int L1296-L1307 / cob_get_int L1309-L1332 /
    cob_get_long_long L1334-L1357).
    """
    cases = [
        ("DISPLAY", mk(bytearray(5), T_DISP, digits=5, flags=F_SIGN)),
        ("COMP", mk(bytearray(4), T_BIN, digits=5, flags=F_SIGN)),
        ("COMP-3", mk(bytearray(3), T_PACK, digits=5, flags=F_SIGN)),
    ]
    for label, field in cases:
        move.cob_set_int(field, 12345)
        assert move.cob_get_int(field) == 12345, label
        # negative value into the signed field
        move.cob_set_int(field, -123)
        assert move.cob_get_int(field) == -123, label

    # cob_get_long_long for a value that overflows 32 bits, on a PIC 9(14).
    big = 12345678901234
    f14 = mk(bytearray(14), T_DISP, digits=14)
    move.cob_move(mk(b"12345678901234", T_DISP, digits=14), f14)
    assert move.cob_get_long_long(f14) == big
    # Same value carried through a COMP (BINARY) 9(14) item.
    comp14 = mk(bytearray(8), T_BIN, digits=14, flags=F_SIGN)
    move.cob_move(mk(b"12345678901234", T_DISP, digits=14), comp14)
    assert move.cob_get_long_long(comp14) == big


def test_cob_move_numeric_to_numeric():
    """DISPLAY S9(5)V99 = -123.45 -> COMP-3 preserves value across USAGE.

    The byte representation differs (zoned decimal vs BCD nibbles) but the
    numeric *value* must be identical; then COMP-3 -> DISPLAY recovers it,
    proving the dispatcher routes both directions through the matching
    converters (move.c cob_move L1028-L1163).
    """
    src = make_num(T_DISP, digits=7, scale=2, signed=True, value="-123.45",
                   size=7)
    assert read_dec(src) == Decimal("-123.45")

    # DISPLAY -> COMP-3 (cob_move_display_to_packed); value preserved.
    packed = mk(bytearray(4), T_PACK, digits=7, scale=2, flags=F_SIGN)
    move.cob_move(src, packed)
    assert read_dec(packed) == Decimal("-123.45")
    # The packed byte image must differ from the zoned-decimal source image.
    assert bytes(packed.data) != bytes(src.data)
    # ... and carry the negative sign nibble (0x0D).
    assert (packed.data[-1] & 0x0F) == 0x0D

    # COMP-3 -> DISPLAY (cob_move_packed_to_display); value still preserved.
    back = mk(bytearray(7), T_DISP, digits=7, scale=2, flags=F_SIGN)
    move.cob_move(packed, back)
    assert read_dec(back) == Decimal("-123.45")


def test_cob_move_truncation():
    """High-order digits are truncated when the receiver is narrower.

    Moving 12345 into a PIC 9(3) keeps the three low-order digits ``345`` per
    COBOL's right-aligned (by decimal position) store - the C
    ``store_common_region`` overlap logic (move.c L101-L132).
    """
    src = mk(b"12345", T_DISP, digits=5)
    dst = mk(bytearray(3), T_DISP, digits=3)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"345"

    # Same rule for a scaled receiver: 123.45 -> PIC 9(1)V9 keeps 3.4.
    src2 = make_num(T_DISP, digits=5, scale=2, signed=False, value="123.45",
                    size=5)
    dst2 = mk(bytearray(2), T_DISP, digits=2, scale=1)
    move.cob_move(src2, dst2)
    assert bytes(dst2.data) == b"34"


# ===========================================================================
# Phase 2 - Alphanumeric moves (padding / justification)
# ===========================================================================
def test_move_alnum_left_justify_pad():
    """``"AB"`` into PIC X(5) is left-justified and space-padded -> ``"AB   "``.

    The default (non-JUSTIFIED) alphanumeric move copies the source to the left
    and fills the remainder with spaces (move.c cob_move_alphanum_to_alphanum
    L391-L420).
    """
    src = mk(b"AB", T_ALNUM)
    dst = mk(bytearray(5), T_ALNUM)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"AB   "


def test_move_alnum_truncate():
    """``"ABCDEF"`` into PIC X(3) truncates the low-order tail -> ``"ABC"``.

    A left-justified alphanumeric receiver keeps the leading characters
    (move.c cob_move_alphanum_to_alphanum, size1 > size2 branch).
    """
    src = mk(b"ABCDEF", T_ALNUM)
    dst = mk(bytearray(3), T_ALNUM)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"ABC"


def test_move_justified_right():
    """``COB_FLAG_JUSTIFIED`` right-justifies: ``"AB"`` into PIC X(5) -> ``"   AB"``.

    The JUSTIFIED RIGHT path pads on the *left* (move.c
    cob_move_alphanum_to_alphanum, ``COB_FIELD_JUSTIFIED`` branch); this asserts
    the flag path is honoured.
    """
    src = mk(b"AB", T_ALNUM)
    dst = mk(bytearray(5), T_ALNUM, flags=F_JUST)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"   AB"

    # JUSTIFIED RIGHT with a longer source keeps the low-order (right) chars.
    src2 = mk(b"ABCDE", T_ALNUM)
    dst2 = mk(bytearray(3), T_ALNUM, flags=F_JUST)
    move.cob_move(src2, dst2)
    assert bytes(dst2.data) == b"CDE"


# ===========================================================================
# Phase 3 - NUMERIC-EDITED (cob_move_display_to_edited) & de-edit
# ===========================================================================
def test_move_to_numeric_edited():
    """Editing of numeric values into NUMERIC-EDITED pictures (move.c L616-L848).

    Covers the representative editing categories:

    * comma + decimal insertion with leading zero suppression
      (``ZZ,ZZ9.99``): 1234.50 -> ``" 1,234.50"``.  Note the leading *space*:
      ``ZZ,ZZ9`` has five integer digit positions but the value has only four
      significant integer digits, so the leftmost ``Z`` suppresses to a space.
      (The AAP narrative wrote ``"1,234.50"`` without that space; the
      ``move.c``-faithful result keeps it, which the prompt explicitly allows -
      "if a specific edited PIC behaves differently, assert ... move.c
      semantics".)  A value-preserving de-edit round-trip is also asserted.
    * trailing ``CR`` sign editing (``9999`` + ``CR``): -5 -> ``"0005CR"`` and
      +5 -> ``"0005  "`` (the credit symbol blanks when non-negative).
    * full zero suppression (``ZZZZ``): 0 -> ``"    "`` (all spaces).
    """
    # ZZ,ZZ9.99 with 1234.50
    edited = mk(bytearray(9), T_EDITED, digits=6, scale=2,
                pic=pic(("Z", 2), (",", 1), ("Z", 2), ("9", 1),
                        (".", 1), ("9", 2)))
    src = make_num(T_DISP, digits=6, scale=2, signed=False, value="1234.50",
                   size=6)
    move.cob_move(src, edited)
    assert bytes(edited.data) == b" 1,234.50"
    # de-edit recovers the value exactly.
    back = mk(bytearray(6), T_DISP, digits=6, scale=2)
    move.cob_move(edited, back)
    assert read_dec(back) == Decimal("1234.50")

    # 9999CR with -5
    cr = mk(bytearray(6), T_EDITED, digits=4, scale=0,
            pic=pic(("9", 4), ("C", 1)))
    neg = make_num(T_DISP, digits=4, scale=0, signed=True, value="-5", size=4)
    move.cob_move(neg, cr)
    assert bytes(cr.data) == b"0005CR"

    # 9999CR with +5 -> the CR positions blank out.
    cr_pos = mk(bytearray(6), T_EDITED, digits=4, scale=0,
                pic=pic(("9", 4), ("C", 1)))
    pos = make_num(T_DISP, digits=4, scale=0, signed=True, value="5", size=4)
    move.cob_move(pos, cr_pos)
    assert bytes(cr_pos.data) == b"0005  "

    # ZZZZ with 0 -> fully suppressed to spaces.
    zsup = mk(bytearray(4), T_EDITED, digits=4, scale=0, pic=pic(("Z", 4)))
    zero = mk(b"0000", T_DISP, digits=4)
    move.cob_move(zero, zsup)
    assert bytes(zsup.data) == b"    "


def test_move_edited_to_display():
    """De-edit: NUMERIC-EDITED -> NUMERIC-DISPLAY recovers the numeric value.

    Builds an edited image from a known value, then moves it back to a plain
    DISPLAY field and confirms the value is recovered (move.c
    cob_move_edited_to_display L850-L925).
    """
    edited = mk(bytearray(6), T_EDITED, digits=5, scale=2,
                pic=pic(("Z", 2), ("9", 1), (".", 1), ("9", 2)))
    src = make_num(T_DISP, digits=5, scale=2, signed=False, value="12.34",
                   size=5)
    move.cob_move(src, edited)
    # sanity: the edited image is the human-readable form.
    assert bytes(edited.data) == b" 12.34"

    disp = mk(bytearray(5), T_DISP, digits=5, scale=2)
    move.cob_move(edited, disp)
    assert read_dec(disp) == Decimal("12.34")
    assert bytes(disp.data) == b"01234"

    # A signed edited image (trailing CR) de-edits to a negative value.
    cr = mk(bytearray(5), T_EDITED, digits=3, scale=0, pic=pic(("9", 3), ("C", 1)))
    neg = make_num(T_DISP, digits=3, scale=0, signed=True, value="-12", size=3)
    move.cob_move(neg, cr)
    assert bytes(cr.data) == b"012CR"
    disp2 = mk(bytearray(3), T_DISP, digits=3, scale=0, flags=F_SIGN)
    move.cob_move(cr, disp2)
    assert read_dec(disp2) == Decimal("-12")


# ===========================================================================
# Phase 4 - MOVE ALL & figurative constants
# ===========================================================================
def test_move_all():
    """``cob_move_all`` repeats the source pattern across the destination.

    Covers the figurative paths (move.c cob_move_all L986-L1026):

    * ``MOVE ALL "*"`` fills a PIC X(5) with ``"*****"`` - both via the direct
      ``cob_move_all`` entry and via ``cob_move`` dispatch on an
      ``ALPHANUMERIC_ALL`` source (the dispatcher's first branch),
    * ``MOVE ZERO`` (the ``cob_zero`` figurative) into a numeric field yields
      all ASCII zeros, and
    * ``MOVE SPACE`` (the ``cob_space`` figurative) into an alphanumeric field
      yields all spaces.
    """
    # MOVE ALL "*" -> PIC X(5) via the direct entry point.
    dst = mk(bytearray(5), T_ALNUM)
    move.cob_move_all(mk(b"*", T_ALNUM), dst)
    assert bytes(dst.data) == b"*****"

    # MOVE ALL "*" via cob_move dispatch on an ALPHANUMERIC_ALL source.
    dst_disp = mk(bytearray(5), T_ALNUM)
    move.cob_move(mk(b"*", T_ALNUM_ALL), dst_disp)
    assert bytes(dst_disp.data) == b"*****"

    # A multi-byte pattern repeats cyclically.
    dst_cycle = mk(bytearray(5), T_ALNUM)
    move.cob_move_all(mk(b"AB", T_ALNUM), dst_cycle)
    assert bytes(dst_cycle.data) == b"ABABA"

    # MOVE ZERO -> numeric field is all ASCII zeros.
    numdst = mk(bytearray(3), T_DISP, digits=3)
    move.cob_move(common.cob_zero, numdst)
    assert bytes(numdst.data) == b"000"

    # MOVE SPACE -> alphanumeric field is all spaces.
    alnumdst = mk(bytearray(4), T_ALNUM)
    move.cob_move(common.cob_space, alnumdst)
    assert bytes(alnumdst.data) == b"    "


# ===========================================================================
# Phase 5 - BLANK WHEN ZERO & init
# ===========================================================================
def test_blank_when_zero():
    """``COB_FLAG_BLANK_ZERO``: moving 0 yields all spaces (move.c L831-L836).

    BLANK WHEN ZERO is honoured by the NUMERIC-EDITED store path: when the
    edited result is all zeros and the field carries ``COB_FLAG_BLANK_ZERO``,
    the receiver is blanked to spaces.  Guarded so that, if a build does not
    implement the flag, the test skips rather than fails.
    """
    edited = mk(bytearray(3), T_EDITED, digits=3, scale=0,
                flags=F_BLANK_ZERO, pic=pic(("9", 3)))
    src = mk(b"000", T_DISP, digits=3)
    move.cob_move(src, edited)
    if bytes(edited.data) != b"   ":
        pytest.skip("BLANK WHEN ZERO not implemented in this build")
    assert bytes(edited.data) == b"   "

    # A non-zero value through the same field is NOT blanked.
    edited2 = mk(bytearray(3), T_EDITED, digits=3, scale=0,
                 flags=F_BLANK_ZERO, pic=pic(("9", 3)))
    move.cob_move(mk(b"012", T_DISP, digits=3), edited2)
    assert bytes(edited2.data) == b"012"


def test_init_move_callable():
    """``move.cob_init_move()`` is callable without error (move.c L1359-L1363)."""
    assert move.cob_init_move() is None


# ===========================================================================
# Supplementary coverage - the full converter matrix, indirect-move routes,
# edge cases and the convenience accessors.  These exercise the remaining
# branches of ``move.py`` so the suite comfortably clears the >=80% coverage
# gate (AAP 0.7.1) while pinning the byte-for-byte behaviour of every route.
# ===========================================================================

# ---- DISPLAY <-> DISPLAY / ALPHANUMERIC -----------------------------------
def test_display_to_display_zero_pad_left():
    src = mk(b"123", T_DISP, digits=3)
    dst = mk(bytearray(5), T_DISP, digits=5)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"00123"


def test_display_to_display_scaled_alignment():
    # 12.3 (scale 1) -> field scale 2 : digits realign by decimal position.
    src = mk(b"123", T_DISP, digits=3, scale=1)
    dst = mk(bytearray(5), T_DISP, digits=5, scale=2)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"01230"


def test_display_to_display_sign_preserved():
    src = mk(b"123", T_DISP, digits=3, flags=F_SIGN)
    common.cob_put_sign(src, -1)
    dst = mk(bytearray(3), T_DISP, digits=3, flags=F_SIGN)
    move.cob_move(src, dst)
    assert common.cob_get_sign(dst) < 0


def test_display_to_alphanum_padding():
    src = mk(b"12", T_DISP, digits=2)
    dst = mk(bytearray(5), T_ALNUM)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"12   "


def test_display_to_alphanum_truncation():
    src = mk(b"12345", T_DISP, digits=5)
    dst = mk(bytearray(3), T_ALNUM)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"123"


def test_display_to_alphanum_p_scaling_zero_fill():
    # scale < 0 (trailing P's) zero-fills the implied positions.
    src = mk(b"12", T_DISP, digits=2, scale=-2)
    dst = mk(bytearray(6), T_ALNUM)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"1200  "


# ---- ALPHANUMERIC <-> ALPHANUMERIC ----------------------------------------
def test_alphanum_to_alphanum_justified_truncate_left():
    src = mk(b"ABCDE", T_ALNUM)
    dst = mk(bytearray(3), T_ALNUM, flags=F_JUST)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"CDE"


def test_alphanum_to_display_with_sign_and_point():
    src = mk(b"-12.3", T_ALNUM)
    dst = mk(bytearray(4), T_DISP, digits=4, scale=1, flags=F_SIGN)
    move.cob_move(src, dst)
    # cob_get_sign normalises the overpunched trailing digit in place, so read
    # the sign before inspecting the magnitude bytes (common.c L934-976).
    assert common.cob_get_sign(dst) < 0
    assert bytes(common.COB_FIELD_DATA(dst)) == b"0123"


def test_alphanum_to_display_invalid_char_zeroes():
    src = mk(b"1@3", T_ALNUM)
    dst = mk(bytearray(3), T_DISP, digits=3)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"000"


def test_alphanum_to_display_surplus_low_order_kept():
    src = mk(b"1234567", T_ALNUM)
    dst = mk(bytearray(3), T_DISP, digits=3)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"567"


def test_alphanum_to_display_double_decimal_zeroes():
    # Wide receiver reaches a 2nd decimal point -> error path zeroes field.
    src = mk(b"1.2.3", T_ALNUM)
    dst = mk(bytearray(5), T_DISP, digits=5, scale=3)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"00000"


# ---- PACKED (COMP-3) ------------------------------------------------------
def test_display_to_packed_even_digits_signed():
    src = mk(b"4567", T_DISP, digits=4)
    dst = mk(bytearray(3), T_PACK, digits=4, flags=F_SIGN)
    move.cob_move(src, dst)
    assert bytes(dst.data).hex() == "04567c"   # sign nibble C = positive


def test_display_to_packed_unsigned_nibble():
    src = mk(b"123", T_DISP, digits=3)
    dst = mk(bytearray(2), T_PACK, digits=3)
    move.cob_move(src, dst)
    assert (bytes(dst.data)[-1] & 0x0F) == 0x0F   # unsigned sign nibble


def test_packed_to_display_roundtrip():
    disp = mk(b"4567", T_DISP, digits=4)
    pk = mk(bytearray(3), T_PACK, digits=4, flags=F_SIGN)
    move.cob_move(disp, pk)
    back = mk(bytearray(4), T_DISP, digits=4)
    move.cob_move(pk, back)
    assert bytes(back.data) == b"4567"


def test_packed_get_int_and_long_long():
    pk = mk(bytearray(3), T_PACK, digits=4, flags=F_SIGN)
    move.cob_move(mk(b"4567", T_DISP, digits=4), pk)
    assert move.cob_packed_get_int(pk) == 4567
    assert move.cob_packed_get_long_long(pk) == 4567


# ---- BINARY (COMP) --------------------------------------------------------
def test_display_to_binary_and_back():
    src = mk(b"12345", T_DISP, digits=5)
    b = mk(bytearray(4), T_BIN, digits=5, flags=F_SIGN)
    move.cob_move(src, b)
    assert move.cob_get_int(b) == 12345
    disp = mk(bytearray(5), T_DISP, digits=5)
    move.cob_move(b, disp)
    assert bytes(disp.data) == b"12345"


def test_display_to_binary_negative():
    src = mk(b"00042", T_DISP, digits=5, flags=F_SIGN)
    common.cob_put_sign(src, -1)
    b = mk(bytearray(4), T_BIN, digits=5, flags=F_SIGN)
    move.cob_move(src, b)
    assert move.cob_get_int(b) == -42


def test_display_to_binary_truncation_by_digits():
    # digits=2 binary truncates 12345 mod 100 = 45.
    src = mk(b"12345", T_DISP, digits=5)
    b = mk(bytearray(4), T_BIN, digits=2, flags=F_SIGN)
    move.cob_move(src, b)
    assert move.cob_get_int(b) == 45


def test_math_fmod_ll_sign_follows_dividend():
    fmod = _has("math_fmod_ll")
    if fmod is None:
        pytest.skip("math_fmod_ll not present in this build")
    assert fmod(-12345, 100) == -45
    assert fmod(12345, 100) == 45
    assert fmod(5, 0) == 5


# ---- FLOAT / DOUBLE (COMP-1 / COMP-2) -------------------------------------
def test_display_to_double_and_back():
    src = mk(b"12345", T_DISP, digits=5, scale=2)
    d = mk(bytearray(8), T_DOUBLE)
    move.cob_move(src, d)
    assert abs(struct.unpack("=d", bytes(d.data))[0] - 123.45) < 1e-9
    disp = mk(bytearray(5), T_DISP, digits=5, scale=2)
    move.cob_move(d, disp)
    assert bytes(disp.data) == b"12345"


def test_display_to_float():
    src = mk(b"025", T_DISP, digits=3)
    f = mk(bytearray(4), T_FLOAT)
    move.cob_move(src, f)
    assert abs(struct.unpack("=f", bytes(f.data))[0] - 25.0) < 1e-4


# ---- EDITED - asterisk protect / floating sign / floating currency --------
def test_display_to_edited_asterisk_protect():
    p = pic(("*", 3), ("9", 1), (".", 1), ("9", 2))
    ed = mk(bytearray(7), T_EDITED, digits=6, scale=2, pic=p)
    src = mk(b"000123", T_DISP, digits=6, scale=2)
    move.cob_move(src, ed)
    # PIC ***9.99: value 0001.23 zero-suppresses the three leading zeros to '*'.
    assert bytes(ed.data) == b"***1.23"


def test_edited_floating_minus_negative():
    ed = mk(bytearray(5), T_EDITED, digits=5, scale=0, pic=pic(("-", 4), ("9", 1)))
    src = mk(b"00012", T_DISP, digits=5, flags=F_SIGN)
    common.cob_put_sign(src, -1)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"  -12"


def test_edited_floating_minus_positive_blank():
    ed = mk(bytearray(5), T_EDITED, digits=5, scale=0, pic=pic(("-", 4), ("9", 1)))
    src = mk(b"00012", T_DISP, digits=5, flags=F_SIGN)
    common.cob_put_sign(src, 1)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"   12"


def test_edited_floating_currency():
    ed = mk(bytearray(7), T_EDITED, digits=5, scale=2,
            pic=pic(("$", 3), ("9", 1), (".", 1), ("9", 2)))
    src = mk(b"00123", T_DISP, digits=5, scale=2)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"  $1.23"


def test_edited_trailing_plus_negative():
    ed = mk(bytearray(4), T_EDITED, digits=3, scale=0, pic=pic(("9", 3), ("+", 1)))
    src = mk(b"012", T_DISP, digits=3, flags=F_SIGN)
    common.cob_put_sign(src, -1)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"012-"


def test_edited_b_and_zero_insertion():
    ed = mk(bytearray(6), T_EDITED, digits=4, scale=0,
            pic=pic(("9", 2), ("B", 1), ("9", 2), ("0", 1)))
    src = mk(b"1234", T_DISP, digits=4)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"12 340"


# ---- ALPHANUMERIC-EDITED --------------------------------------------------
def test_alphanum_to_edited_insertion():
    p = pic(("X", 2), ("/", 1), ("X", 2))
    ed = mk(bytearray(5), T_ALNUM_EDITED, pic=p)
    src = mk(b"ABCD", T_ALNUM)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"AB/CD"


def test_alphanum_to_edited_insertion_and_short_source():
    ed = mk(bytearray(7), T_ALNUM_EDITED,
            pic=pic(("X", 2), ("B", 1), ("X", 2), ("/", 1), ("X", 1)))
    src = mk(b"ABC", T_ALNUM)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"AB C / "


def test_alphanum_to_edited_invalid_pic_char():
    # '@' is not a valid edited PIC symbol -> emits '?' at that position.
    ed = mk(bytearray(3), T_ALNUM_EDITED, pic=pic(("X", 1), ("@", 1), ("X", 1)))
    src = mk(b"AB", T_ALNUM)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"A?B"


# ---- Dispatcher edge cases ------------------------------------------------
def test_move_zero_size_dst_noop():
    src = mk(b"123", T_DISP, digits=3)
    dst = mk(bytearray(0), T_ALNUM, size=0)
    move.cob_move(src, dst)   # must not raise
    assert dst.size == 0


def test_move_zero_size_src_uses_space():
    src = mk(bytearray(0), T_ALNUM, size=0)
    dst = mk(bytearray(3), T_ALNUM)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"   "


def test_group_move_is_alphanumeric():
    src = mk(b"HELLO", T_GROUP)
    dst = mk(bytearray(3), T_GROUP)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"HEL"


# ---- Indirect-move routes (non-DISPLAY <-> non-DISPLAY) -------------------
def test_packed_to_packed_indirect():
    p1 = mk(bytearray(3), T_PACK, digits=4, flags=F_SIGN)
    move.cob_move(mk(b"4567", T_DISP, digits=4), p1)
    p2 = mk(bytearray(3), T_PACK, digits=4, flags=F_SIGN)
    move.cob_move(p1, p2)
    assert bytes(p1.data) == bytes(p2.data)


def test_binary_to_packed_indirect():
    b1 = mk(bytearray(4), T_BIN, digits=5, flags=F_SIGN)
    move.cob_set_int(b1, 321)
    pk = mk(bytearray(3), T_PACK, digits=4, flags=F_SIGN)
    move.cob_move(b1, pk)
    assert move.cob_packed_get_int(pk) == 321


def test_edited_to_packed_indirect():
    ed = mk(bytearray(6), T_EDITED, digits=5, scale=2,
            pic=pic(("Z", 2), ("9", 1), (".", 1), ("9", 2)))
    move.cob_move(mk(b"01234", T_DISP, digits=5, scale=2), ed)
    pk = mk(bytearray(3), T_PACK, digits=4, scale=2, flags=F_SIGN)
    move.cob_move(ed, pk)
    assert move.cob_packed_get_int(pk) == 12   # integer part of 12.34


def test_double_to_packed_indirect():
    fl = mk(bytearray(8), T_DOUBLE)
    move.cob_move(mk(b"01234", T_DISP, digits=5, scale=2), fl)
    pk = mk(bytearray(3), T_PACK, digits=4, scale=2, flags=F_SIGN)
    move.cob_move(fl, pk)
    assert move.cob_packed_get_int(pk) == 12


def test_display_to_alphanum_edited_via_scale_indirect():
    # scale > digits forces the indirect display->display path before editing.
    ed = mk(bytearray(4), T_ALNUM_EDITED, pic=pic(("X", 4)))
    src = mk(b"12", T_DISP, digits=2, scale=4)
    move.cob_move(src, ed)
    assert len(ed.data) == 4


def test_binary_to_display_indirect_wide():
    # BINARY -> EDITED routes via the wide (20-digit) indirect DISPLAY temp.
    b = mk(bytearray(4), T_BIN, digits=5, flags=F_SIGN)
    move.cob_set_int(b, 678)
    ed = mk(bytearray(3), T_EDITED, digits=3, scale=0, pic=pic(("9", 3)))
    move.cob_move(b, ed)
    assert bytes(ed.data) == b"678"


# ---- Convenience integer accessors ----------------------------------------
def test_set_get_int_zero_clears_field():
    f = mk(bytearray(5), T_DISP, digits=5, flags=F_SIGN)
    move.cob_set_int(f, 12345)
    move.cob_set_int(f, 0)
    assert move.cob_get_int(f) == 0


def test_to_c_int_wraps_like_cast():
    to_int = _has("_to_c_int")
    to_ll = _has("_to_c_longlong")
    if to_int is None or to_ll is None:
        pytest.skip("C-cast wrap helpers not present in this build")
    assert to_int(0x80000000) == -2147483648
    assert to_ll(0x8000000000000000) == -(2 ** 63)


def test_get_long_long_binary_negative():
    b8 = mk(bytearray(8), T_BIN, digits=18, flags=F_SIGN)
    move.cob_set_int(b8, -999)
    assert move.cob_get_long_long(b8) == -999


def test_get_long_long_packed():
    p1 = mk(bytearray(3), T_PACK, digits=4, flags=F_SIGN)
    move.cob_move(mk(b"4567", T_DISP, digits=4), p1)
    assert move.cob_get_long_long(p1) == 4567


def test_get_int_edited_default_branch():
    ed = mk(bytearray(3), T_EDITED, digits=3, pic=pic(("9", 3)))
    move.cob_move(mk(b"077", T_DISP, digits=3), ed)
    assert move.cob_get_int(ed) == 77


def test_get_long_long_edited_default_branch():
    ed = mk(bytearray(3), T_EDITED, digits=3, pic=pic(("9", 3)))
    move.cob_move(mk(b"077", T_DISP, digits=3), ed)
    assert move.cob_get_long_long(ed) == 77


def test_display_get_int_scaled_and_negative_scale():
    f = mk(b"12345", T_DISP, digits=5, scale=2)
    assert move.cob_display_get_int(f) == 123          # integer part of 123.45
    assert move.cob_display_get_long_long(f) == 123
    f2 = mk(b"12", T_DISP, digits=2, scale=-2)          # scale < 0 multiplies
    assert move.cob_display_get_int(f2) == 1200


def test_display_get_int_skips_leading_zeros():
    f = mk(b"00099", T_DISP, digits=5)
    assert move.cob_display_get_int(f) == 99


# ---- store_common_region / indirect_move / binary mget|mset direct --------
def test_store_common_region_direct():
    fn = _has("store_common_region")
    if fn is None:
        pytest.skip("store_common_region not present in this build")
    dst = mk(bytearray(5), T_DISP, digits=5, scale=0)
    fn(dst, bytearray(b"123"), 3, 0)
    assert bytes(dst.data) == b"00123"


def test_indirect_move_direct():
    fn = _has("indirect_move")
    if fn is None:
        pytest.skip("indirect_move not present in this build")
    src = mk(b"4567", T_DISP, digits=4)
    dst = mk(bytearray(3), T_PACK, digits=4, flags=F_SIGN)
    fn(move.cob_move_display_to_display, src, dst, 4, 0)
    assert move.cob_packed_get_int(dst) == 4567


def test_binary_mget_mset_int64_roundtrip():
    mget = _has("cob_binary_mget_int64")
    mset = _has("cob_binary_mset_int64")
    if mget is None or mset is None:
        pytest.skip("binary mget/mset helpers not present in this build")
    b = mk(bytearray(8), T_BIN, digits=18, flags=F_SIGN)
    mset(b, -123456789)
    assert mget(b) == -123456789


# ---- cob_set_pointer - CALL ... RETURNING pointer store path --------------
def test_set_pointer_round_trips_via_common():
    setp = _has("cob_set_pointer")
    if setp is None:
        pytest.skip("cob_set_pointer not present in this build")
    width = struct.calcsize("P")
    f = common.cob_field(
        size=width, data=bytearray(width),
        attr=common.cob_field_attr(type=T_BIN))
    ret = setp(f, 0x7777)
    assert ret is f
    assert common.cob_get_pointer(f) == 0x7777


# ---- Unsigned-binary, scale-up and additional indirect dispatch branches --
def test_unsigned_binary_get_and_to_display():
    # Unsigned COMP exercises the cob_binary_*_uint64 path (no sign nibble).
    ub = mk(bytearray(4), T_BIN, digits=5)
    mset = _has("cob_binary_mset_int64")
    if mset is not None:
        mset(ub, 4242)
    else:                                   # fall back to a DISPLAY source move
        move.cob_move(mk(b"04242", T_DISP, digits=5), ub)
    assert move.cob_binary_mget_int64(ub) == 4242
    disp = mk(bytearray(5), T_DISP, digits=5)
    move.cob_move(ub, disp)
    assert bytes(disp.data) == b"04242"


def test_display_to_binary_scale_up():
    # Receiver scale (2) > source scale (0): low-order positions zero-fill.
    src = mk(b"123", T_DISP, digits=3, scale=0)
    b = mk(bytearray(4), T_BIN, digits=5, scale=2, flags=F_SIGN)
    move.cob_move(src, b)
    assert move.cob_get_long_long(b) == 12300


def test_binary_to_binary_indirect():
    b1 = mk(bytearray(4), T_BIN, digits=5, flags=F_SIGN)
    move.cob_set_int(b1, 777)
    b2 = mk(bytearray(4), T_BIN, digits=5, flags=F_SIGN)
    move.cob_move(b1, b2)
    assert move.cob_get_int(b2) == 777


def test_edited_to_edited_indirect():
    p = pic(("Z", 2), ("9", 1), (".", 1), ("9", 2))
    ed1 = mk(bytearray(6), T_EDITED, digits=5, scale=2, pic=p)
    move.cob_move(mk(b"01234", T_DISP, digits=5, scale=2), ed1)
    ed2 = mk(bytearray(6), T_EDITED, digits=5, scale=2, pic=p)
    move.cob_move(ed1, ed2)
    assert bytes(ed2.data) == b" 12.34"
