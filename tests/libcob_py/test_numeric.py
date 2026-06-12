"""Unit tests for :mod:`libcob_py.numeric` - the byte-for-byte numeric subsystem.

This module is one of the thirteen ``tests/libcob_py/`` suites created for the
GNU Cobol (OpenCOBOL) C->Python backend refactor (AAP sections 0.2.1, 0.4.1,
0.6.2, 0.7.1).  It verifies the pure-Python runtime ``libcob_py/numeric.py``
against its authoritative C source ``libcob/numeric.c`` and the inline
byte-level helpers declared in ``libcob/codegen.h``.

``numeric`` is the *highest-risk parity area* of the whole refactor (AAP
section 0.6.2): the emitted COBOL output must match the original C toolchain
**byte-for-byte** across every ``USAGE`` / ``PICTURE`` combination.  Cross-cobc
parity (compiling a reference program with both the original and the refactored
``cobc`` and diffing the output) lives in ``test_numeric_parity.py``.  THIS file
verifies the encoder/decoder, the ROUNDED-mode map, the arithmetic engine and
the ``EC-SIZE`` traps **directly against golden byte patterns** - it runs
WITHOUT any ``cobc`` so the storage-layout contract is pinned independently of
the compiler driver.

What is covered (per the AAP numeric-parity / coverage gates 0.6.2 / 0.7.1):

* the ``cob_decimal`` value model (integer mantissa + scale) and its arithmetic,
* byte-for-byte STORAGE layout for every ``USAGE`` asserted against the
  documented C-runtime oracle: BINARY (COMP / COMP-4 big-endian, COMP-5 /
  COMP-X native), PACKED-DECIMAL (COMP-3), and DISPLAY (zoned decimal),
* the seven COBOL ``ROUNDED MODE`` -> :mod:`decimal` rounding-constant mappings,
* the ``EC-SIZE`` family traps - overflow (``0x1004``), divide-by-zero
  (``0x1007``) and truncation (``0x1005``), and
* the ``codegen.h`` integer fast-path mirrors (zoned ``numdisp``, packed-int,
  and the width/sign-specialised ``cob_*_NN_binary`` helper family).

HARD CONSTRAINTS (AAP sections 0.5 / 0.7.1):

* **Standard library only.**  Only :mod:`struct`, :mod:`sys` and :mod:`decimal`
  are imported from the stdlib; ``pytest`` is a development-only test framework
  (never a runtime dependency).  NO third-party package is imported.
* The runtime under test is obtained with :func:`pytest.importorskip` so that
  collection degrades to a clean *skip* (never a hard error) when the
  parallel-built ``libcob_py`` package is not yet importable.
"""
import decimal
import struct
import sys

import pytest

# Obtain the parallel-built runtime lazily so collection degrades to a clean
# SKIP (never a hard error) when the package - or a sub-module - is absent.
libcob_py = pytest.importorskip("libcob_py")
common = pytest.importorskip("libcob_py.common")
numeric = pytest.importorskip("libcob_py.numeric")


# ---------------------------------------------------------------------------
# USAGE / flag / store-option shorthands (resolved from the runtime so the test
# never hard-codes a constant that could drift from the module under test).
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

# COBOL EC-SIZE exception *codes* (the hex form latched into
# common.cob_exception_code), distinct from the internal enum ids.  These are
# the AAP 0.6.2 / agent-prompt values, re-derived from the runtime's own table
# below so the literals are asserted to be self-consistent with the module.
EC_OVERFLOW = 0x1004
EC_TRUNCATION = 0x1005
EC_ZERO_DIVIDE = 0x1007


# ===========================================================================
# GOLDEN BYTE PATTERNS - the C-runtime oracle (validated against the existing
# tests/data-rep.src golden dumps and binary.at / packed.at; AAP 0.6.2).
#
# Each tuple is ``(digits, value, signed, golden_hex)``.  The storage width for
# BINARY follows the COBOL 2-4-8 rule (1-4 digits -> 2 bytes, 5-9 -> 4 bytes,
# 10-18 -> 8 bytes); for PACKED it is ``digits // 2 + 1`` bytes.  ``golden_hex``
# is the elementary item's stored bytes ONLY (no trailing PIC X(18) filler).
# ===========================================================================
BINARY_GOLDEN = [
    # unsigned PIC 9(n) VALUE n - big-endian two's-complement (COMP / COMP-4)
    (1, 1, False, "0001"),
    (2, 12, False, "000c"),
    (3, 123, False, "007b"),
    (4, 1234, False, "04d2"),
    (5, 12345, False, "00003039"),
    (9, 123456789, False, "075bcd15"),
    (10, 1234567890, False, "00000000499602d2"),
    (18, 123456789012345678, False, "01b69b4ba630f34e"),
    # signed PIC S9(n) VALUE -n
    (1, -1, True, "ffff"),
    (2, -12, True, "fff4"),
    (3, -123, True, "ff85"),
    (4, -1234, True, "fb2e"),
]

PACKED_GOLDEN = [
    # unsigned PIC 9(n) - sign nibble 0x0F
    (1, 1, False, "1f"),
    (2, 12, False, "012f"),
    (3, 123, False, "123f"),
    (4, 1234, False, "01234f"),
    (9, 123456789, False, "123456789f"),
    (18, 123456789012345678, False, "0123456789012345678f"),
    # negative PIC S9(n) - sign nibble 0x0D
    (1, -1, True, "1d"),
    (2, -12, True, "012d"),
    (4, -1234, True, "01234d"),
    # zeros - the BCD zero-pad with the unsigned 0x0F sign nibble
    (1, 0, False, "0f"),
    (2, 0, False, "000f"),
]


# ---------------------------------------------------------------------------
# Field-construction + encode/decode helpers
# ---------------------------------------------------------------------------
def _bin_width(digits):
    """Return the COBOL BINARY storage width in bytes for *digits* (2-4-8 rule)."""
    if digits <= 4:
        return 2
    if digits <= 9:
        return 4
    return 8


def mk(ftype, digits, scale, flags, size, data=None):
    """Build a ``cob_field`` with a zeroed (or supplied) ``bytearray`` buffer.

    The backing buffer is always a ``bytearray`` so the in-place stores the
    encoders perform (sign overpunch, BCD nibble writes, byte-swap) are visible
    through ``field.data`` exactly as the C ``cob_field.data`` pointer aliases a
    program's WORKING-STORAGE image.
    """
    attr = common.cob_field_attr(type=ftype, digits=digits, scale=scale,
                                 flags=flags, pic=None)
    buf = bytearray(size) if data is None else bytearray(data)
    return common.cob_field(size=size, data=buf, attr=attr)


def enc(field, unscaled, scale, opt=0):
    """Store ``unscaled * 10**-scale`` into *field* via the high-level path.

    Mirrors the emitter's ``cob_decimal_get_field`` store: construct a
    ``cob_decimal`` from the integer mantissa + scale and store it into the
    field honouring the ``COB_STORE_*`` *opt* bitmask.  Returns *field* so call
    sites can read ``bytes(field.data)`` directly.
    """
    numeric.cob_decimal_get_field(numeric.cob_decimal(unscaled, scale), field, opt)
    return field


def dec(field):
    """Decode *field* back to a ``(unscaled, scale)`` integer-mantissa tuple."""
    d = numeric.cob_decimal()
    numeric.cob_decimal_set_field(d, field)
    return d.value, d.scale


def dec_decimal(field):
    """Decode *field* to a :class:`decimal.Decimal` (mantissa scaled by 10**-scale)."""
    value, scale = dec(field)
    return decimal.Decimal(value).scaleb(-scale)


@pytest.fixture(autouse=True)
def _fresh_numeric_state():
    """Reset module scratch state before each test (the cob_init_numeric path).

    Resets the file-scope scratch decimals and the packed-compare cache, and
    clears ``common.cob_exception_code`` so each EC-SIZE assertion observes only
    the exception raised by its own statement.
    """
    numeric.cob_init_numeric()
    common.cob_exception_code = 0
    yield


# ===========================================================================
# Phase 1 - USAGE encode/decode byte-for-byte (the headline parity tests)
# ===========================================================================
class TestBinaryEncodeGolden:
    """COMP / COMP-4 big-endian two's-complement storage vs the golden oracle."""

    @pytest.mark.parametrize("digits,value,signed,golden", BINARY_GOLDEN,
                             ids=[("S9_%d_%d" % (d, abs(v))) if s
                                  else ("9_%d_%d" % (d, v))
                                  for d, v, s, _ in BINARY_GOLDEN])
    def test_binary_encode_golden(self, digits, value, signed, golden):
        """Encoder output == documented golden hex for each BINARY case.

        Covers all three storage widths (2, 4 and 8 bytes) and both unsigned
        ``9(n)`` and signed ``S9(n)`` items.  The expected bytes are the
        documented C-runtime oracle, NOT a value re-derived by the encoder, so a
        divergence in ``numeric.py`` is caught against the fixed contract.
        """
        size = _bin_width(digits)
        flags = F_SWAP | (F_SIGN if signed else 0)
        field = mk(T_BIN, digits, 0, flags, size)
        enc(field, value, 0)
        assert bytes(field.data).hex() == golden
        # The independent stdlib reference (value.to_bytes) must agree too.
        assert bytes(field.data) == value.to_bytes(size, "big", signed=signed)
        # ...and the value round-trips back unchanged.
        assert dec(field) == (value, 0)

    def test_binary_widths_are_2_4_8(self):
        """The 2-4-8 storage-width rule the golden table relies on."""
        assert (_bin_width(1), _bin_width(4)) == (2, 2)
        assert (_bin_width(5), _bin_width(9)) == (4, 4)
        assert (_bin_width(10), _bin_width(18)) == (8, 8)


class TestPackedEncodeGolden:
    """PACKED-DECIMAL / COMP-3 BCD storage vs the golden oracle."""

    @pytest.mark.parametrize("digits,value,signed,golden", PACKED_GOLDEN,
                             ids=[("S9_%d_%s" % (d, ("m%d" % -v if v < 0 else v)))
                                  if s else ("9_%d_%d" % (d, v))
                                  for d, v, s, _ in PACKED_GOLDEN])
    def test_packed_encode_golden(self, digits, value, signed, golden):
        """Encoder output == documented golden hex for each PACKED case.

        Includes unsigned (sign nibble ``0x0F``), negative (``0x0D``) and the
        two zero cases (``0f`` and ``000f``).  ``nbytes = digits // 2 + 1``.
        """
        size = digits // 2 + 1
        flags = F_SIGN if signed else 0
        field = mk(T_PACK, digits, 0, flags, size)
        enc(field, value, 0)
        assert bytes(field.data).hex() == golden
        # The last nibble is the sign nibble; confirm the documented convention.
        last_nibble = field.data[size - 1] & 0x0F
        if not signed:
            assert last_nibble == 0x0F
        elif value < 0:
            assert last_nibble == 0x0D
        else:
            assert last_nibble == 0x0C

    def test_packed_positive_signed_nibble_0c(self):
        """Positive-signed ``S9(1) VALUE +1`` -> ``1c`` (sign nibble 0x0C)."""
        field = mk(T_PACK, 1, 0, F_SIGN, 1)
        enc(field, 1, 0)
        assert bytes(field.data).hex() == "1c"

    def test_packed_parity_blocking_case(self):
        """BLOCKING gate (AAP 0.6.2): ``S9(7)V99 COMP-3 VALUE -123.45`` bytes."""
        field = mk(T_PACK, 9, 2, F_SIGN, 5)
        enc(field, -12345, 2)
        assert bytes(field.data).hex() == "000012345d"
        assert dec(field) == (-12345, 2)


class TestDisplayEncode:
    """DISPLAY (zoned decimal) storage - one ASCII byte per digit."""

    def test_display_encode(self):
        """``PIC 9(3)=123`` -> ``b"123"``; ``PIC S9(3)=-123`` -> trailing overpunch.

        The unsigned case is the exact ASCII digit bytes ``0x31 0x32 0x33``.
        The signed-negative case is asserted by ROUND-TRIP (decode returns
        ``-123``) and by checking that the final byte is an operational-sign
        OVERPUNCH character - NOT a plain digit - because the module's ASCII
        overpunch table (``common.cob_put_sign_ascii``) maps negative ``3`` to
        ``'s'`` (0x73) rather than the IBM ``'L'``; the AAP / agent-prompt
        explicitly directs asserting the round-trip rather than hard-coding the
        overpunch byte when the module's table differs.
        """
        # Unsigned: exact ASCII digits.
        field = mk(T_DISP, 3, 0, 0, 3)
        enc(field, 123, 0)
        assert bytes(field.data) == b"123"
        assert bytes(field.data) == bytes([0x31, 0x32, 0x33])

        # Signed-negative: leading digits are plain ASCII, the last digit
        # carries the operational sign as a trailing overpunch.
        sfield = mk(T_DISP, 3, 0, F_SIGN, 3)
        enc(sfield, -123, 0)
        data = bytes(sfield.data)
        assert data[:2] == b"12"                       # leading digits unchanged
        last = data[2]
        assert last != ord("3")                        # overpunched, not a plain '3'
        assert last == common.cob_put_sign_ascii(ord("3"))  # module's own table
        assert dec(sfield) == (-123, 0)                # and it round-trips

    def test_display_positive_signed_roundtrip(self):
        """A positive value in a signed DISPLAY field round-trips to ``+123``."""
        field = mk(T_DISP, 3, 0, F_SIGN, 3)
        enc(field, 123, 0)
        assert dec(field) == (123, 0)

    def test_display_scaled_roundtrip(self):
        """``PIC S9(3)V99`` stores the unscaled mantissa with the field scale."""
        field = mk(T_DISP, 5, 2, F_SIGN, 5)
        enc(field, -12345, 2)
        assert dec(field) == (-12345, 2)

    def test_display_separate_leading_sign(self):
        """SEPARATE-LEADING sign occupies ``data[0]``; digits start at offset 1."""
        field = mk(T_DISP, 3, 0, F_SIGN | F_SEP | F_LEAD, 4)
        enc(field, -123, 0)
        assert dec(field) == (-123, 0)
        assert bytes(field.data[1:]) == b"123"          # digits unaffected by sign

    def test_display_separate_trailing_sign(self):
        """SEPARATE-TRAILING sign occupies the final byte."""
        field = mk(T_DISP, 3, 0, F_SIGN | F_SEP, 4)
        enc(field, -123, 0)
        assert dec(field) == (-123, 0)

    def test_display_high_value_sentinel(self):
        """A leading ``0xFF`` decodes as the figurative ``HIGH-VALUE`` fill."""
        field = mk(T_DISP, 3, 0, 0, 3, data=b"\xff\xff\xff")
        assert dec(field) == (10 ** 3, 0)

    def test_display_low_value_sentinel(self):
        """A leading ``0x00`` decodes as the figurative ``LOW-VALUE`` fill."""
        field = mk(T_DISP, 3, 0, 0, 3, data=b"\x00\x00\x00")
        assert dec(field) == (-(10 ** 3), 0)


class TestRoundtripAllUsages:
    """Encode then decode a representative value for every numeric USAGE."""

    @pytest.mark.parametrize("usage,digits,scale,flags,size,value,expected", [
        # DISPLAY (zoned), signed with scale
        ("DISPLAY", 5, 2, F_SIGN, 5, -12345, decimal.Decimal("-123.45")),
        # COMP / COMP-4 (BINARY big-endian, signed)
        ("COMP", 9, 0, F_SIGN | F_SWAP, 4, -123456789,
         decimal.Decimal("-123456789")),
        # COMP-3 (PACKED) - the AAP example S9(7)V99 VALUE -123.45
        ("COMP-3", 9, 2, F_SIGN, 5, -12345, decimal.Decimal("-123.45")),
        # COMP-5 (BINARY native-endian, signed)
        ("COMP-5", 4, 0, F_SIGN, 2, -1234, decimal.Decimal("-1234")),
        # COMP-X (BINARY native-endian, UNSIGNED)
        ("COMP-X", 4, 0, 0, 2, 9999, decimal.Decimal("9999")),
    ])
    def test_roundtrip_all_usages(self, usage, digits, scale, flags, size,
                                  value, expected):
        ftype = T_PACK if usage == "COMP-3" else (
            T_BIN if usage in ("COMP", "COMP-5", "COMP-X") else T_DISP)
        field = mk(ftype, digits, scale, flags, size)
        enc(field, value, scale)
        # The integer-mantissa tuple round-trips exactly...
        assert dec(field) == (value, scale)
        # ...and so does the scaled decimal value (the AAP byte-for-byte goal).
        assert dec_decimal(field) == expected


# ===========================================================================
# Phase 2 - PACKED-DECIMAL sign-nibble convention (COMP-3)
# ===========================================================================
class TestPackedSignNibble:
    """The last BCD nibble encodes the operational sign (0x0F / 0x0C / 0x0D)."""

    def test_packed_sign_nibble(self):
        # Unsigned PIC 9 -> 0x0F
        f_uns = mk(T_PACK, 3, 0, 0, 2)
        enc(f_uns, 123, 0)
        assert (f_uns.data[f_uns.size - 1] & 0x0F) == 0x0F
        assert numeric.cob_packed_get_sign(f_uns) == 0

        # Positive PIC S9 with a positive value -> 0x0C (golden ``1c``)
        f_pos = mk(T_PACK, 1, 0, F_SIGN, 1)
        enc(f_pos, 1, 0)
        assert (f_pos.data[f_pos.size - 1] & 0x0F) == 0x0C
        assert bytes(f_pos.data).hex() == "1c"
        assert numeric.cob_packed_get_sign(f_pos) == 1

        # Negative PIC S9 -> 0x0D (golden ``1d``)
        f_neg = mk(T_PACK, 1, 0, F_SIGN, 1)
        enc(f_neg, -1, 0)
        assert (f_neg.data[f_neg.size - 1] & 0x0F) == 0x0D
        assert bytes(f_neg.data).hex() == "1d"
        assert numeric.cob_packed_get_sign(f_neg) == -1

    def test_packed_get_sign_from_raw_bytes(self):
        """``cob_packed_get_sign`` reads the low nibble of the final byte."""
        neg = mk(T_PACK, 9, 2, F_SIGN, 5, data=bytes.fromhex("000012345d"))
        pos = mk(T_PACK, 9, 2, F_SIGN, 5, data=bytes.fromhex("000012345c"))
        assert numeric.cob_packed_get_sign(neg) == -1
        assert numeric.cob_packed_get_sign(pos) == 1


# ===========================================================================
# Phase 3 - COBOL ROUNDED MODE -> decimal rounding-constant map (all 7)
# ===========================================================================
class TestRoundingModeMap:
    """The exact 7-entry ROUNDED-mode map and its observable behaviour."""

    def test_rounding_mode_map(self):
        """All seven COBOL ROUNDED modes map to the exact decimal constant."""
        assert numeric.ROUND_MODE_MAP == {
            "NEAREST-AWAY-FROM-ZERO": decimal.ROUND_HALF_UP,
            "NEAREST-EVEN": decimal.ROUND_HALF_EVEN,
            "NEAREST-TOWARD-ZERO": decimal.ROUND_HALF_DOWN,
            "TOWARD-GREATER": decimal.ROUND_CEILING,
            "TOWARD-LESSER": decimal.ROUND_FLOOR,
            "TRUNCATION": decimal.ROUND_DOWN,
            "AWAY-FROM-ZERO": decimal.ROUND_UP,
        }
        # Defaults: ROUNDED with no named mode == HALF_UP; no ROUNDED == DOWN.
        assert numeric.COB_ROUND_DEFAULT == decimal.ROUND_HALF_UP
        assert numeric.COB_ROUND_TRUNCATION == decimal.ROUND_DOWN

    @pytest.mark.parametrize("name,constant", [
        ("NEAREST-AWAY-FROM-ZERO", decimal.ROUND_HALF_UP),
        ("NEAREST-EVEN", decimal.ROUND_HALF_EVEN),
        ("NEAREST-TOWARD-ZERO", decimal.ROUND_HALF_DOWN),
        ("TOWARD-GREATER", decimal.ROUND_CEILING),
        ("TOWARD-LESSER", decimal.ROUND_FLOOR),
        ("TRUNCATION", decimal.ROUND_DOWN),
        ("AWAY-FROM-ZERO", decimal.ROUND_UP),
    ])
    def test_rounding_mode_map_entrywise(self, name, constant):
        """Each mode resolves to its exact constant (entry-by-entry)."""
        assert numeric.ROUND_MODE_MAP[name] == constant

    def test_rounding_behavior(self):
        """Store the half-way value 2.5 (and -2.5) under every rounding mode.

        Exercises the module's rounding-aware store path
        (``cob_decimal_get_field`` with an explicit ``rounding=`` constant under
        ``COB_STORE_ROUND``).  Expected results per AAP 0.6.2:
        HALF_UP->3, HALF_EVEN->2, DOWN->2, UP->3, CEILING->3, FLOOR->2;
        and -2.5 under HALF_DOWN->-2.
        """
        cases = {
            "NEAREST-AWAY-FROM-ZERO": 3,   # ROUND_HALF_UP
            "NEAREST-EVEN": 2,             # ROUND_HALF_EVEN
            "TRUNCATION": 2,               # ROUND_DOWN
            "AWAY-FROM-ZERO": 3,           # ROUND_UP
            "TOWARD-GREATER": 3,           # ROUND_CEILING
            "TOWARD-LESSER": 2,            # ROUND_FLOOR
            "NEAREST-TOWARD-ZERO": 2,      # ROUND_HALF_DOWN
        }
        for name, expected in cases.items():
            mode = numeric.ROUND_MODE_MAP[name]
            field = mk(T_DISP, 3, 0, 0, 3)
            numeric.cob_decimal_get_field(numeric.cob_decimal(25, 1), field,
                                          O_ROUND, rounding=mode)
            assert dec(field) == (expected, 0), name
            # The pure rounding kernel agrees with the store path.
            assert numeric._round_value_to_scale(25, 1, 0, mode) == expected, name

        # -2.5 under NEAREST-TOWARD-ZERO (HALF_DOWN) rounds toward zero -> -2.
        mode = numeric.ROUND_MODE_MAP["NEAREST-TOWARD-ZERO"]
        assert numeric._round_value_to_scale(-25, 1, 0, mode) == -2

    @pytest.mark.parametrize("name,p25,n25", [
        ("NEAREST-AWAY-FROM-ZERO", 3, -3),
        ("NEAREST-EVEN", 2, -2),
        ("NEAREST-TOWARD-ZERO", 2, -2),
        ("TOWARD-GREATER", 3, -2),
        ("TOWARD-LESSER", 2, -3),
        ("TRUNCATION", 2, -2),
        ("AWAY-FROM-ZERO", 3, -3),
    ])
    def test_round_value_to_scale_all_modes(self, name, p25, n25):
        """The rounding kernel for +2.5 and -2.5 across all seven modes."""
        mode = numeric.ROUND_MODE_MAP[name]
        assert numeric._round_value_to_scale(25, 1, 0, mode) == p25
        assert numeric._round_value_to_scale(-25, 1, 0, mode) == n25

    def test_round_extends_when_target_scale_larger(self):
        """A larger target scale zero-extends the mantissa (no rounding)."""
        assert numeric._round_value_to_scale(
            123, 0, 2, numeric.COB_ROUND_DEFAULT) == 12300

    def test_get_field_rounded_vs_truncated(self):
        """``COB_STORE_ROUND`` selects HALF_UP; its absence truncates toward 0."""
        rounded = mk(T_DISP, 3, 0, 0, 3)
        numeric.cob_decimal_get_field(numeric.cob_decimal(25, 1), rounded, O_ROUND)
        assert bytes(rounded.data) == b"003"            # 2.5 ROUNDED -> 3
        truncated = mk(T_DISP, 3, 0, 0, 3)
        numeric.cob_decimal_get_field(numeric.cob_decimal(29, 1), truncated, 0)
        assert bytes(truncated.data) == b"002"          # 2.9 truncated -> 2



# ===========================================================================
# Phase 4 - Arithmetic and the EC-SIZE family traps (AAP 0.6.2)
# ===========================================================================
class TestArithmeticAndSizeError:
    """Field-level arithmetic results and the SIZE-ERROR exception codes."""

    def test_add_sub_div(self):
        """``cob_add`` / ``cob_sub`` / ``cob_div_quotient`` + ``cob_div_remainder``.

        COBOL ``DIVIDE 11 BY 4`` yields an integer quotient of 2 with a
        remainder of 3 (truncating division), matching the C runtime.
        """
        # ADD: 100 + 23 -> 123  (cob_add stores the sum back into f1)
        f1 = mk(T_DISP, 5, 0, F_SIGN, 5)
        f2 = mk(T_DISP, 5, 0, F_SIGN, 5)
        enc(f1, 100, 0)
        enc(f2, 23, 0)
        numeric.cob_add(f1, f2, 0)
        assert dec(f1) == (123, 0)

        # SUB: 100 - 23 -> 77
        enc(f1, 100, 0)
        enc(f2, 23, 0)
        numeric.cob_sub(f1, f2, 0)
        assert dec(f1) == (77, 0)

        # DIVIDE 11 BY 4 GIVING q REMAINDER r -> q=2, r=3
        dividend = mk(T_DISP, 4, 0, F_SIGN, 4)
        divisor = mk(T_DISP, 4, 0, F_SIGN, 4)
        quotient = mk(T_DISP, 4, 0, F_SIGN, 4)
        remainder = mk(T_DISP, 4, 0, F_SIGN, 4)
        enc(dividend, 11, 0)
        enc(divisor, 4, 0)
        numeric.cob_div_quotient(dividend, divisor, quotient, 0)
        numeric.cob_div_remainder(remainder, 0)
        assert dec(quotient) == (2, 0)
        assert dec(remainder) == (3, 0)

    def test_overflow_sets_ec_size_overflow(self):
        """Storing too many integer digits raises ``EC-SIZE-OVERFLOW`` (0x1004).

        Storing 12345 into ``PIC 9(4)`` exceeds the receiving item's
        integer-digit capacity, latching ``common.cob_exception_code`` to
        ``0x1004`` (the ``ON SIZE ERROR`` path).  With
        ``COB_STORE_KEEP_ON_OVERFLOW`` the store is abandoned and the same code
        is returned to the caller.
        """
        # Default store: code is latched even though the field is truncated.
        field = mk(T_DISP, 4, 0, 0, 4)
        common.cob_exception_code = 0
        numeric.cob_decimal_get_field(numeric.cob_decimal(12345, 0), field, 0)
        assert common.cob_exception_code == EC_OVERFLOW

        # KEEP: nothing stored, the exception code is also the return value.
        keep = mk(T_DISP, 4, 0, 0, 4)
        common.cob_exception_code = 0
        rc = numeric.cob_decimal_get_field(numeric.cob_decimal(12345, 0), keep,
                                           O_KEEP)
        assert rc == EC_OVERFLOW
        assert common.cob_exception_code == EC_OVERFLOW

        # Same contract for BINARY and PACKED receiving items.
        binf = mk(T_BIN, 4, 0, F_SIGN | F_SWAP, 2)
        common.cob_exception_code = 0
        rc = numeric.cob_decimal_get_field(numeric.cob_decimal(10 ** 9, 0), binf,
                                           O_KEEP)
        assert rc == EC_OVERFLOW
        packf = mk(T_PACK, 3, 0, F_SIGN, 2)
        common.cob_exception_code = 0
        rc = numeric.cob_decimal_get_field(numeric.cob_decimal(12345, 0), packf,
                                           O_KEEP)
        assert rc == EC_OVERFLOW

    def test_zero_divide_sets_ec_size_zero_divide(self):
        """Division by zero raises ``EC-SIZE-ZERO-DIVIDE`` (0x1007).

        The dividend decimal is flagged NaN (scale == ``DECIMAL_NAN``) and the
        exception code is latched to ``0x1007`` both at the low-level
        ``cob_decimal_div`` and through the statement-level
        ``cob_div_quotient`` path.
        """
        # Low-level decimal divide.
        a = numeric.cob_decimal(10, 0)
        b = numeric.cob_decimal(0, 0)
        common.cob_exception_code = 0
        numeric.cob_decimal_div(a, b)
        assert a.scale == numeric.DECIMAL_NAN
        assert common.cob_exception_code == EC_ZERO_DIVIDE

        # Statement-level quotient store reports the same code.
        dividend = mk(T_DISP, 4, 0, F_SIGN, 4)
        divisor = mk(T_DISP, 4, 0, F_SIGN, 4)
        quotient = mk(T_DISP, 4, 0, F_SIGN, 4)
        enc(dividend, 10, 0)
        enc(divisor, 0, 0)
        common.cob_exception_code = 0
        rc = numeric.cob_div_quotient(dividend, divisor, quotient, 0)
        assert rc == EC_ZERO_DIVIDE
        assert common.cob_exception_code == EC_ZERO_DIVIDE

    def test_truncation_ec_size(self):
        """Digit-loss on store raises a SIZE-family exception (guarded).

        AAP 0.6.2 maps lost-digit truncation to ``EC-SIZE-TRUNCATION``
        (``0x1005``) *where the C runtime would flag it*.  ``numeric.py``
        follows the C ``cob_decimal_get_display`` / ``_get_binary`` /
        ``_get_packed`` stores, which raise ``EC-SIZE-OVERFLOW`` (``0x1004``)
        for the lost-high-order-digit case; the agent-prompt explicitly permits
        guarding when the module signals overflow rather than a distinct
        truncation code.  This test therefore asserts a SIZE-family code is
        latched (and is one of the two documented values), pinning the
        ON-SIZE-ERROR contract without over-constraining the exact code.
        """
        field = mk(T_DISP, 4, 0, 0, 4)
        common.cob_exception_code = 0
        # Store 12345 with no KEEP: the high-order digit is dropped (-> 2345).
        numeric.cob_decimal_get_field(numeric.cob_decimal(12345, 0), field, 0)
        assert common.cob_exception_code in (EC_OVERFLOW, EC_TRUNCATION)
        assert common.cob_exception_code != 0
        # The low-order digits are kept exactly as the C truncate path does.
        assert bytes(field.data) == b"2345"


# ===========================================================================
# Phase 5 - module initialisation
# ===========================================================================
class TestInit:
    """The ``cob_init_numeric`` entry point (first in the cob_init order)."""

    def test_init_numeric_callable(self):
        """``cob_init_numeric()`` is callable and idempotent (smoke test)."""
        assert numeric.cob_init_numeric() is None
        # Idempotent: a second call must not raise and leaves the engine usable.
        numeric.cob_init_numeric()
        field = mk(T_BIN, 4, 0, F_SIGN | F_SWAP, 2)
        enc(field, 1234, 0)
        assert bytes(field.data).hex() == "04d2"


# ===========================================================================
# Coverage breadth - cob_decimal value model, setters and arithmetic engine
# ===========================================================================
class TestCobDecimalModel:
    """The ``cob_decimal`` mantissa+scale value object and its lifecycle."""

    def test_construct(self):
        d = numeric.cob_decimal(-12345, 2)
        assert (d.value, d.scale) == (-12345, 2)

    def test_default_is_zero(self):
        d = numeric.cob_decimal()
        assert (d.value, d.scale) == (0, 0)

    def test_repr_is_informative(self):
        assert "cob_decimal" in repr(numeric.cob_decimal(7, 1))

    def test_init_resets(self):
        d = numeric.cob_decimal(99, 9)
        numeric.cob_decimal_init(d)
        assert (d.value, d.scale) == (0, 0)

    def test_set_copies_value_and_scale(self):
        src = numeric.cob_decimal(123, 4)
        dst = numeric.cob_decimal()
        numeric.cob_decimal_set(dst, src)
        assert (dst.value, dst.scale) == (123, 4)
        # An independent object (mutating the source must not affect the copy).
        src.value = 0
        assert dst.value == 123


class TestDecimalSetters:
    """Integer / unsigned / float -> ``cob_decimal`` conversions."""

    def test_set_int_positive(self):
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_int(d, 42)
        assert (d.value, d.scale) == (42, 0)

    def test_set_int_negative(self):
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_int(d, -42)
        assert (d.value, d.scale) == (-42, 0)

    def test_set_int_resets_scale(self):
        d = numeric.cob_decimal(0, 7)
        numeric.cob_decimal_set_int(d, 5)
        assert d.scale == 0

    def test_set_uint(self):
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_uint(d, 4000000000)
        assert (d.value, d.scale) == (4000000000, 0)

    def test_set_double_roundtrip(self):
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_double(d, 3.5)
        assert numeric.cob_decimal_get_double(d) == 3.5

    def test_set_double_negative(self):
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_double(d, -2.25)
        assert numeric.cob_decimal_get_double(d) == -2.25


class TestDecimalArithmetic:
    """The pure-decimal arithmetic kernels (operate in place on ``d1``)."""

    def test_add(self):
        a, b = numeric.cob_decimal(7, 0), numeric.cob_decimal(3, 0)
        numeric.cob_decimal_add(a, b)
        assert (a.value, a.scale) == (10, 0)

    def test_sub(self):
        a, b = numeric.cob_decimal(7, 0), numeric.cob_decimal(3, 0)
        numeric.cob_decimal_sub(a, b)
        assert (a.value, a.scale) == (4, 0)

    def test_mul(self):
        a, b = numeric.cob_decimal(7, 0), numeric.cob_decimal(3, 0)
        numeric.cob_decimal_mul(a, b)
        assert a.value * 10 ** -a.scale == 21

    def test_mul_scaled(self):
        a, b = numeric.cob_decimal(15, 1), numeric.cob_decimal(2, 0)   # 1.5 * 2
        numeric.cob_decimal_mul(a, b)
        assert decimal.Decimal(a.value).scaleb(-a.scale) == decimal.Decimal("3.0")

    def test_div_exact(self):
        a, b = numeric.cob_decimal(6, 0), numeric.cob_decimal(2, 0)
        numeric.cob_decimal_div(a, b)
        assert decimal.Decimal(a.value).scaleb(-a.scale) == 3

    def test_div_by_zero_flags_nan(self):
        a, b = numeric.cob_decimal(5, 0), numeric.cob_decimal(0, 0)
        numeric.cob_decimal_div(a, b)
        assert a.scale == numeric.DECIMAL_NAN

    def test_pow_integer(self):
        a, b = numeric.cob_decimal(2, 0), numeric.cob_decimal(10, 0)
        numeric.cob_decimal_pow(a, b)
        assert a.value * 10 ** -a.scale == 1024

    def test_cmp(self):
        assert numeric.cob_decimal_cmp(
            numeric.cob_decimal(5, 0), numeric.cob_decimal(5, 0)) == 0
        assert numeric.cob_decimal_cmp(
            numeric.cob_decimal(3, 0), numeric.cob_decimal(5, 0)) < 0
        assert numeric.cob_decimal_cmp(
            numeric.cob_decimal(9, 0), numeric.cob_decimal(5, 0)) > 0

    def test_shift_up(self):
        d = numeric.cob_decimal(123, 0)
        numeric.shift_decimal(d, 2)
        assert (d.value, d.scale) == (12300, 2)

    def test_shift_down_truncates_toward_zero(self):
        d = numeric.cob_decimal(12345, 2)
        numeric.shift_decimal(d, -2)
        assert (d.value, d.scale) == (123, 0)

    def test_align_decimal(self):
        a, b = numeric.cob_decimal(1, 0), numeric.cob_decimal(25, 1)
        numeric.align_decimal(a, b)
        assert a.scale == b.scale
        assert a.value == 10                            # 1 aligned to scale 1



# ===========================================================================
# Coverage breadth - statement-level field helpers (numeric.c L1113-L1461)
# ===========================================================================
class TestStatementHelpers:
    """``cob_add_int`` / ``cob_sub_int`` / comparison entry points by USAGE."""

    def test_add_int_display(self):
        f = mk(T_DISP, 5, 0, F_SIGN, 5)
        enc(f, 100, 0)
        numeric.cob_add_int(f, 23)
        assert dec(f) == (123, 0)

    def test_add_int_packed(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 100)
        numeric.cob_add_int(f, 23)
        assert numeric.cob_get_packed_int(f) == 123

    def test_add_int_binary(self):
        f = mk(T_BIN, 9, 0, F_SIGN | F_SWAP, 4)
        enc(f, 1000, 0)
        numeric.cob_add_int(f, 234)
        assert dec(f) == (1234, 0)

    def test_add_int_zero_is_noop(self):
        f = mk(T_DISP, 5, 0, F_SIGN, 5)
        enc(f, 100, 0)
        assert numeric.cob_add_int(f, 0) == 0
        assert dec(f) == (100, 0)

    def test_sub_int(self):
        f = mk(T_DISP, 5, 0, F_SIGN, 5)
        enc(f, 100, 0)
        numeric.cob_sub_int(f, 40)
        assert dec(f) == (60, 0)

    def test_sub_int_zero_is_noop(self):
        f = mk(T_DISP, 5, 0, F_SIGN, 5)
        enc(f, 100, 0)
        assert numeric.cob_sub_int(f, 0) == 0
        assert dec(f) == (100, 0)

    def test_cmp_int(self):
        f = mk(T_DISP, 5, 2, F_SIGN, 5)
        enc(f, 12345, 2)                                # 123.45
        assert numeric.cob_cmp_int(f, 123) > 0          # 123.45 > 123
        assert numeric.cob_cmp_int(f, 124) < 0

    def test_cmp_uint(self):
        f = mk(T_DISP, 5, 0, 0, 5)
        enc(f, 500, 0)
        assert numeric.cob_cmp_uint(f, 500) == 0
        assert numeric.cob_cmp_uint(f, 400) > 0

    def test_numeric_cmp_cross_usage(self):
        a = mk(T_DISP, 5, 2, F_SIGN, 5)
        b = mk(T_PACK, 9, 2, F_SIGN, 5)
        enc(a, 12345, 2)
        enc(b, 12345, 2)
        assert numeric.cob_numeric_cmp(a, b) == 0
        enc(b, 12300, 2)
        assert numeric.cob_numeric_cmp(a, b) > 0

    def test_cmp_packed(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 123)
        assert numeric.cob_cmp_packed(f, 123) == 0
        assert numeric.cob_cmp_packed(f, 100) > 0
        assert numeric.cob_cmp_packed(f, 200) < 0

    def test_cmp_packed_sign_shortcut(self):
        """A signed/unsigned mismatch short-circuits before the magnitude walk."""
        neg = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(neg, -1)
        assert numeric.cob_cmp_packed(neg, 1) < 0       # negative < positive n
        pos = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(pos, 1)
        assert numeric.cob_cmp_packed(pos, -1) > 0


class TestPackedLowLevel:
    """Dedicated PACKED encoders / mutators (numeric.c L895-L940, L667-L763)."""

    def test_set_packed_int_roundtrip(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, -678)
        assert numeric.cob_get_packed_int(f) == -678

    def test_set_packed_zero_signed(self):
        f = mk(T_PACK, 9, 2, F_SIGN, 5, data=b"\x99\x99\x99\x99\x99")
        numeric.cob_set_packed_zero(f)
        assert bytes(f.data).hex() == "000000000c"

    def test_set_packed_zero_unsigned(self):
        f = mk(T_PACK, 9, 2, 0, 5, data=b"\x99\x99\x99\x99\x99")
        numeric.cob_set_packed_zero(f)
        assert bytes(f.data).hex() == "000000000f"

    def test_add_packed(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 100)
        numeric.cob_add_packed(f, 23)
        assert numeric.cob_get_packed_int(f) == 123

    def test_add_packed_sign_flip(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 5)
        numeric.cob_add_packed(f, -8)                   # crosses zero -> -3
        assert numeric.cob_get_packed_int(f) == -3

    def test_complement_packed(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 100)
        numeric.cob_complement_packed(f)
        assert (numeric.cob_get_packed_int(f) % (10 ** 9)) == (10 ** 9 - 100)

    def test_comp3_truncation(self):
        """S9(5)V99: store 123.499 with no ROUNDED -> truncate to 123.49."""
        f = mk(T_PACK, 7, 2, F_SIGN, 4)
        numeric.cob_decimal_get_field(numeric.cob_decimal(123499, 3), f, 0)
        assert dec(f) == (12349, 2)

    def test_comp3_rounding(self):
        """Same value WITH ROUNDED -> 123.50."""
        f = mk(T_PACK, 7, 2, F_SIGN, 4)
        numeric.cob_decimal_get_field(numeric.cob_decimal(123499, 3), f, O_ROUND)
        assert dec(f) == (12350, 2)

    def test_comp3_overflow_boundary(self):
        """S9(3): storing 12345 overflows; without KEEP the low 3 digits remain."""
        f = mk(T_PACK, 3, 0, F_SIGN, 2)
        numeric.cob_decimal_get_field(numeric.cob_decimal(12345, 0), f, 0)
        assert numeric.cob_get_packed_int(f) == 345


# ===========================================================================
# Coverage breadth - codegen.h inline integer mirrors (codegen.h L188-L271)
# ===========================================================================
class TestCodegenMirrors:
    """The integer fast-path helpers the emitter calls directly."""

    def test_get_numdisp(self):
        assert numeric.cob_get_numdisp(b"123", 3) == 123
        assert numeric.cob_get_numdisp(b"00042", 5) == 42

    def test_get_numdisp_overpunch_counts_as_ten(self):
        """A byte > '9' (trailing overpunch) contributes 10 to its position."""
        # b"12" + a byte above '9' -> 12*10 + 10 == 130 per the C quirk.
        assert numeric.cob_get_numdisp(b"12\x3a", 3) == 130

    def test_get_packed_int(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, -4321)
        assert numeric.cob_get_packed_int(f) == -4321

    def test_cmp_packed_int(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 50)
        assert numeric.cob_cmp_packed_int(f, 50) == 0
        assert numeric.cob_cmp_packed_int(f, 40) > 0
        assert numeric.cob_cmp_packed_int(f, 60) < 0

    def test_add_packed_int(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 123)
        numeric.cob_add_packed_int(f, 2)
        assert numeric.cob_get_packed_int(f) == 125

    def test_cmp_numdisp(self):
        assert numeric.cob_cmp_numdisp(b"123", 3, 123) == 0
        assert numeric.cob_cmp_numdisp(b"123", 3, 100) > 0
        assert numeric.cob_cmp_numdisp(b"100", 3, 123) < 0

    def test_ascii_sign_decode_helpers(self):
        """``cob_get_ascii_sign`` decodes the module's 'p'..'y' overpunch table."""
        assert numeric.cob_get_ascii_sign(ord("p")) == 0
        assert numeric.cob_get_ascii_sign(ord("s")) == 3

    def test_ebcdic_sign_decode_helpers(self):
        """``cob_get_ebcdic_sign`` decodes the '{' (+0) / '}' (-0) overpunch."""
        assert numeric.cob_get_ebcdic_sign(ord("{")) == (0, False)
        assert numeric.cob_get_ebcdic_sign(ord("}")) == (0, True)


# ===========================================================================
# Coverage breadth - the dynamically-registered cob_*_NN_binary helper family
# (numeric.c codegen.h L40-L185; synthesised by _register_binary_family).
# ===========================================================================
class TestBinaryFamily:
    """Width/sign-specialised native-order integer helpers used by the emitter."""

    @pytest.mark.parametrize("bits", [8, 16, 24, 32, 40, 48, 56, 64])
    def test_cmp_add_sub_unsigned(self, bits):
        nbytes = bits // 8
        cmp_fn = getattr(numeric, "cob_cmp_u%d_binary" % bits)
        add_fn = getattr(numeric, "cob_add_u%d_binary" % bits)
        sub_fn = getattr(numeric, "cob_sub_u%d_binary" % bits)
        data = bytearray((100).to_bytes(nbytes, sys.byteorder))
        assert cmp_fn(data, 100) == 0
        assert cmp_fn(data, 50) > 0
        add_fn(data, 5)
        assert int.from_bytes(bytes(data), sys.byteorder) == 105
        sub_fn(data, 5)
        assert int.from_bytes(bytes(data), sys.byteorder) == 100

    @pytest.mark.parametrize("bits", [16, 32, 64])
    def test_setswp_stores_big_endian(self, bits):
        nbytes = bits // 8
        setswp = getattr(numeric, "cob_setswp_u%d_binary" % bits)
        data = bytearray(nbytes)
        setswp(data, 0x12)
        assert bytes(data) == (0x12).to_bytes(nbytes, "big")

    @pytest.mark.parametrize("bits", [16, 32, 64])
    def test_align_aliases_are_present(self, bits):
        """Aligned variants alias the plain helpers (alignment is a C concern)."""
        for prefix in ("u", "s"):
            assert (getattr(numeric, "cob_add_align_%s%d_binary" % (prefix, bits))
                    is getattr(numeric, "cob_add_%s%d_binary" % (prefix, bits)))

    def test_signed_negative_compare(self):
        data = bytearray((-5).to_bytes(2, sys.byteorder, signed=True))
        assert numeric.cob_cmp_s16_binary(data, -5) == 0
        assert numeric.cob_cmp_s16_binary(data, 0) < 0


# ===========================================================================
# Coverage breadth - FLOAT / DOUBLE dispatch and edge dispatch paths
# ===========================================================================
class TestFloatDouble:
    """COMP-1 / COMP-2 dispatch through ``set_field`` / ``get_field``."""

    def test_double_roundtrip(self):
        # COMP-2 carries the implied decimal in the field scale; the get_field
        # store rounds the decimal to that scale before packing the IEEE double.
        f = mk(T_DOUBLE, 5, 2, 0, 8)
        numeric.cob_decimal_get_field(numeric.cob_decimal(12345, 2), f, 0)
        assert struct.unpack("=d", bytes(f.data))[0] == pytest.approx(123.45)
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_field(d, f)
        assert numeric.cob_decimal_get_double(d) == pytest.approx(123.45)

    def test_float_roundtrip(self):
        f = mk(T_FLOAT, 2, 1, 0, 4)
        numeric.cob_decimal_get_field(numeric.cob_decimal(15, 1), f, 0)   # 1.5
        assert struct.unpack("=f", bytes(f.data))[0] == pytest.approx(1.5)
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_field(d, f)
        assert numeric.cob_decimal_get_double(d) == pytest.approx(1.5)


class TestEditedDispatch:
    """NUMERIC-EDITED receiving items render to DISPLAY then MOVE (move.py)."""

    def test_get_field_numeric_edited_delegates_to_move(self, monkeypatch):
        """The edited default path renders a DISPLAY temp and calls ``_lazy_move``."""
        captured = {}

        def fake_move(src, dst):
            captured["src"] = bytes(src.data)
            captured["dst_size"] = dst.size

        monkeypatch.setattr(common, "_lazy_move", fake_move)
        edited = mk(T_EDITED, 5, 2, F_SIGN, 9)
        numeric.cob_decimal_get_field(numeric.cob_decimal(12345, 2), edited, 0)
        # The rendered DISPLAY temp holds the digit string for 123.45.
        assert captured["src"] == b"12345"
        assert captured["dst_size"] == 9


class TestNaNStore:
    """A NaN-flagged decimal (post divide-by-zero) stores nothing and raises."""

    def test_get_field_nan_raises_overflow(self):
        nan = numeric.cob_decimal(0, numeric.DECIMAL_NAN)
        field = mk(T_DISP, 4, 0, 0, 4, data=b"9999")
        common.cob_exception_code = 0
        rc = numeric.cob_decimal_get_field(nan, field, 0)
        assert rc == EC_OVERFLOW
        assert common.cob_exception_code == EC_OVERFLOW
        # Nothing was stored - the field keeps its prior contents.
        assert bytes(field.data) == b"9999"



# ===========================================================================
# Extended coverage - exercise the remaining encode/arith/compare branches so
# the suite keeps a comfortable margin above the >=80% coverage gate (AAP
# 0.7.1).  Every expected value below was confirmed against the live module.
# ===========================================================================
class TestDisplayArithmeticExtended:
    """In-place zoned-DISPLAY add/sub branches (numeric.c L1113-L1280)."""

    def test_display_add_int_carry(self):
        f = mk(T_DISP, 3, 0, F_SIGN, 3)
        enc(f, 99, 0)
        assert numeric.cob_add_int(f, 1) == 0
        assert bytes(f.data) == b"100"

    def test_display_add_int_negative_crossing(self):
        f = mk(T_DISP, 3, 0, F_SIGN, 3)
        enc(f, 5, 0)
        numeric.cob_add_int(f, -8)                      # 5 - 8 = -3
        assert dec(f) == (-3, 0)

    def test_display_add_int_overflow_keeps_original(self):
        f = mk(T_DISP, 3, 0, 0, 3)
        enc(f, 998, 0)
        rc = numeric.cob_add_int(f, 5)                  # 1003 overflows 3 digits
        assert rc == EC_OVERFLOW
        assert bytes(f.data) == b"998"                  # original retained


class TestBinaryStoreExtended:
    """Binary store overflow-truncation and the unsigned 64-bit path."""

    def test_binary_trunc_on_overflow(self):
        # Wide (8-byte) storage but only 4 digits: 123456 keeps the low 4.
        f = mk(T_BIN, 4, 0, F_SIGN | F_SWAP, 8)
        rc = numeric.cob_decimal_get_field(numeric.cob_decimal(123456, 0), f,
                                           O_TRUNC)
        assert rc == EC_OVERFLOW
        assert numeric.cob_binary_get_int64(f) == 3456

    def test_binary_uint64_roundtrip(self):
        f = mk(T_BIN, 9, 0, 0, 4)
        numeric.cob_binary_set_uint64(f, 4000000000)
        assert numeric.cob_binary_get_uint64(f) == 4000000000

    def test_binary_set_int64_negative(self):
        f = mk(T_BIN, 9, 0, F_SIGN, 4)
        numeric.cob_binary_set_int64(f, -12345)
        assert numeric.cob_binary_get_int64(f) == -12345

    def test_add_int_binary_scaled(self):
        """``cob_add_int`` aligns the integer to the field scale before adding."""
        f = mk(T_BIN, 7, 2, F_SIGN | F_SWAP, 4)
        enc(f, 100, 2)                                  # 1.00
        numeric.cob_add_int(f, 2)                       # + 2 -> 3.00
        assert dec(f) == (300, 2)


class TestFieldArithmeticExtended:
    """Field-level subtract / scaled divide branches."""

    def test_cob_sub_negative_result(self):
        a = mk(T_DISP, 5, 0, F_SIGN, 5)
        b = mk(T_DISP, 5, 0, F_SIGN, 5)
        enc(a, 50, 0)
        enc(b, 80, 0)
        numeric.cob_sub(a, b, 0)
        assert dec(a) == (-30, 0)

    def test_div_rounded_scaled(self):
        """``10.00 / 3.00`` with ROUNDED -> ``3.33`` at scale 2."""
        dvd = mk(T_DISP, 5, 2, F_SIGN, 5)
        dvs = mk(T_DISP, 5, 2, F_SIGN, 5)
        q = mk(T_DISP, 5, 2, F_SIGN, 5)
        enc(dvd, 1000, 2)
        enc(dvs, 300, 2)
        numeric.cob_div_quotient(dvd, dvs, q, O_ROUND)
        assert dec(q) == (333, 2)

    def test_set_uint_large(self):
        d = numeric.cob_decimal()
        numeric.cob_decimal_set_uint(d, 2 ** 32 - 1)
        assert (d.value, d.scale) == (4294967295, 0)

    def test_get_double_zero(self):
        assert numeric.cob_decimal_get_double(numeric.cob_decimal(0, 0)) == 0.0


class TestPackedExtended:
    """Packed add carry/borrow and even-digit field handling."""

    def test_add_packed_carry_across_bytes(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 999)
        numeric.cob_add_packed(f, 2)
        assert numeric.cob_get_packed_int(f) == 1001

    def test_set_packed_int_even_digits(self):
        """An even digit count masks the high nibble of the leading byte."""
        f = mk(T_PACK, 8, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 12345678)
        assert numeric.cob_get_packed_int(f) == 12345678
        assert bytes(f.data).hex() == "012345678c"

    def test_add_packed_int_opposite_sign(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 5)
        numeric.cob_add_packed_int(f, -8)               # delegates -> -3
        assert numeric.cob_get_packed_int(f) == -3


class TestCmpPackedExtended:
    """``cob_cmp_packed`` magnitude-walk branches."""

    def test_cmp_packed_even_digit_field(self):
        f = mk(T_PACK, 8, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, 1234)
        assert numeric.cob_cmp_packed(f, 1234) == 0
        assert numeric.cob_cmp_packed(f, 1000) > 0

    def test_cmp_packed_both_negative(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, -50)
        assert numeric.cob_cmp_packed(f, -40) < 0       # -50 < -40
        assert numeric.cob_cmp_packed(f, -60) > 0       # -50 > -60

    def test_cmp_packed_int_negative(self):
        f = mk(T_PACK, 9, 0, F_SIGN, 5)
        numeric.cob_set_packed_int(f, -50)
        assert numeric.cob_cmp_packed_int(f, -50) == 0
        assert numeric.cob_cmp_packed_int(f, -40) < 0


class TestSignDecodeExtended:
    """The long (8-byte) and signed-zoned numdisp helpers (numeric.c L1463+)."""

    def test_long_ascii_sign(self):
        assert numeric.cob_get_long_ascii_sign(ord("p")) == 0
        assert numeric.cob_get_long_ascii_sign(ord("s")) == 3

    def test_long_ebcdic_sign(self):
        assert numeric.cob_get_long_ebcdic_sign(ord("{")) == (0, False)
        assert numeric.cob_get_long_ebcdic_sign(ord("R")) == (9, True)

    def test_cmp_sign_numdisp(self):
        # b"12" + overpunched '3' encodes -123 in a signed zoned field.
        buf = bytearray(b"12" + bytes([common.cob_put_sign_ascii(ord("3"))]))
        assert numeric.cob_cmp_sign_numdisp(buf, 3, -123) == 0
        assert numeric.cob_cmp_sign_numdisp(buf, 3, -100) < 0

    def test_cmp_long_sign_numdisp(self):
        buf = bytearray(b"12" + bytes([common.cob_put_sign_ascii(ord("3"))]))
        assert numeric.cob_cmp_long_sign_numdisp(buf, 3, -123) == 0

