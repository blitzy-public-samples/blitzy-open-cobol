"""Unit tests for :mod:`libcob_py.move` - the data-movement (MOVE) runtime.

Exercises the pure-Python port of ``libcob/move.c`` (the GAP-resolution module,
AAP section 0.6.5).  Per the coverage gate (AAP section 0.7.1, >=80% line
coverage) the suite covers every converter in the ``cob_move`` dispatch matrix
(DISPLAY / PACKED / BINARY / FLOAT / DOUBLE / EDITED / ALPHANUMERIC and their
edited variants), the figurative ALPHANUMERIC-ALL fill, the convenience integer
accessors (``cob_set_int`` / ``cob_get_int`` / ``cob_get_long_long`` and the
packed/display getters), and the runtime initialiser.

Standard library only - the runtime under test introduces ZERO third-party
dependencies; ``pytest`` is a development-only test framework (AAP 0.5 / 0.7.1).
"""
import struct

import pytest

libcob_py = pytest.importorskip("libcob_py")
common = pytest.importorskip("libcob_py.common")
move = pytest.importorskip("libcob_py.move")


def mk(data, type=common.COB_TYPE_UNKNOWN, digits=0, scale=0, flags=0,
       size=None, pic=None):
    """Build a cob_field from pre-encoded bytes."""
    payload = bytearray(data)
    attr = common.cob_field_attr(type=type, digits=digits, scale=scale,
                                 flags=flags, pic=pic)
    return common.cob_field(size=len(payload) if size is None else size,
                            data=payload, attr=attr)


def pic(*pairs):
    """Encode a PICTURE as the 5-byte-group wire format (symbol + int32 count)."""
    out = bytearray()
    for sym, count in pairs:
        out.append(ord(sym))
        out += struct.pack("=i", count)
    return bytes(out)


HAVE_SIGN = common.COB_FLAG_HAVE_SIGN


# ---------------------------------------------------------------------------
# DISPLAY <-> DISPLAY / ALPHANUMERIC
# ---------------------------------------------------------------------------
def test_display_to_display_right_justify():
    src = mk(b"123", common.COB_TYPE_NUMERIC_DISPLAY, digits=3)
    dst = mk(bytearray(5), common.COB_TYPE_NUMERIC_DISPLAY, digits=5)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"00123"


def test_display_to_display_scaled_alignment():
    # 12.3 (scale 1) -> field scale 2 : digits realign by decimal position.
    src = mk(b"123", common.COB_TYPE_NUMERIC_DISPLAY, digits=3, scale=1)
    dst = mk(bytearray(5), common.COB_TYPE_NUMERIC_DISPLAY, digits=5, scale=2)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"01230"


def test_display_to_display_sign_preserved():
    src = mk(b"123", common.COB_TYPE_NUMERIC_DISPLAY, digits=3, flags=HAVE_SIGN)
    common.cob_put_sign(src, -1)
    dst = mk(bytearray(3), common.COB_TYPE_NUMERIC_DISPLAY, digits=3,
             flags=HAVE_SIGN)
    move.cob_move(src, dst)
    assert common.cob_get_sign(dst) < 0


def test_display_to_alphanum_padding():
    src = mk(b"12", common.COB_TYPE_NUMERIC_DISPLAY, digits=2)
    dst = mk(bytearray(5), common.COB_TYPE_ALPHANUMERIC)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"12   "


def test_display_to_alphanum_truncation():
    src = mk(b"12345", common.COB_TYPE_NUMERIC_DISPLAY, digits=5)
    dst = mk(bytearray(3), common.COB_TYPE_ALPHANUMERIC)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"123"


def test_display_to_alphanum_p_scaling_zero_fill():
    # scale < 0 (trailing P's) zero-fills the implied positions.
    src = mk(b"12", common.COB_TYPE_NUMERIC_DISPLAY, digits=2, scale=-2)
    dst = mk(bytearray(6), common.COB_TYPE_ALPHANUMERIC)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"1200  "


# ---------------------------------------------------------------------------
# ALPHANUMERIC <-> ALPHANUMERIC
# ---------------------------------------------------------------------------
def test_alphanum_to_alphanum_pad():
    src = mk(b"AB", common.COB_TYPE_ALPHANUMERIC)
    dst = mk(bytearray(5), common.COB_TYPE_ALPHANUMERIC)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"AB   "


def test_alphanum_to_alphanum_justified_right():
    src = mk(b"AB", common.COB_TYPE_ALPHANUMERIC)
    dst = mk(bytearray(5), common.COB_TYPE_ALPHANUMERIC,
             flags=common.COB_FLAG_JUSTIFIED)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"   AB"


def test_alphanum_to_alphanum_justified_truncate_left():
    src = mk(b"ABCDE", common.COB_TYPE_ALPHANUMERIC)
    dst = mk(bytearray(3), common.COB_TYPE_ALPHANUMERIC,
             flags=common.COB_FLAG_JUSTIFIED)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"CDE"


def test_alphanum_to_display_with_sign_and_point():
    src = mk(b"-12.3", common.COB_TYPE_ALPHANUMERIC)
    dst = mk(bytearray(4), common.COB_TYPE_NUMERIC_DISPLAY, digits=4, scale=1,
             flags=HAVE_SIGN)
    move.cob_move(src, dst)
    # cob_get_sign reads the sign AND normalises the overpunched trailing
    # digit back to a plain digit in place (common.c L934-976), so check it
    # before inspecting the magnitude bytes.
    assert common.cob_get_sign(dst) < 0
    assert bytes(common.COB_FIELD_DATA(dst)) == b"0123"


def test_alphanum_to_display_invalid_char_zeroes():
    src = mk(b"1@3", common.COB_TYPE_ALPHANUMERIC)
    dst = mk(bytearray(3), common.COB_TYPE_NUMERIC_DISPLAY, digits=3)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"000"


# ---------------------------------------------------------------------------
# PACKED (COMP-3)
# ---------------------------------------------------------------------------
def test_display_to_packed_even_digits_signed():
    src = mk(b"4567", common.COB_TYPE_NUMERIC_DISPLAY, digits=4)
    dst = mk(bytearray(3), common.COB_TYPE_NUMERIC_PACKED, digits=4,
             flags=HAVE_SIGN)
    move.cob_move(src, dst)
    assert bytes(dst.data).hex() == "04567c"   # sign nibble C = positive


def test_display_to_packed_negative():
    src = mk(b"4567", common.COB_TYPE_NUMERIC_DISPLAY, digits=4, flags=HAVE_SIGN)
    common.cob_put_sign(src, -1)
    dst = mk(bytearray(3), common.COB_TYPE_NUMERIC_PACKED, digits=4,
             flags=HAVE_SIGN)
    move.cob_move(src, dst)
    assert bytes(dst.data)[-1] & 0x0F == 0x0D   # negative sign nibble


def test_display_to_packed_unsigned():
    src = mk(b"123", common.COB_TYPE_NUMERIC_DISPLAY, digits=3)
    dst = mk(bytearray(2), common.COB_TYPE_NUMERIC_PACKED, digits=3)
    move.cob_move(src, dst)
    assert bytes(dst.data)[-1] & 0x0F == 0x0F   # unsigned sign nibble


def test_packed_to_display_roundtrip():
    disp = mk(b"4567", common.COB_TYPE_NUMERIC_DISPLAY, digits=4)
    pk = mk(bytearray(3), common.COB_TYPE_NUMERIC_PACKED, digits=4,
            flags=HAVE_SIGN)
    move.cob_move(disp, pk)
    back = mk(bytearray(4), common.COB_TYPE_NUMERIC_DISPLAY, digits=4)
    move.cob_move(pk, back)
    assert bytes(back.data) == b"4567"


def test_packed_get_int():
    pk = mk(bytearray(3), common.COB_TYPE_NUMERIC_PACKED, digits=4,
            flags=HAVE_SIGN)
    move.cob_move(mk(b"4567", common.COB_TYPE_NUMERIC_DISPLAY, digits=4), pk)
    assert move.cob_packed_get_int(pk) == 4567
    assert move.cob_packed_get_long_long(pk) == 4567


# ---------------------------------------------------------------------------
# BINARY (COMP)
# ---------------------------------------------------------------------------
def test_display_to_binary_and_back():
    src = mk(b"12345", common.COB_TYPE_NUMERIC_DISPLAY, digits=5)
    b = mk(bytearray(4), common.COB_TYPE_NUMERIC_BINARY, digits=5,
           flags=HAVE_SIGN)
    move.cob_move(src, b)
    assert move.cob_get_int(b) == 12345
    disp = mk(bytearray(5), common.COB_TYPE_NUMERIC_DISPLAY, digits=5)
    move.cob_move(b, disp)
    assert bytes(disp.data) == b"12345"


def test_display_to_binary_negative():
    src = mk(b"00042", common.COB_TYPE_NUMERIC_DISPLAY, digits=5, flags=HAVE_SIGN)
    common.cob_put_sign(src, -1)
    b = mk(bytearray(4), common.COB_TYPE_NUMERIC_BINARY, digits=5,
           flags=HAVE_SIGN)
    move.cob_move(src, b)
    assert move.cob_get_int(b) == -42


def test_display_to_binary_truncation_by_digits():
    # digits=2 binary truncates 12345 mod 100 = 45.
    src = mk(b"12345", common.COB_TYPE_NUMERIC_DISPLAY, digits=5)
    b = mk(bytearray(4), common.COB_TYPE_NUMERIC_BINARY, digits=2,
           flags=HAVE_SIGN)
    move.cob_move(src, b)
    assert move.cob_get_int(b) == 45


def test_math_fmod_ll_sign_follows_dividend():
    assert move.math_fmod_ll(-12345, 100) == -45
    assert move.math_fmod_ll(12345, 100) == 45
    assert move.math_fmod_ll(5, 0) == 5


# ---------------------------------------------------------------------------
# FLOAT / DOUBLE (COMP-1 / COMP-2)
# ---------------------------------------------------------------------------
def test_display_to_double_and_back():
    src = mk(b"12345", common.COB_TYPE_NUMERIC_DISPLAY, digits=5, scale=2)
    d = mk(bytearray(8), common.COB_TYPE_NUMERIC_DOUBLE)
    move.cob_move(src, d)
    assert abs(struct.unpack("=d", bytes(d.data))[0] - 123.45) < 1e-9
    disp = mk(bytearray(5), common.COB_TYPE_NUMERIC_DISPLAY, digits=5, scale=2)
    move.cob_move(d, disp)
    assert bytes(disp.data) == b"12345"


def test_display_to_float():
    src = mk(b"025", common.COB_TYPE_NUMERIC_DISPLAY, digits=3)
    f = mk(bytearray(4), common.COB_TYPE_NUMERIC_FLOAT)
    move.cob_move(src, f)
    assert abs(struct.unpack("=f", bytes(f.data))[0] - 25.0) < 1e-4


# ---------------------------------------------------------------------------
# EDITED
# ---------------------------------------------------------------------------
def test_display_to_edited_zero_suppression():
    p = pic(("Z", 2), ("9", 1), (".", 1), ("9", 2))
    ed = mk(bytearray(6), common.COB_TYPE_NUMERIC_EDITED, digits=5, scale=2,
            pic=p)
    src = mk(b"01234", common.COB_TYPE_NUMERIC_DISPLAY, digits=5, scale=2)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b" 12.34"


def test_display_to_edited_asterisk_protect():
    p = pic(("*", 3), ("9", 1), (".", 1), ("9", 2))
    ed = mk(bytearray(7), common.COB_TYPE_NUMERIC_EDITED, digits=6, scale=2,
            pic=p)
    src = mk(b"000123", common.COB_TYPE_NUMERIC_DISPLAY, digits=6, scale=2)
    move.cob_move(src, ed)
    # PIC ***9.99 is 7 positions; value 0001.23 zero-suppresses the three
    # leading zeros to '*' giving "***1.23".
    assert bytes(ed.data) == b"***1.23"


def test_display_to_edited_cr_when_negative():
    p = pic(("9", 3), ("C", 1))
    ed = mk(bytearray(5), common.COB_TYPE_NUMERIC_EDITED, digits=3, scale=0,
            pic=p)
    src = mk(b"012", common.COB_TYPE_NUMERIC_DISPLAY, digits=3, flags=HAVE_SIGN)
    common.cob_put_sign(src, -1)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"012CR"


def test_display_to_edited_blank_when_zero():
    p = pic(("9", 3))
    ed = mk(bytearray(3), common.COB_TYPE_NUMERIC_EDITED, digits=3, scale=0,
            flags=common.COB_FLAG_BLANK_ZERO, pic=p)
    src = mk(b"000", common.COB_TYPE_NUMERIC_DISPLAY, digits=3)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"   "


def test_edited_to_display_roundtrip():
    p = pic(("Z", 2), ("9", 1), (".", 1), ("9", 2))
    ed = mk(bytearray(6), common.COB_TYPE_NUMERIC_EDITED, digits=5, scale=2,
            pic=p)
    move.cob_move(mk(b"01234", common.COB_TYPE_NUMERIC_DISPLAY, digits=5,
                     scale=2), ed)
    disp = mk(bytearray(5), common.COB_TYPE_NUMERIC_DISPLAY, digits=5, scale=2)
    move.cob_move(ed, disp)
    assert bytes(disp.data) == b"01234"


def test_alphanum_to_edited_insertion():
    p = pic(("X", 2), ("/", 1), ("X", 2))
    ed = mk(bytearray(5), common.COB_TYPE_ALPHANUMERIC_EDITED, pic=p)
    src = mk(b"ABCD", common.COB_TYPE_ALPHANUMERIC)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"AB/CD"


# ---------------------------------------------------------------------------
# Figurative ALPHANUMERIC-ALL + dispatcher edge cases
# ---------------------------------------------------------------------------
def test_move_all_fill_alphanumeric():
    src = common.cob_space          # ALPHANUMERIC_ALL, single byte ' '
    dst = mk(bytearray(4), common.COB_TYPE_ALPHANUMERIC)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"    "


def test_move_all_fill_numeric():
    src = common.cob_zero           # ALPHANUMERIC_ALL '0'
    dst = mk(bytearray(3), common.COB_TYPE_NUMERIC_DISPLAY, digits=3)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"000"


def test_move_zero_size_dst_noop():
    src = mk(b"123", common.COB_TYPE_NUMERIC_DISPLAY, digits=3)
    dst = mk(bytearray(0), common.COB_TYPE_ALPHANUMERIC, size=0)
    move.cob_move(src, dst)   # must not raise
    assert dst.size == 0


def test_move_zero_size_src_uses_space():
    src = mk(bytearray(0), common.COB_TYPE_ALPHANUMERIC, size=0)
    dst = mk(bytearray(3), common.COB_TYPE_ALPHANUMERIC)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"   "


def test_group_move_is_alphanumeric():
    src = mk(b"HELLO", common.COB_TYPE_GROUP)
    dst = mk(bytearray(3), common.COB_TYPE_GROUP)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"HEL"


# ---------------------------------------------------------------------------
# Convenience integer accessors
# ---------------------------------------------------------------------------
def test_set_get_int_display():
    f = mk(bytearray(5), common.COB_TYPE_NUMERIC_DISPLAY, digits=5,
           flags=HAVE_SIGN)
    move.cob_set_int(f, 42)
    assert move.cob_get_int(f) == 42
    move.cob_set_int(f, -7)
    assert move.cob_get_int(f) == -7


def test_set_get_int_packed():
    f = mk(bytearray(3), common.COB_TYPE_NUMERIC_PACKED, digits=5,
           flags=HAVE_SIGN)
    move.cob_set_int(f, 999)
    assert move.cob_get_int(f) == 999


def test_get_int_wraps_to_c_int():
    # _to_c_int wraps a value beyond 2**31 like a C (int) cast.
    assert move._to_c_int(0x80000000) == -2147483648
    assert move._to_c_longlong(0x8000000000000000) == -(2 ** 63)


def test_get_long_long_display():
    f = mk(b"000000000123456789", common.COB_TYPE_NUMERIC_DISPLAY, digits=18)
    assert move.cob_get_long_long(f) == 123456789


def test_display_get_int_scaled():
    # 123.45 -> integer part 123.
    f = mk(b"12345", common.COB_TYPE_NUMERIC_DISPLAY, digits=5, scale=2)
    assert move.cob_display_get_int(f) == 123
    assert move.cob_display_get_long_long(f) == 123


def test_display_get_int_negative_scale():
    # scale < 0 multiplies by 10**-scale.
    f = mk(b"12", common.COB_TYPE_NUMERIC_DISPLAY, digits=2, scale=-2)
    assert move.cob_display_get_int(f) == 1200


def test_get_int_via_default_branch_for_edited():
    p = pic(("9", 3))
    ed = mk(bytearray(3), common.COB_TYPE_NUMERIC_EDITED, digits=3, pic=p)
    move.cob_move(mk(b"123", common.COB_TYPE_NUMERIC_DISPLAY, digits=3), ed)
    assert move.cob_get_int(ed) == 123


def test_cob_init_move_is_callable():
    assert move.cob_init_move() is None


# ---------------------------------------------------------------------------
# Edited moves - floating sign / floating currency / trailing sign / insertion
# ---------------------------------------------------------------------------
def test_edited_floating_minus_negative():
    # PIC ----9 : floating sign places a single '-' before first sig digit.
    ed = mk(bytearray(5), common.COB_TYPE_NUMERIC_EDITED, digits=5, scale=0,
            pic=pic(("-", 4), ("9", 1)))
    src = mk(b"00012", common.COB_TYPE_NUMERIC_DISPLAY, digits=5, flags=HAVE_SIGN)
    common.cob_put_sign(src, -1)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"  -12"


def test_edited_floating_minus_positive_blank():
    ed = mk(bytearray(5), common.COB_TYPE_NUMERIC_EDITED, digits=5, scale=0,
            pic=pic(("-", 4), ("9", 1)))
    src = mk(b"00012", common.COB_TYPE_NUMERIC_DISPLAY, digits=5, flags=HAVE_SIGN)
    common.cob_put_sign(src, 1)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"   12"


def test_edited_floating_currency():
    ed = mk(bytearray(7), common.COB_TYPE_NUMERIC_EDITED, digits=5, scale=2,
            pic=pic(("$", 3), ("9", 1), (".", 1), ("9", 2)))
    src = mk(b"00123", common.COB_TYPE_NUMERIC_DISPLAY, digits=5, scale=2)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"  $1.23"


def test_edited_trailing_plus_negative():
    ed = mk(bytearray(4), common.COB_TYPE_NUMERIC_EDITED, digits=3, scale=0,
            pic=pic(("9", 3), ("+", 1)))
    src = mk(b"012", common.COB_TYPE_NUMERIC_DISPLAY, digits=3, flags=HAVE_SIGN)
    common.cob_put_sign(src, -1)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"012-"


def test_edited_b_and_zero_insertion():
    ed = mk(bytearray(6), common.COB_TYPE_NUMERIC_EDITED, digits=4, scale=0,
            pic=pic(("9", 2), ("B", 1), ("9", 2), ("0", 1)))
    src = mk(b"1234", common.COB_TYPE_NUMERIC_DISPLAY, digits=4)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"12 340"


def test_alphanum_to_edited_insertion_and_short_source():
    ed = mk(bytearray(7), common.COB_TYPE_ALPHANUMERIC_EDITED,
            pic=pic(("X", 2), ("B", 1), ("X", 2), ("/", 1), ("X", 1)))
    src = mk(b"ABC", common.COB_TYPE_ALPHANUMERIC)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"AB C / "


def test_alphanum_to_edited_invalid_pic_char():
    # '@' is not a valid edited PIC symbol -> emits '?' at that position.
    ed = mk(bytearray(3), common.COB_TYPE_ALPHANUMERIC_EDITED,
            pic=pic(("X", 1), ("@", 1), ("X", 1)))
    src = mk(b"AB", common.COB_TYPE_ALPHANUMERIC)
    move.cob_move(src, ed)
    assert bytes(ed.data) == b"A?B"


# ---------------------------------------------------------------------------
# alphanum_to_display - surplus-digit skip and double-decimal bad path
# ---------------------------------------------------------------------------
def test_alphanum_to_display_surplus_low_order_kept():
    src = mk(b"1234567", common.COB_TYPE_ALPHANUMERIC)
    dst = mk(bytearray(3), common.COB_TYPE_NUMERIC_DISPLAY, digits=3)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"567"


def test_alphanum_to_display_double_decimal_zeroes():
    # Wide receiver reaches the 2nd decimal point -> error path zeroes field.
    src = mk(b"1.2.3", common.COB_TYPE_ALPHANUMERIC)
    dst = mk(bytearray(5), common.COB_TYPE_NUMERIC_DISPLAY, digits=5, scale=3)
    move.cob_move(src, dst)
    assert bytes(dst.data) == b"00000"


# ---------------------------------------------------------------------------
# Dispatcher indirect-move routes (non-DISPLAY <-> non-DISPLAY)
# ---------------------------------------------------------------------------
def test_packed_to_packed_indirect():
    p1 = mk(bytearray(3), common.COB_TYPE_NUMERIC_PACKED, digits=4, flags=HAVE_SIGN)
    move.cob_move(mk(b"4567", common.COB_TYPE_NUMERIC_DISPLAY, digits=4), p1)
    p2 = mk(bytearray(3), common.COB_TYPE_NUMERIC_PACKED, digits=4, flags=HAVE_SIGN)
    move.cob_move(p1, p2)
    assert bytes(p1.data) == bytes(p2.data)


def test_binary_to_packed_indirect():
    b1 = mk(bytearray(4), common.COB_TYPE_NUMERIC_BINARY, digits=5, flags=HAVE_SIGN)
    move.cob_set_int(b1, 321)
    pk = mk(bytearray(3), common.COB_TYPE_NUMERIC_PACKED, digits=4, flags=HAVE_SIGN)
    move.cob_move(b1, pk)
    assert move.cob_packed_get_int(pk) == 321


def test_edited_to_packed_indirect():
    ed = mk(bytearray(6), common.COB_TYPE_NUMERIC_EDITED, digits=5, scale=2,
            pic=pic(("Z", 2), ("9", 1), (".", 1), ("9", 2)))
    move.cob_move(mk(b"01234", common.COB_TYPE_NUMERIC_DISPLAY, digits=5,
                     scale=2), ed)
    pk = mk(bytearray(3), common.COB_TYPE_NUMERIC_PACKED, digits=4, scale=2,
            flags=HAVE_SIGN)
    move.cob_move(ed, pk)
    assert move.cob_packed_get_int(pk) == 12   # integer part of 12.34


def test_double_to_packed_indirect():
    fl = mk(bytearray(8), common.COB_TYPE_NUMERIC_DOUBLE)
    move.cob_move(mk(b"01234", common.COB_TYPE_NUMERIC_DISPLAY, digits=5,
                     scale=2), fl)
    pk = mk(bytearray(3), common.COB_TYPE_NUMERIC_PACKED, digits=4, scale=2,
            flags=HAVE_SIGN)
    move.cob_move(fl, pk)
    assert move.cob_packed_get_int(pk) == 12


def test_display_to_alphanum_edited_via_scale_indirect():
    # scale > digits forces the indirect display->display path before editing.
    ed = mk(bytearray(4), common.COB_TYPE_ALPHANUMERIC_EDITED,
            pic=pic(("X", 4)))
    src = mk(b"12", common.COB_TYPE_NUMERIC_DISPLAY, digits=2, scale=4)
    move.cob_move(src, ed)
    assert len(ed.data) == 4


# ---------------------------------------------------------------------------
# get_int / get_long_long across all USAGEs (covers dispatch branches)
# ---------------------------------------------------------------------------
def test_get_long_long_binary_negative():
    b8 = mk(bytearray(8), common.COB_TYPE_NUMERIC_BINARY, digits=18,
            flags=HAVE_SIGN)
    move.cob_set_int(b8, -999)
    assert move.cob_get_long_long(b8) == -999


def test_get_long_long_packed():
    p1 = mk(bytearray(3), common.COB_TYPE_NUMERIC_PACKED, digits=4,
            flags=HAVE_SIGN)
    move.cob_move(mk(b"4567", common.COB_TYPE_NUMERIC_DISPLAY, digits=4), p1)
    assert move.cob_get_long_long(p1) == 4567


def test_get_int_edited_default_branch():
    ed = mk(bytearray(3), common.COB_TYPE_NUMERIC_EDITED, digits=3,
            pic=pic(("9", 3)))
    move.cob_move(mk(b"077", common.COB_TYPE_NUMERIC_DISPLAY, digits=3), ed)
    assert move.cob_get_int(ed) == 77


def test_get_long_long_edited_default_branch():
    ed = mk(bytearray(3), common.COB_TYPE_NUMERIC_EDITED, digits=3,
            pic=pic(("9", 3)))
    move.cob_move(mk(b"077", common.COB_TYPE_NUMERIC_DISPLAY, digits=3), ed)
    assert move.cob_get_long_long(ed) == 77


def test_display_get_int_skips_leading_zeros():
    f = mk(b"00099", common.COB_TYPE_NUMERIC_DISPLAY, digits=5)
    assert move.cob_display_get_int(f) == 99


def test_set_int_zero_clears_field():
    f = mk(bytearray(5), common.COB_TYPE_NUMERIC_DISPLAY, digits=5,
           flags=HAVE_SIGN)
    move.cob_set_int(f, 12345)
    move.cob_set_int(f, 0)
    assert move.cob_get_int(f) == 0


# ===========================================================================
# move.cob_set_pointer - CALL ... RETURNING pointer store path.
# The emitter chooses the move.* namespace for symmetry with move.cob_set_int;
# pointer storage is owned by common (the shared raw-address-bytes model), so
# this delegates there.
# ===========================================================================
import sys as _sys


def _ptr_field(addr=0):
    w = struct.calcsize("P")
    return common.cob_field(
        size=w, data=bytearray(int(addr).to_bytes(w, _sys.byteorder)),
        attr=common.cob_field_attr(type=common.COB_TYPE_NUMERIC_BINARY))


class TestMoveSetPointer:
    def test_round_trips_via_common(self):
        f = _ptr_field(0)
        ret = move.cob_set_pointer(f, 0x7777)
        assert ret is f
        assert common.cob_get_pointer(f) == 0x7777

    def test_null_value(self):
        f = _ptr_field(0x1234)
        move.cob_set_pointer(f, None)
        assert common.cob_get_pointer(f) == 0

    def test_matches_common_set_pointer(self):
        a, b = _ptr_field(0), _ptr_field(0)
        move.cob_set_pointer(a, 0x5555)
        common.cob_set_pointer(b, 0x5555)
        assert bytes(a.data) == bytes(b.data)
