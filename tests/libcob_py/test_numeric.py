"""Unit tests for :mod:`libcob_py.numeric`.

Exercises the pure-Python port of the C runtime ``libcob/numeric.c`` and the
inline byte-level helpers of ``libcob/codegen.h`` that replace the GMP
``cob_decimal`` arithmetic path with stdlib :mod:`decimal`.  The suite covers,
per the AAP numeric-parity gate (sections 0.6.2 / 0.7.1):

* the ``cob_decimal`` value model and its arithmetic API,
* byte-for-byte STORAGE layout for every ``USAGE``
  (DISPLAY / BINARY / COMP-3 / COMP-5 / COMP-X), including the BLOCKING
  ``S9(7)V99 COMP-3 VALUE -123.45 -> 00 00 12 34 5d`` parity case,
* the 7 ROUNDED-mode -> :mod:`decimal` rounding-constant mappings,
* overflow / divide-by-zero raising the ``EC-SIZE-*`` family, and
* the ``codegen.h`` integer fast-path mirrors (numdisp, packed-int, and the
  width/sign-specialised ``cob_*_NN_binary`` family).

Standard library only - the runtime under test introduces ZERO third-party
dependencies; ``pytest`` is a development-only test framework (AAP 0.5 / 0.7.1).
"""
import struct
import sys

import pytest

# Obtain the parallel-built runtime lazily so collection degrades to a clean
# SKIP (never a hard error) when the package is not yet importable.
libcob_py = pytest.importorskip("libcob_py")
common = pytest.importorskip("libcob_py.common")
numeric = pytest.importorskip("libcob_py.numeric")


# ---------------------------------------------------------------------------
# USAGE / flag shorthands
# ---------------------------------------------------------------------------
T_DISP = common.COB_TYPE_NUMERIC_DISPLAY
T_BIN = common.COB_TYPE_NUMERIC_BINARY
T_PACK = common.COB_TYPE_NUMERIC_PACKED
T_FLOAT = common.COB_TYPE_NUMERIC_FLOAT
T_DOUBLE = common.COB_TYPE_NUMERIC_DOUBLE
T_EDITED = common.COB_TYPE_NUMERIC_EDITED

F_SIGN = common.COB_FLAG_HAVE_SIGN
F_SEP = common.COB_FLAG_SIGN_SEPARATE
F_LEAD = common.COB_FLAG_SIGN_LEADING
F_SWAP = common.COB_FLAG_BINARY_SWAP

O_ROUND = common.COB_STORE_ROUND
O_KEEP = common.COB_STORE_KEEP_ON_OVERFLOW
O_TRUNC = common.COB_STORE_TRUNC_ON_OVERFLOW

EC_OVERFLOW = 0x1004
EC_ZERO_DIVIDE = 0x1007


def mk(ftype, digits, scale, flags, size, data=None):
    """Build a ``cob_field`` with a zeroed (or supplied) backing buffer."""
    attr = common.cob_field_attr(type=ftype, digits=digits, scale=scale,
                                 flags=flags, pic=None)
    buf = bytearray(size) if data is None else bytearray(data)
    return common.cob_field(size=size, data=buf, attr=attr)


def enc(field, unscaled, scale, opt=0):
    """Encode ``unscaled * 10**-scale`` into *field*; return the field."""
    numeric.cob_decimal_get_field(numeric.cob_decimal(unscaled, scale), field, opt)
    return field


def dec(field):
    """Decode *field* back to a ``(unscaled, scale)`` tuple."""
    d = numeric.cob_decimal()
    numeric.cob_decimal_set_field(d, field)
    return d.value, d.scale


@pytest.fixture(autouse=True)
def _fresh_numeric_state():
    """Reset the module's scratch decimals before each test (cob_init_numeric)."""
    numeric.cob_init_numeric()
    common.cob_exception_code = 0
    yield


# ===========================================================================
# cob_decimal value model + lifecycle
# ===========================================================================
class TestCobDecimal:
    def test_construct_and_repr(self):
        d = numeric.cob_decimal(-12345, 2)
        assert (d.value, d.scale) == (-12345, 2)
        assert "value=-12345" in repr(d)

    def test_default_is_zero(self):
        d = numeric.cob_decimal()
        assert (d.value, d.scale) == (0, 0)

    def test_init_resets(self):
        d = numeric.cob_decimal(99, 5)
        numeric.cob_decimal_init(d)
        assert (d.value, d.scale) == (0, 0)

    def test_set_copies(self):
        src = numeric.cob_decimal(42, 3)
        dst = numeric.cob_decimal()
        numeric.cob_decimal_set(dst, src)
        assert (dst.value, dst.scale) == (42, 3)
        # independent copy
        src.value = 0
        assert dst.value == 42

    def test_init_numeric_primes_context(self):
        numeric.cob_init_numeric()
        import decimal
        assert decimal.getcontext().prec >= 64
        # scratch decimals reset
        assert (numeric.cob_d1.value, numeric.cob_d1.scale) == (0, 0)


# ===========================================================================
# cob_decimal_set_int / cob_decimal_set_uint
#
# These are the runtime homes of the helpers the C backend used to EMIT as
# static functions in every translation unit (codegen.c gen_decset/gen_udecset).
# The front-end (typeck.c L2046/L2057/L2076/L2079) now emits calls to
# numeric.cob_decimal_set_int / _uint instead.  mpz_set_si / mpz_set_ui set the
# (signed / unsigned) integer with scale 0.
# ===========================================================================
class TestCobDecimalSetInt:
    def test_set_int_positive(self):
        d = numeric.cob_decimal(99, 5)
        numeric.cob_decimal_set_int(d, 12345)
        assert (d.value, d.scale) == (12345, 0)

    def test_set_int_negative(self):
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_int(d, -12345)
        assert (d.value, d.scale) == (-12345, 0)

    def test_set_int_zero(self):
        d = numeric.cob_decimal(7, 2)
        numeric.cob_decimal_set_int(d, 0)
        assert (d.value, d.scale) == (0, 0)

    def test_set_int_resets_scale(self):
        # A non-zero pre-existing scale must be cleared (mpz_set_si; d->scale = 0).
        d = numeric.cob_decimal(1, 9)
        numeric.cob_decimal_set_int(d, 5)
        assert d.scale == 0

    def test_set_uint_small(self):
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_uint(d, 7)
        assert (d.value, d.scale) == (7, 0)

    def test_set_uint_above_int_max(self):
        # Unsigned PICTUREs can carry values above 2**31 (cob_get_int magnitude).
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_uint(d, 4000000000)
        assert (d.value, d.scale) == (4000000000, 0)

    def test_set_uint_wrap_faithful_to_c_unsigned_int(self):
        # The C parameter is ``unsigned int``; a negative argument wraps modulo
        # 2**32.  The front-end never produces this, but the mask preserves
        # byte-for-byte parity with mpz_set_ui for the theoretical edge.
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_uint(d, -1)
        assert d.value == 0xFFFFFFFF
        assert d.scale == 0


# ===========================================================================
# decimal.Decimal float bridge
# ===========================================================================
class TestDoubleBridge:
    def test_set_get_double_roundtrip(self):
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_double(d, 123.5)
        assert abs(numeric.cob_decimal_get_double(d) - 123.5) < 1e-6

    def test_set_double_negative(self):
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_double(d, -0.25)
        assert abs(numeric.cob_decimal_get_double(d) + 0.25) < 1e-6

    def test_get_double_zero(self):
        d = numeric.cob_decimal(0, 0)
        assert numeric.cob_decimal_get_double(d) == 0.0


# ===========================================================================
# Scale shifting / alignment + decimal arithmetic
# ===========================================================================
class TestArithmetic:
    def test_shift_decimal_up(self):
        d = numeric.cob_decimal(123, 0)
        numeric.shift_decimal(d, 2)
        assert d.value == 12300

    def test_shift_decimal_down_truncates_toward_zero(self):
        d = numeric.cob_decimal(-129, 0)
        numeric.shift_decimal(d, -1)        # mpz_tdiv_q semantics
        assert d.value == -12

    def test_align_decimal(self):
        a = numeric.cob_decimal(5, 0)
        b = numeric.cob_decimal(125, 2)
        numeric.align_decimal(a, b)
        assert a.scale == b.scale == 2
        assert a.value == 500 and b.value == 125

    def test_add(self):
        a = numeric.cob_decimal(125, 2)     # 1.25
        b = numeric.cob_decimal(5, 1)       # 0.5
        numeric.cob_decimal_add(a, b)
        assert (a.value, a.scale) == (175, 2)   # 1.75

    def test_sub(self):
        a = numeric.cob_decimal(200, 2)
        b = numeric.cob_decimal(125, 2)
        numeric.cob_decimal_sub(a, b)
        assert (a.value, a.scale) == (75, 2)

    def test_mul(self):
        a = numeric.cob_decimal(12, 1)      # 1.2
        b = numeric.cob_decimal(3, 0)       # 3
        numeric.cob_decimal_mul(a, b)
        assert (a.value, a.scale) == (36, 1)        # 1.2 * 3 == 3.6

    def test_div(self):
        a = numeric.cob_decimal(1, 0)
        b = numeric.cob_decimal(4, 0)
        numeric.cob_decimal_div(a, b)
        # 1/4 -> 0.25 exactly after the 37-digit guard + truncation
        q = a.value * (10 ** -a.scale) if a.scale <= 0 else a.value / 10 ** a.scale
        assert abs(q - 0.25) < 1e-30

    def test_div_by_zero_sets_nan_and_exception(self):
        a = numeric.cob_decimal(10, 0)
        b = numeric.cob_decimal(0, 0)
        numeric.cob_decimal_div(a, b)
        assert a.scale == numeric.DECIMAL_NAN
        assert common.cob_exception_code == EC_ZERO_DIVIDE

    def test_div_zero_dividend(self):
        a = numeric.cob_decimal(0, 0)
        b = numeric.cob_decimal(7, 0)
        numeric.cob_decimal_div(a, b)
        assert a.value == 0

    def test_pow_integer(self):
        a = numeric.cob_decimal(2, 0)
        b = numeric.cob_decimal(10, 0)
        numeric.cob_decimal_pow(a, b)
        assert a.value * 10 ** -a.scale == 1024

    def test_cmp(self):
        assert numeric.cob_decimal_cmp(numeric.cob_decimal(5, 0),
                                       numeric.cob_decimal(3, 0)) == 1
        assert numeric.cob_decimal_cmp(numeric.cob_decimal(3, 0),
                                       numeric.cob_decimal(5, 0)) == -1
        assert numeric.cob_decimal_cmp(numeric.cob_decimal(125, 2),
                                       numeric.cob_decimal(5, 1)) == 1
        assert numeric.cob_decimal_cmp(numeric.cob_decimal(50, 1),
                                       numeric.cob_decimal(5, 0)) == 0


# ===========================================================================
# ROUNDED-mode mapping + rounding engine (AAP 0.6.2)
# ===========================================================================
class TestRounding:
    def test_round_mode_map_exact(self):
        import decimal
        assert numeric.ROUND_MODE_MAP == {
            "NEAREST-AWAY-FROM-ZERO": decimal.ROUND_HALF_UP,
            "NEAREST-EVEN": decimal.ROUND_HALF_EVEN,
            "NEAREST-TOWARD-ZERO": decimal.ROUND_HALF_DOWN,
            "TOWARD-GREATER": decimal.ROUND_CEILING,
            "TOWARD-LESSER": decimal.ROUND_FLOOR,
            "TRUNCATION": decimal.ROUND_DOWN,
            "AWAY-FROM-ZERO": decimal.ROUND_UP,
        }
        assert numeric.COB_ROUND_DEFAULT == decimal.ROUND_HALF_UP
        assert numeric.COB_ROUND_TRUNCATION == decimal.ROUND_DOWN

    @pytest.mark.parametrize("name,p25,n25,p21,n21", [
        ("NEAREST-AWAY-FROM-ZERO", 3, -3, 2, -2),
        ("NEAREST-EVEN", 2, -2, 2, -2),
        ("NEAREST-TOWARD-ZERO", 2, -2, 2, -2),
        ("TOWARD-GREATER", 3, -2, 3, -2),
        ("TOWARD-LESSER", 2, -3, 2, -3),
        ("TRUNCATION", 2, -2, 2, -2),
        ("AWAY-FROM-ZERO", 3, -3, 3, -3),
    ])
    def test_round_value_to_scale_all_modes(self, name, p25, n25, p21, n21):
        mode = numeric.ROUND_MODE_MAP[name]
        assert numeric._round_value_to_scale(25, 1, 0, mode) == p25
        assert numeric._round_value_to_scale(-25, 1, 0, mode) == n25
        assert numeric._round_value_to_scale(21, 1, 0, mode) == p21
        assert numeric._round_value_to_scale(-21, 1, 0, mode) == n21

    def test_round_extends_when_target_scale_larger(self):
        # to_scale > from_scale -> exact zero-extension, no rounding
        assert numeric._round_value_to_scale(123, 0, 2, numeric.COB_ROUND_DEFAULT) == 12300

    def test_round_zero(self):
        assert numeric._round_value_to_scale(0, 5, 0, numeric.COB_ROUND_DEFAULT) == 0

    def test_get_field_rounded_vs_truncated(self):
        f = mk(T_DISP, 3, 0, 0, 3)
        numeric.cob_decimal_get_field(numeric.cob_decimal(25, 1), f, O_ROUND)
        assert bytes(f.data) == b"003"               # 2.5 ROUNDED -> 3
        numeric.cob_decimal_get_field(numeric.cob_decimal(29, 1), f, 0)
        assert bytes(f.data) == b"002"               # 2.9 truncated -> 2

    def test_get_field_explicit_rounding_mode(self):
        f = mk(T_DISP, 3, 0, 0, 3)
        # NEAREST-EVEN: 2.5 -> 2
        numeric.cob_decimal_get_field(numeric.cob_decimal(25, 1), f, O_ROUND,
                                      rounding=numeric.ROUND_MODE_MAP["NEAREST-EVEN"])
        assert bytes(f.data) == b"002"


# ===========================================================================
# DISPLAY (zoned decimal) storage
# ===========================================================================
class TestDisplay:
    def test_unsigned_roundtrip(self):
        f = mk(T_DISP, 5, 0, 0, 5)
        enc(f, 12345, 0)
        assert bytes(f.data) == b"12345"
        assert dec(f) == (12345, 0)

    def test_signed_roundtrip_negative(self):
        f = mk(T_DISP, 5, 0, F_SIGN, 5)
        enc(f, -12345, 0)
        assert dec(f) == (-12345, 0)

    def test_signed_roundtrip_positive(self):
        f = mk(T_DISP, 5, 0, F_SIGN, 5)
        enc(f, 12345, 0)
        assert dec(f) == (12345, 0)

    def test_scaled_roundtrip(self):
        f = mk(T_DISP, 5, 2, F_SIGN, 5)
        enc(f, -12345, 2)            # -123.45
        assert dec(f) == (-12345, 2)

    def test_separate_leading_sign(self):
        # SEPARATE LEADING: sign byte at data[0], digits follow
        f = mk(T_DISP, 3, 0, F_SIGN | F_SEP | F_LEAD, 4)
        enc(f, -123, 0)
        assert dec(f) == (-123, 0)

    def test_separate_trailing_sign(self):
        f = mk(T_DISP, 3, 0, F_SIGN | F_SEP, 4)
        enc(f, -123, 0)
        assert dec(f) == (-123, 0)

    def test_high_value_sentinel(self):
        f = mk(T_DISP, 5, 0, 0, 5, data=b"\xff\xff\xff\xff\xff")
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_display(d, f)
        assert d.value == 10 ** 5

    def test_low_value_sentinel(self):
        f = mk(T_DISP, 5, 0, 0, 5, data=b"\x00\x00\x00\x00\x00")
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_display(d, f)
        assert d.value == -(10 ** 5)

    def test_overflow_keep(self):
        f = mk(T_DISP, 3, 0, 0, 3)
        rc = numeric.cob_decimal_get_field(numeric.cob_decimal(12345, 0), f, O_KEEP)
        assert rc == EC_OVERFLOW

    def test_overflow_truncates_by_default(self):
        f = mk(T_DISP, 3, 0, 0, 3)
        numeric.cob_decimal_get_field(numeric.cob_decimal(12345, 0), f, 0)
        assert bytes(f.data) == b"345"        # low-order 3 digits kept


# ===========================================================================
# BINARY (COMP / COMP-4 / COMP-5 / COMP-X) storage
# ===========================================================================
class TestBinary:
    def test_get_set_int64_native(self):
        f = mk(T_BIN, 9, 0, F_SIGN, 4)
        numeric.cob_binary_set_int64(f, -12345)
        assert numeric.cob_binary_get_int64(f) == -12345

    def test_get_set_uint64_native(self):
        f = mk(T_BIN, 9, 0, 0, 4)
        numeric.cob_binary_set_uint64(f, 4000000000)
        assert numeric.cob_binary_get_uint64(f) == 4000000000

    def test_comp_big_endian_signed(self):
        f = mk(T_BIN, 9, 0, F_SIGN | F_SWAP, 4)
        enc(f, 123456789, 0)
        assert bytes(f.data) == (123456789).to_bytes(4, "big", signed=True)
        assert dec(f) == (123456789, 0)
        enc(f, -123456789, 0)
        assert dec(f) == (-123456789, 0)

    def test_comp_x_unsigned_native(self):
        f = mk(T_BIN, 4, 0, 0, 2)
        enc(f, 9999, 0)
        assert dec(f) == (9999, 0)

    def test_comp5_signed_native(self):
        f = mk(T_BIN, 4, 0, F_SIGN, 2)
        enc(f, -1234, 0)
        assert dec(f) == (-1234, 0)

    @pytest.mark.parametrize("size,val", [
        (2, 1234), (4, 123456789), (8, 123456789012345678),
    ])
    def test_standard_widths_big_endian(self, size, val):
        f = mk(T_BIN, size * 4, 0, F_SIGN | F_SWAP, size)
        enc(f, val, 0)
        assert bytes(f.data) == val.to_bytes(size, "big", signed=True)
        assert dec(f) == (val, 0)

    @pytest.mark.parametrize("size,val", [
        (3, 1000000), (5, 1099511627), (6, 281474976710), (7, 72057594037927),
    ])
    def test_nonstandard_widths(self, size, val):
        f = mk(T_BIN, size * 2, 0, F_SIGN | F_SWAP, size)
        numeric.cob_binary_set_int64(f, val)
        assert numeric.cob_binary_get_int64(f) == val
        numeric.cob_binary_set_int64(f, -val)
        assert numeric.cob_binary_get_int64(f) == -val

    def test_zero_stores_zero_bytes(self):
        f = mk(T_BIN, 9, 0, F_SIGN | F_SWAP, 4, data=b"\x01\x02\x03\x04")
        numeric.cob_decimal_get_field(numeric.cob_decimal(0, 0), f, 0)
        assert bytes(f.data) == b"\x00\x00\x00\x00"

    def test_overflow_keep(self):
        f = mk(T_BIN, 4, 0, F_SIGN | F_SWAP, 2)
        rc = numeric.cob_decimal_get_field(numeric.cob_decimal(10 ** 9, 0), f, O_KEEP)
        assert rc == EC_OVERFLOW

    def test_overflow_trunc(self):
        f = mk(T_BIN, 4, 0, F_SIGN | F_SWAP, 8)   # wide storage, 4 digits
        rc = numeric.cob_decimal_get_field(numeric.cob_decimal(123456, 0), f, O_TRUNC)
        # digit-count overflow -> keeps low 4 digits (3456)
        assert rc == EC_OVERFLOW
        assert numeric.cob_binary_get_int64(f) == 3456


# ===========================================================================
# PACKED-DECIMAL / COMP-3 storage
# ===========================================================================
class TestPacked:
    def test_parity_negative(self):
        """BLOCKING gate: S9(7)V99 COMP-3 VALUE -123.45 -> 00 00 12 34 5d."""
        f = mk(T_PACK, 9, 2, F_SIGN, 5)
        enc(f, -12345, 2)
        assert bytes(f.data) == bytes.fromhex("000012345d")
        assert dec(f) == (-12345, 2)

    def test_parity_positive(self):
        f = mk(T_PACK, 9, 2, F_SIGN, 5)
        enc(f, 12345, 2)
        assert bytes(f.data) == bytes.fromhex("000012345c")

    def test_unsigned_sign_nibble(self):
        f = mk(T_PACK, 9, 2, 0, 5)
        enc(f, 12345, 2)
        assert bytes(f.data) == bytes.fromhex("000012345f")

    def test_get_sign(self):
        f = mk(T_PACK, 9, 2, F_SIGN, 5, data=bytes.fromhex("000012345d"))
        assert numeric.cob_packed_get_sign(f) == -1
        f = mk(T_PACK, 9, 2, F_SIGN, 5, data=bytes.fromhex("000012345c"))
        assert numeric.cob_packed_get_sign(f) == 1

    def test_set_packed_zero_signed(self):
        f = mk(T_PACK, 9, 2, F_SIGN, 5, data=b"\x99\x99\x99\x99\x99")
        numeric.cob_set_packed_zero(f)
        assert bytes(f.data) == bytes.fromhex("000000000c")

    def test_set_packed_zero_unsigned(self):
        f = mk(T_PACK, 9, 2, 0, 5, data=b"\x99\x99\x99\x99\x99")
        numeric.cob_set_packed_zero(f)
        assert bytes(f.data) == bytes.fromhex("000000000f")

    def test_set_packed_int(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, -678)
        assert numeric.cob_get_packed_int(f) == -678

    def test_add_packed(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 100)
        numeric.cob_add_packed(f, 23)
        assert numeric.cob_get_packed_int(f) == 123

    def test_add_packed_sign_flip(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 5)
        numeric.cob_add_packed(f, -8)          # 5 - 8 = -3 (crosses zero)
        assert numeric.cob_get_packed_int(f) == -3

    def test_complement_packed(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 100)
        numeric.cob_complement_packed(f)       # ten's complement of the digits
        # complement of 000000100 over 9 digits = 999999900
        assert (numeric.cob_get_packed_int(f) % (10 ** 9)) == (10 ** 9 - 100)

    # The AAP numeric-parity gate (0.6.2 / 0.7.1) mandates >= 3 COMP-3 cases:
    # truncation, rounding, and the overflow boundary.
    def test_comp3_truncation(self):
        # S9(5)V99: store 123.499 with no ROUNDED -> truncate to 123.49
        f = mk(T_PACK, 7, 2, F_SIGN, 4)
        numeric.cob_decimal_get_field(numeric.cob_decimal(123499, 3), f, 0)
        assert dec(f) == (12349, 2)

    def test_comp3_rounding(self):
        # same value WITH ROUNDED -> 123.50
        f = mk(T_PACK, 7, 2, F_SIGN, 4)
        numeric.cob_decimal_get_field(numeric.cob_decimal(123499, 3), f, O_ROUND)
        assert dec(f) == (12350, 2)

    def test_comp3_overflow_boundary_keep(self):
        # S9(3): storing 12345 overflows; KEEP abandons the store
        f = mk(T_PACK, 3, 0, F_SIGN, 2)
        rc = numeric.cob_decimal_get_field(numeric.cob_decimal(12345, 0), f, O_KEEP)
        assert rc == EC_OVERFLOW

    def test_comp3_overflow_boundary_truncates(self):
        # without KEEP, the high-order digits are dropped (keep low 3 -> 345)
        f = mk(T_PACK, 3, 0, F_SIGN, 2)
        numeric.cob_decimal_get_field(numeric.cob_decimal(12345, 0), f, 0)
        assert numeric.cob_get_packed_int(f) == 345


# ===========================================================================
# FLOAT / DOUBLE dispatch through set_field / get_field
# ===========================================================================
class TestFloatDouble:
    def test_double_set_field(self):
        f = mk(T_DOUBLE, 0, 0, 0, 8, data=struct.pack("=d", 123.5))
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_field(d, f)
        assert abs(numeric.cob_decimal_get_double(d) - 123.5) < 1e-6

    def test_double_get_field(self):
        # A COMP-2 field carries scale 0; get_field shifts the decimal to the
        # field scale before the IEEE conversion (numeric.c L1007 +
        # L1019-L1021), exactly as the C runtime does.  An integer value is
        # therefore the unambiguous, byte-for-byte case.
        f = mk(T_DOUBLE, 0, 0, 0, 8)
        numeric.cob_decimal_get_field(numeric.cob_decimal(123, 0), f, 0)
        assert abs(struct.unpack("=d", bytes(f.data))[0] - 123.0) < 1e-9

    def test_float_set_field(self):
        f = mk(T_FLOAT, 0, 0, 0, 4, data=struct.pack("=f", 2.5))
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_field(d, f)
        assert abs(numeric.cob_decimal_get_double(d) - 2.5) < 1e-5

    def test_float_get_field(self):
        # See test_double_get_field: field scale 0 -> integer value is exact.
        f = mk(T_FLOAT, 0, 0, 0, 4)
        numeric.cob_decimal_get_field(numeric.cob_decimal(7, 0), f, 0)
        assert abs(struct.unpack("=f", bytes(f.data))[0] - 7.0) < 1e-5


# ===========================================================================
# NaN handling on store
# ===========================================================================
class TestNaNStore:
    def test_get_field_nan_raises_overflow(self):
        f = mk(T_DISP, 5, 0, 0, 5)
        d = numeric.cob_decimal(0, numeric.DECIMAL_NAN)
        rc = numeric.cob_decimal_get_field(d, f, 0)
        assert rc == EC_OVERFLOW
        assert common.cob_exception_code == EC_OVERFLOW


# ===========================================================================
# In-place DISPLAY arithmetic + statement-level helpers
# ===========================================================================
class TestStatementHelpers:
    def test_display_add_int(self):
        f = mk(T_DISP, 5, 0, F_SIGN, 5)
        enc(f, 100, 0)
        numeric.cob_display_add_int(f, 23)
        assert dec(f) == (123, 0)

    def test_display_add_int_negative_result(self):
        f = mk(T_DISP, 5, 0, F_SIGN, 5)
        enc(f, 5, 0)
        numeric.cob_display_add_int(f, -8)
        assert dec(f) == (-3, 0)

    def test_cob_add_int_display(self):
        f = mk(T_DISP, 5, 2, F_SIGN, 5)
        enc(f, 10000, 2)                      # 100.00
        numeric.cob_add_int(f, 5)             # + 5.00
        assert dec(f) == (10500, 2)

    def test_cob_add_int_packed(self):
        f = mk(T_PACK, 9, 2, F_SIGN, 5)
        enc(f, 10000, 2)                      # 100.00
        numeric.cob_add_int(f, 5)             # scale-aware add -> 105.00
        assert dec(f) == (10500, 2)

    def test_cob_add_int_binary(self):
        f = mk(T_BIN, 9, 0, F_SIGN | F_SWAP, 4)
        enc(f, 1000, 0)
        numeric.cob_add_int(f, 234)
        assert dec(f) == (1234, 0)

    def test_cob_sub_int(self):
        f = mk(T_DISP, 5, 0, F_SIGN, 5)
        enc(f, 500, 0)
        numeric.cob_sub_int(f, 123)
        assert dec(f) == (377, 0)

    def test_cob_add_fields(self):
        a = mk(T_DISP, 5, 2, F_SIGN, 5)
        b = mk(T_DISP, 5, 2, F_SIGN, 5)
        enc(a, 12500, 2)        # 125.00
        enc(b, 5000, 2)         # 50.00
        numeric.cob_add(a, b, 0)
        assert dec(a) == (17500, 2)

    def test_cob_sub_fields(self):
        a = mk(T_DISP, 5, 2, F_SIGN, 5)
        b = mk(T_DISP, 5, 2, F_SIGN, 5)
        enc(a, 20000, 2)
        enc(b, 5000, 2)
        numeric.cob_sub(a, b, 0)
        assert dec(a) == (15000, 2)

    def test_div_quotient_and_remainder(self):
        dividend = mk(T_DISP, 5, 0, F_SIGN, 5)
        divisor = mk(T_DISP, 5, 0, F_SIGN, 5)
        quotient = mk(T_DISP, 5, 0, F_SIGN, 5)
        remainder = mk(T_DISP, 5, 0, F_SIGN, 5)
        enc(dividend, 17, 0)
        enc(divisor, 5, 0)
        numeric.cob_div_quotient(dividend, divisor, quotient, 0)
        assert dec(quotient) == (3, 0)
        numeric.cob_div_remainder(remainder, 0)
        assert dec(remainder) == (2, 0)

    def test_cmp_int(self):
        f = mk(T_DISP, 5, 0, F_SIGN, 5)
        enc(f, 100, 0)
        assert numeric.cob_cmp_int(f, 100) == 0
        assert numeric.cob_cmp_int(f, 50) == 1
        assert numeric.cob_cmp_int(f, 200) == -1

    def test_cmp_uint(self):
        f = mk(T_DISP, 5, 0, 0, 5)
        enc(f, 100, 0)
        assert numeric.cob_cmp_uint(f, 100) == 0
        assert numeric.cob_cmp_uint(f, 99) == 1

    def test_numeric_cmp(self):
        a = mk(T_DISP, 5, 2, F_SIGN, 5)
        b = mk(T_PACK, 9, 2, F_SIGN, 5)
        enc(a, 12345, 2)
        enc(b, 12345, 2)
        assert numeric.cob_numeric_cmp(a, b) == 0
        enc(b, 12300, 2)
        assert numeric.cob_numeric_cmp(a, b) == 1

    def test_cmp_packed(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 123)
        assert numeric.cob_cmp_packed(f, 123) == 0
        assert numeric.cob_cmp_packed(f, 100) > 0
        assert numeric.cob_cmp_packed(f, 200) < 0


# ===========================================================================
# codegen.h inline mirrors
# ===========================================================================
class TestCodegenMirrors:
    def test_get_numdisp(self):
        assert numeric.cob_get_numdisp(b"12345", 5) == 12345
        # byte > '9' (overpunch) contributes 10
        assert numeric.cob_get_numdisp(b"123" + bytes([ord("9") + 5]), 4) == 1230 + 10

    def test_packed_int_helpers(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, -12345)
        assert numeric.cob_get_packed_int(f) == -12345
        assert numeric.cob_cmp_packed_int(f, -12345) == 0
        assert numeric.cob_cmp_packed_int(f, 0) == -1
        assert numeric.cob_cmp_packed_int(f, -99999) == 1

    def test_add_packed_int_same_sign(self):
        f = mk(T_PACK, 9, 0, 0, 5)        # unsigned, scale 0
        numeric.cob_set_packed_int(f, 100)
        numeric.cob_add_packed_int(f, 23)
        assert numeric.cob_get_packed_int(f) == 123

    def test_add_packed_int_opposite_sign_delegates(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, -100)
        numeric.cob_add_packed_int(f, 30)      # opposite sign -> cob_add_int path
        assert numeric.cob_get_packed_int(f) == -70

    def test_add_packed_int_zero_noop(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 42)
        assert numeric.cob_add_packed_int(f, 0) == 0
        assert numeric.cob_get_packed_int(f) == 42

    def test_cmp_numdisp(self):
        assert numeric.cob_cmp_numdisp(b"12345", 5, 12345) == 0
        assert numeric.cob_cmp_numdisp(b"12345", 5, 99999) == -1
        assert numeric.cob_cmp_numdisp(b"12345", 5, 1) == 1
        assert numeric.cob_cmp_long_numdisp(b"00100", 5, 100) == 0

    def test_cmp_sign_numdisp_ascii_overpunch(self):
        # '123u' -> 123|5 negative (ASCII 'p'+5='u') -> -1235
        data = b"123" + bytes([ord("p") + 5])
        assert numeric.cob_cmp_sign_numdisp(data, 4, -1235) == 0
        assert numeric.cob_cmp_sign_numdisp(b"1235", 4, 1235) == 0
        assert numeric.cob_cmp_long_sign_numdisp(b"1235", 4, 1235) == 0

    def test_sign_decode_helpers(self):
        assert numeric.cob_get_ebcdic_sign(ord("{")) == (0, False)
        assert numeric.cob_get_ebcdic_sign(ord("A")) == (1, False)
        assert numeric.cob_get_ebcdic_sign(ord("}")) == (0, True)
        assert numeric.cob_get_ebcdic_sign(ord("R")) == (9, True)
        assert numeric.cob_get_ebcdic_sign(ord("0")) == (0, False)   # unknown -> default
        assert numeric.cob_get_ascii_sign(ord("p")) == 0
        assert numeric.cob_get_ascii_sign(ord("y")) == 9
        assert numeric.cob_get_long_ebcdic_sign(ord("J")) == (1, True)
        assert numeric.cob_get_long_ascii_sign(ord("t")) == 4

    @pytest.mark.parametrize("bits", [8, 16, 24, 32, 40, 48, 56, 64])
    def test_binary_family_native_cmp_add_sub(self, bits):
        nbytes = bits // 8
        order = sys.byteorder
        # unsigned compare
        cmp_u = getattr(numeric, "cob_cmp_u%d_binary" % bits)
        add_u = getattr(numeric, "cob_add_u%d_binary" % bits)
        sub_u = getattr(numeric, "cob_sub_u%d_binary" % bits)
        buf = bytearray((100).to_bytes(nbytes, order))
        assert cmp_u(buf, 100) == 0
        assert cmp_u(buf, 50) == 1
        assert cmp_u(buf, 200) == -1
        assert cmp_u(buf, -1) == 1               # unsigned field vs negative
        add_u(buf, 5)
        assert int.from_bytes(bytes(buf[:nbytes]), order) == 105
        sub_u(buf, 10)
        assert int.from_bytes(bytes(buf[:nbytes]), order) == 95
        # signed compare
        cmp_s = getattr(numeric, "cob_cmp_s%d_binary" % bits)
        sbuf = bytearray((-7).to_bytes(nbytes, order, signed=True))
        assert cmp_s(sbuf, -7) == 0
        assert cmp_s(sbuf, 0) == -1

    @pytest.mark.parametrize("bits", [16, 24, 32, 40, 48, 56, 64])
    def test_binary_family_swap(self, bits):
        nbytes = bits // 8
        setswp = getattr(numeric, "cob_setswp_u%d_binary" % bits)
        cmpswp = getattr(numeric, "cob_cmpswp_u%d_binary" % bits)
        buf = bytearray(nbytes)
        setswp(buf, 258)
        assert bytes(buf[:nbytes]) == (258).to_bytes(nbytes, "big")
        assert cmpswp(buf, 258) == 0
        assert cmpswp(buf, 1) == 1

    @pytest.mark.parametrize("bits", [16, 32, 64])
    def test_binary_align_aliases(self, bits):
        # aligned variants are semantic aliases of the plain helpers
        assert (getattr(numeric, "cob_cmp_align_u%d_binary" % bits)
                is getattr(numeric, "cob_cmp_u%d_binary" % bits))
        assert (getattr(numeric, "cob_add_align_s%d_binary" % bits)
                is getattr(numeric, "cob_add_s%d_binary" % bits))
        assert (getattr(numeric, "cob_sub_align_u%d_binary" % bits)
                is getattr(numeric, "cob_sub_u%d_binary" % bits))

    def test_add_binary_wraps(self):
        # 8-bit add wraps mod 256 (two's-complement low byte)
        buf = bytearray([250])
        numeric.cob_add_u8_binary(buf, 10)
        assert buf[0] == 4                        # 260 mod 256


# ===========================================================================
# NUMERIC-EDITED dispatch + extended cob_cmp_packed coverage
# ===========================================================================
class TestEditedDispatch:
    def test_get_field_numeric_edited_renders_and_moves(self, monkeypatch):
        # move.py is built in parallel; intercept the deferred MOVE so this test
        # validates only that get_field renders the correct DISPLAY temporary
        # and forwards it to the move subsystem.
        captured = {}

        def fake_move(src, dst):
            captured["src"] = bytes(common.COB_FIELD_DATA(src))
            captured["dst"] = dst

        monkeypatch.setattr(common, "_lazy_move", fake_move)
        edited = mk(T_EDITED, 5, 2, 0, 8)
        rc = numeric.cob_decimal_get_field(numeric.cob_decimal(12345, 2), edited, 0)
        assert rc == common.cob_exception_code
        # the rendered temp holds the right-justified zoned magnitude digits
        assert captured["src"] == b"12345"
        assert captured["dst"] is edited


class TestCmpPackedExtended:
    def test_both_negative(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, -500)
        assert numeric.cob_cmp_packed(f, -500) == 0
        assert numeric.cob_cmp_packed(f, -400) < 0   # -500 < -400
        assert numeric.cob_cmp_packed(f, -600) > 0   # -500 > -600

    def test_sign_shortcut(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 5)
        assert numeric.cob_cmp_packed(f, -1) == 1     # positive field vs negative n
        numeric.cob_set_packed_int(f, -5)
        assert numeric.cob_cmp_packed(f, 1) == -1     # negative field vs positive n

    def test_even_digit_field(self):
        # even digit count exercises the high-nibble pad mask branch
        f = mk(T_PACK, 8, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 1234)
        assert numeric.cob_cmp_packed(f, 1234) == 0
        assert numeric.cob_cmp_packed(f, 1000) > 0

    def test_lastval_cache_reuse(self):
        # calling twice with the same n exercises the cached-image fast path
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 777)
        assert numeric.cob_cmp_packed(f, 777) == 0
        assert numeric.cob_cmp_packed(f, 777) == 0

    def test_zero(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 0)
        assert numeric.cob_cmp_packed(f, 0) == 0


class TestDisplayArithEdge:
    def test_display_add_int_overflow_keeps_original(self):
        # adding past the field capacity raises overflow and leaves the value
        # unchanged (numeric.c save/restore on overflow)
        f = mk(T_DISP, 3, 0, F_SIGN, 3)
        enc(f, 999, 0)
        rc = numeric.cob_display_add_int(f, 100)      # 1099 doesn't fit in 3 digits
        assert rc == EC_OVERFLOW
        assert dec(f) == (999, 0)

    def test_add_int_zero_noop(self):
        f = mk(T_DISP, 5, 0, F_SIGN, 5)
        enc(f, 123, 0)
        assert numeric.cob_add_int(f, 0) == 0
        assert dec(f) == (123, 0)

    def test_sub_int_zero_noop(self):
        f = mk(T_DISP, 5, 0, F_SIGN, 5)
        enc(f, 123, 0)
        assert numeric.cob_sub_int(f, 0) == 0
        assert dec(f) == (123, 0)
