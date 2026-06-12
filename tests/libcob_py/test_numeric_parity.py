"""Byte-for-byte numeric PARITY harness (AAP sections 0.6.2 / 0.7.1, Rule R6).

Numeric parity is the critical non-functional requirement of the C->Python
backend refactor: a value stored under any ``USAGE`` / ``PICTURE`` / ``COMP``
combination must occupy the **exact same bytes** the original GnuCOBOL C runtime
(``libcob/numeric.c``) produced.  This module is the runnable parity gate.

Two layers of parity are asserted:

1. **Byte-pattern parity (always runnable).**  For at least one case per COMP
   type - DISPLAY (zoned), COMP/COMP-4 (big-endian binary), COMP-5 (native
   binary), COMP-X (unsigned binary), and COMP-3/PACKED-DECIMAL - a value is
   stored through :func:`libcob_py.numeric.cob_decimal_get_field` and the
   resulting bytes are compared against an INDEPENDENTLY computed expected
   pattern derived directly from the documented COBOL storage encoding (NOT
   from the runtime under test).  Per Rule R6 there are at least three COMP-3
   cases exercising **truncation**, **rounding**, and the **overflow boundary**
   (the three behaviours called out in AAP section 0.6.2).  Any mismatch is a
   blocking regression.

2. **Dual-compiler end-to-end parity (optional).**  When both the original C
   ``cobc`` and the refactored Python ``cobc`` are available (env vars
   ``COBC_ORIG`` / ``COBC_PY``; see ``conftest.run_cobc`` /
   ``requires_dual_cobc``) the harness compiles a reference program with each
   and asserts byte-for-byte-equal program output, exactly as AAP section 0.6.2
   specifies.  It SKIPS cleanly in the source-only / pre-build tree so the
   byte-pattern layer above remains the always-on gate.

Standard library only; ``pytest`` is a development-only framework (AAP 0.5).
"""
import struct
import sys

import pytest

libcob_py = pytest.importorskip("libcob_py")
common = pytest.importorskip("libcob_py.common")
numeric = pytest.importorskip("libcob_py.numeric")

HAVE_SIGN = common.COB_FLAG_HAVE_SIGN
BINARY_SWAP = common.COB_FLAG_BINARY_SWAP        # set => big-endian storage
REAL_BINARY = common.COB_FLAG_REAL_BINARY        # COMP-5 native order
STORE_ROUND = common.COB_STORE_ROUND
KEEP_ON_OVERFLOW = common.COB_STORE_KEEP_ON_OVERFLOW


# ---------------------------------------------------------------------------
# USAGE field builders (layout per cobc/field.c; sizes per the COBOL encoding)
# ---------------------------------------------------------------------------
def dec(value, scale=0):
    return numeric.cob_decimal(value, scale)


def display_field(digits, scale=0, signed=False):
    flags = HAVE_SIGN if signed else 0
    attr = common.cob_field_attr(type=common.COB_TYPE_NUMERIC_DISPLAY,
                                 digits=digits, scale=scale, flags=flags, pic=None)
    return common.cob_field(size=digits, data=bytearray(digits), attr=attr)


def comp_field(size, digits, scale=0, signed=True, big_endian=True, real_binary=False):
    flags = 0
    if signed:
        flags |= HAVE_SIGN
    if big_endian:
        flags |= BINARY_SWAP          # COMP / COMP-4 store big-endian
    if real_binary:
        flags |= REAL_BINARY          # COMP-5 native order
    attr = common.cob_field_attr(type=common.COB_TYPE_NUMERIC_BINARY,
                                 digits=digits, scale=scale, flags=flags, pic=None)
    return common.cob_field(size=size, data=bytearray(size), attr=attr)


def packed_field(digits, scale=0, signed=True):
    size = digits // 2 + 1
    flags = HAVE_SIGN if signed else 0
    attr = common.cob_field_attr(type=common.COB_TYPE_NUMERIC_PACKED,
                                 digits=digits, scale=scale, flags=flags, pic=None)
    return common.cob_field(size=size, data=bytearray(size), attr=attr)


def store(d, f, opt=STORE_ROUND):
    common.cob_exception_code = 0
    rc = numeric.cob_decimal_get_field(d, f, opt)
    return rc


# ===========================================================================
# DISPLAY (zoned decimal) - one byte per digit
# ===========================================================================
def test_parity_display_unsigned():
    f = display_field(5, scale=0, signed=False)
    store(dec(12345, 0), f)
    assert bytes(f.data) == b"12345"  # 0x31..0x35


def test_parity_display_scaled():
    # 123.45 in PIC 9(3)V99 -> the five digit bytes "12345" (decimal point implied)
    f = display_field(5, scale=2, signed=False)
    store(dec(12345, 2), f)
    assert bytes(f.data) == b"12345"


def test_parity_display_signed_roundtrip():
    # Signed DISPLAY overpunch depends on the module sign mode; assert the
    # stored bytes decode back to the exact signed value (internal parity).
    f = display_field(3, scale=0, signed=True)
    store(dec(-123, 0), f)
    back = numeric.cob_decimal()
    numeric.cob_decimal_set_field(back, f)
    assert back.value == -123


# ===========================================================================
# COMP / COMP-4 / BINARY - big-endian two's complement
# ===========================================================================
def test_parity_comp_2byte_big_endian():
    f = comp_field(2, digits=4, scale=0, signed=True, big_endian=True)
    store(dec(1234, 0), f)
    assert bytes(f.data) == (1234).to_bytes(2, "big", signed=True)  # b'\x04\xd2'


def test_parity_comp_4byte_big_endian():
    f = comp_field(4, digits=9, scale=0, signed=True, big_endian=True)
    store(dec(123456789, 0), f)
    assert bytes(f.data) == (123456789).to_bytes(4, "big", signed=True)


def test_parity_comp_negative_big_endian():
    f = comp_field(2, digits=4, scale=0, signed=True, big_endian=True)
    store(dec(-1234, 0), f)
    assert bytes(f.data) == (-1234).to_bytes(2, "big", signed=True)  # two's complement


# ===========================================================================
# COMP-5 - native byte order (little-endian on this host)
# ===========================================================================
def test_parity_comp5_native_order():
    f = comp_field(2, digits=4, scale=0, signed=True, big_endian=False, real_binary=True)
    store(dec(1234, 0), f)
    assert bytes(f.data) == (1234).to_bytes(2, sys.byteorder, signed=True)


# ===========================================================================
# COMP-X - unsigned binary (big-endian)
# ===========================================================================
def test_parity_compx_unsigned():
    f = comp_field(2, digits=4, scale=0, signed=False, big_endian=True)
    store(dec(0x1234, 0), f)  # 4660
    assert bytes(f.data) == (4660).to_bytes(2, "big", signed=False)  # b'\x12\x34'


# ===========================================================================
# COMP-3 / PACKED-DECIMAL - >=3 cases per Rule R6: truncation, rounding,
# overflow boundary (AAP section 0.6.2).  Encoding: two BCD digits per byte,
# units digit in the HIGH nibble of the last byte, sign in its LOW nibble
# (0x0C +, 0x0D -, 0x0F unsigned); field size = digits // 2 + 1.
# ===========================================================================
def test_parity_packed_positive():
    # +12345 in PIC S9(3)V99 COMP-3 -> 0x12 0x34 0x5C
    f = packed_field(5, scale=2, signed=True)
    store(dec(12345, 2), f)
    assert bytes(f.data) == b"\x12\x34\x5c"


def test_parity_packed_negative():
    # -123 in PIC S9(3) COMP-3 (digits 3 -> 2 bytes) -> 0x12 0x3D
    f = packed_field(3, scale=0, signed=True)
    store(dec(-123, 0), f)
    assert bytes(f.data) == b"\x12\x3d"


def test_parity_packed_unsigned_sign_nibble():
    # 123 in PIC 9(3) COMP-3 unsigned -> sign nibble 0x0F -> 0x12 0x3F
    f = packed_field(3, scale=0, signed=False)
    store(dec(123, 0), f)
    assert bytes(f.data) == b"\x12\x3f"


def test_parity_packed_even_digits_pad():
    # +1234 in PIC S9(4) COMP-3 (even digits -> leading zero pad nibble; 3 bytes)
    #   -> 0x01 0x23 0x4C
    f = packed_field(4, scale=0, signed=True)
    store(dec(1234, 0), f)
    assert bytes(f.data) == b"\x01\x23\x4c"


def test_parity_packed_comp3_truncation():
    # R6 case 1 - TRUNCATION: store 123.456 into S9(3)V99 with NO ROUNDED.
    # Truncates toward zero to 123.45 -> 0x12 0x34 0x5C.
    f = packed_field(5, scale=2, signed=True)
    store(dec(123456, 3), f, opt=0)            # opt 0 => no COB_STORE_ROUND
    assert bytes(f.data) == b"\x12\x34\x5c"


def test_parity_packed_comp3_rounding():
    # R6 case 2 - ROUNDING: same 123.456 WITH ROUNDED (HALF_UP) -> 123.46
    #   -> 0x12 0x34 0x6C.
    f = packed_field(5, scale=2, signed=True)
    store(dec(123456, 3), f, opt=STORE_ROUND)
    assert bytes(f.data) == b"\x12\x34\x6c"


def test_parity_packed_comp3_overflow_boundary():
    # R6 case 3 - OVERFLOW BOUNDARY: 1234567 (7 digits) into S9(5) COMP-3 with
    # KEEP_ON_OVERFLOW -> EC-SIZE-OVERFLOW raised, store abandoned (ON SIZE
    # ERROR).  Field bytes remain at their initial zero state.
    f = packed_field(5, scale=0, signed=True)
    original = bytes(f.data)
    rc = store(dec(1234567, 0), f, opt=KEEP_ON_OVERFLOW)
    assert rc == common.cob_exception_code
    assert common.cob_exception_code == 0x1004  # EC-SIZE-OVERFLOW
    assert bytes(f.data) == original            # store abandoned


def test_parity_packed_comp3_rounding_half_up_boundary():
    # Extra COMP-3 rounding case: 0.005 -> scale 2 HALF_UP -> 0.01 in S9V99.
    f = packed_field(3, scale=2, signed=True)   # digits 3 -> 2 bytes
    store(dec(5, 3), f, opt=STORE_ROUND)         # 0.005
    # 001 with positive sign -> 0x00 0x1C
    assert bytes(f.data) == b"\x00\x1c"


# ===========================================================================
# ROUNDED MODE map parity (AAP section 0.6.2 table)
# ===========================================================================
@pytest.mark.parametrize("mode_name,expected", [
    ("NEAREST-AWAY-FROM-ZERO", 13),   # 12.5 -> 13 (HALF_UP)
    ("NEAREST-EVEN", 12),             # 12.5 -> 12 (banker's)
    ("TRUNCATION", 12),               # 12.5 -> 12 (toward zero)
    ("TOWARD-GREATER", 13),           # 12.5 -> 13 (ceiling)
    ("TOWARD-LESSER", 12),            # 12.5 -> 12 (floor)
    ("AWAY-FROM-ZERO", 13),           # 12.5 -> 13 (up)
])
def test_parity_rounding_modes(mode_name, expected):
    mode = numeric.ROUND_MODE_MAP[mode_name]
    f = display_field(2, scale=0, signed=False)
    numeric.cob_decimal_get_field(dec(125, 1), f, STORE_ROUND, rounding=mode)
    assert bytes(f.data) == b"%02d" % expected


# ===========================================================================
# EC-SIZE trap-context parity (AAP section 0.6.2 trap mechanism)
# ===========================================================================
def test_parity_zero_divide_maps_to_ec_size():
    a = dec(1, 0)
    b = dec(0, 0)
    common.cob_exception_code = 0
    numeric.cob_decimal_div(a, b)
    assert common.cob_exception_code == 0x1007  # EC-SIZE-ZERO-DIVIDE


def test_parity_inexact_not_promoted():
    # A legal ROUNDED store rounds (Inexact) but must NOT raise EC-SIZE.
    f = display_field(4, scale=2, signed=False)
    common.cob_exception_code = 0
    numeric.cob_decimal_get_field(dec(1, 3), f, STORE_ROUND)   # 0.001 -> 0.00
    assert common.cob_exception_code == 0


def test_trap_context_overflow_maps_to_ec_size_overflow():
    # Directly exercise the AAP 0.6.2 trap context: a genuine decimal.Overflow
    # (adjusted exponent > Emax) is trapped and mapped to EC-SIZE-OVERFLOW.
    import decimal
    common.cob_exception_code = 0
    with numeric.cob_size_error_context(STORE_ROUND):
        decimal.Decimal("9E999999999") * decimal.Decimal("9E999999999")
    assert common.cob_exception_code == 0x1004  # EC-SIZE-OVERFLOW


def test_trap_context_division_by_zero_maps_to_ec_size_zero_divide():
    import decimal
    common.cob_exception_code = 0
    with numeric.cob_size_error_context(STORE_ROUND):
        decimal.Decimal(1) / decimal.Decimal(0)
    assert common.cob_exception_code == 0x1007  # EC-SIZE-ZERO-DIVIDE


def test_trap_context_rounding_mode_selected_by_opt():
    # COB_STORE_ROUND absent -> truncation; present -> HALF_UP. Verify the
    # context installs the documented rounding so direct decimal ops match.
    import decimal
    with numeric.cob_size_error_context(0) as ctx:           # no ROUND -> truncate
        assert ctx.rounding == numeric.COB_ROUND_TRUNCATION
    with numeric.cob_size_error_context(STORE_ROUND) as ctx:  # ROUND -> HALF_UP
        assert ctx.rounding == numeric.COB_ROUND_DEFAULT


# ===========================================================================
# Optional dual-compiler end-to-end parity (AAP 0.6.2, literal form).
# Skips cleanly until both the original C cobc and refactored Python cobc are
# available (COBC_ORIG / COBC_PY) - i.e. after the build-system migration.
# ===========================================================================
from tests.libcob_py.conftest import requires_dual_cobc, run_cobc, cobc_orig_path, cobc_py_path  # noqa: E402


@requires_dual_cobc
def test_dual_compiler_numeric_output_parity(tmp_path, template_dir):
    """Compile a reference program with original + refactored cobc; compare bytes."""
    src = template_dir / "numeric-dump.cob"
    orig = cobc_orig_path()
    refac = cobc_py_path()
    out_orig = tmp_path / "orig"
    out_refac = tmp_path / "refac"
    r1 = run_cobc(orig, ["-x", "-o", str(out_orig), str(src)])
    r2 = run_cobc(refac, ["-x", "-o", str(out_refac), str(src)])
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr
    import subprocess
    e1 = subprocess.run([str(out_orig)], capture_output=True, timeout=60)
    e2 = subprocess.run([str(out_refac)], capture_output=True, timeout=60)
    assert e1.stdout == e2.stdout, "byte-for-byte numeric output parity failed"
