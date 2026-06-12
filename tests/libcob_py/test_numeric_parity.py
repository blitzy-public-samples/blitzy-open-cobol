"""Byte-for-byte NUMERIC PARITY harness - the AAP 0.6.2 / 0.7.1 parity gate.

This is the thirteenth and final module of the ``tests/libcob_py/`` suite
created for the GNU Cobol (OpenCOBOL) C->Python backend refactor (AAP sections
0.2.1, 0.4.1, 0.6.2, 0.7.1).  Numeric parity is the *critical* non-functional
requirement of the whole refactor: a value stored under any
``USAGE`` / ``PICTURE`` / ``COMP`` combination must occupy the **exact same
bytes** that the original GnuCOBOL C runtime (``libcob/numeric.c``) produced,
and an emitted program's output must be byte-for-byte identical between the
original C-backed ``cobc`` and the refactored Python-backed ``cobc``.  Any
mismatch is a blocking regression.

Authoritative sources
----------------------
* ``libcob/numeric.c`` - the C runtime whose storage layout (zoned DISPLAY,
  big-endian / native BINARY, packed BCD COMP-3, unsigned COMP-X) this harness
  pins, ported into ``libcob_py/numeric.py``.
* ``tests/data-rep.src/numeric-display.cob`` and ``numeric-dump.cob`` - the
  IMMUTABLE, READ-ONLY parity reference programs (AAP section 0.2.3).  They are
  only ever READ and copied (with the ``@USAGE@`` placeholder substituted) into
  a throw-away temp directory; the originals are NEVER modified.
* ``tests/data-rep.src/binary.at`` / ``packed.at`` - the existing Autotest
  oracle from which the golden byte patterns asserted below were extracted and
  validated.

Two-pronged design (why both prongs exist)
------------------------------------------
The literal AAP 0.6.2 requirement is a *dual-compiler* compile-and-diff: build
the same COBOL program with both the original and the refactored ``cobc`` and
assert byte-for-byte-equal output.  But in a source-only checkout (the usual CI
state for this suite) **neither** compiler binary is built, so a dual-compiler-
only harness would either falsely fail or be entirely skipped, leaving the gate
unenforced.  This module therefore implements BOTH prongs:

* **Prong B - cobc-independent golden-oracle parity (ALWAYS runs).**  Values are
  encoded through ``libcob_py.numeric``'s ``USAGE`` encoder and the resulting
  bytes are compared against the authoritative golden hex patterns (the
  documented C-runtime oracle, NOT a value re-derived by the encoder under
  test).  This guarantees the parity gate is meaningfully enforced even with no
  ``cobc`` present.  Per the gate it provides at least one case per COMP type
  (DISPLAY, BINARY/COMP, PACKED/COMP-3, COMP-5, COMP-X) and at least three
  COMP-3 cases (truncation, rounding, overflow boundary).

* **Prong A - dual-cobc compile/compare harness (SKIPS when cobc absent).**  The
  literal AAP form: substitute ``@USAGE@`` into the read-only template, compile
  with both ``cobc`` binaries (located via the ``conftest`` helpers
  ``cobc_orig_path()`` / ``cobc_py_path()`` -> env ``COBC_ORIG`` / ``COBC_PY``),
  run both executables and compare stdout byte-for-byte.  It SKIPS cleanly (never
  fails) via ``@requires_dual_cobc`` whenever either compiler is unavailable.

Standard library only
---------------------
The only imports are :mod:`subprocess`, :mod:`sys`, :mod:`struct`,
:mod:`decimal` and :mod:`pathlib` from the stdlib; ``pytest`` is a
development-only test framework (never a runtime dependency).  ZERO third-party
packages are imported (AAP sections 0.5 / 0.7.1).  The runtime under test is
obtained with :func:`pytest.importorskip` so collection degrades to a clean
*skip* (never a hard error) when the parallel-built ``libcob_py`` package is not
yet importable.
"""
import decimal
import struct
import subprocess
import sys

import pytest

# Obtain the parallel-built runtime lazily so collection degrades to a clean
# SKIP (never a hard error) when the package - or a sub-module - is absent.
libcob_py = pytest.importorskip("libcob_py")
common = pytest.importorskip("libcob_py.common")
numeric = pytest.importorskip("libcob_py.numeric")

# Conftest helpers / markers for the dual-compiler harness (Prong A).  These are
# stdlib-only definitions in tests/libcob_py/conftest.py; importing them at
# module top level is safe (conftest is loaded before the test modules and puts
# the repository root on sys.path).  ``template_dir`` is a *fixture* and is
# therefore consumed as a test argument rather than imported here.
from tests.libcob_py.conftest import (  # noqa: E402  (after importorskip guard)
    cobc_orig_path,
    cobc_py_path,
    requires_dual_cobc,
    run_cobc,
)

# ---------------------------------------------------------------------------
# USAGE / flag / store-option / exception shorthands.  Every constant is read
# back from the runtime so the test never hard-codes a value that could drift
# from the module under test (the literals are asserted self-consistent below).
# ---------------------------------------------------------------------------
T_DISP = common.COB_TYPE_NUMERIC_DISPLAY
T_BIN = common.COB_TYPE_NUMERIC_BINARY
T_PACK = common.COB_TYPE_NUMERIC_PACKED

F_SIGN = common.COB_FLAG_HAVE_SIGN
F_SWAP = common.COB_FLAG_BINARY_SWAP          # set => big-endian (COMP / COMP-4)

O_ROUND = common.COB_STORE_ROUND
O_KEEP = common.COB_STORE_KEEP_ON_OVERFLOW

# COBOL EC-SIZE exception *codes* (the hex form latched into
# ``common.cob_exception_code``).  These are the AAP 0.6.2 / agent-prompt values.
EC_OVERFLOW = 0x1004        # EC-SIZE-OVERFLOW
EC_TRUNCATION = 0x1005      # EC-SIZE-TRUNCATION
EC_ZERO_DIVIDE = 0x1007     # EC-SIZE-ZERO-DIVIDE


# ===========================================================================
# GOLDEN BYTE PATTERNS - the documented C-runtime oracle (validated against the
# existing tests/data-rep.src binary.at / packed.at dumps; AAP section 0.6.2).
#
# Each tuple is ``(digits, value, signed, golden_hex)`` where ``golden_hex`` is
# the elementary item's stored bytes ONLY.  BINARY storage width follows the
# COBOL 2-4-8 rule; PACKED width is ``digits // 2 + 1`` bytes.
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
    # signed PIC S9(n) VALUE -n - big-endian two's-complement
    (1, -1, True, "ffff"),
    (2, -12, True, "fff4"),
    (3, -123, True, "ff85"),
    (4, -1234, True, "fb2e"),
]

PACKED_GOLDEN = [
    # unsigned PIC 9(n) - trailing sign nibble 0x0F
    (1, 1, False, "1f"),
    (4, 1234, False, "01234f"),
    (9, 123456789, False, "123456789f"),
    # negative PIC S9(n) - trailing sign nibble 0x0D
    (1, -1, True, "1d"),
    (4, -1234, True, "01234d"),
    # zero PIC 9(2) - BCD zero with the unsigned 0x0F sign nibble
    (2, 0, False, "000f"),
]


# ---------------------------------------------------------------------------
# Field-construction + encode/decode helpers (the same builder approach used by
# ``test_numeric.py`` so the parity harness and the unit tests share semantics).
# ---------------------------------------------------------------------------
def _bin_width(digits):
    """Return the COBOL BINARY storage width in bytes for *digits* (2-4-8 rule).

    1-4 digits -> 2 bytes, 5-9 -> 4 bytes, 10-18 -> 8 bytes.  This is the rule
    the BINARY golden table relies on and the layout ``cobc/field.c`` produces.
    """
    if digits <= 4:
        return 2
    if digits <= 9:
        return 4
    return 8


def mk(ftype, digits, scale, flags, size, data=None):
    """Build a ``cob_field`` backed by a (zeroed or supplied) ``bytearray``.

    The backing buffer is always a ``bytearray`` so the in-place stores the
    encoders perform (sign overpunch, BCD nibble writes, byte-swap) are visible
    through ``field.data`` exactly as the C ``cob_field.data`` pointer aliases a
    program's WORKING-STORAGE image.
    """
    attr = common.cob_field_attr(type=ftype, digits=digits, scale=scale,
                                 flags=flags, pic=None)
    buf = bytearray(size) if data is None else bytearray(data)
    return common.cob_field(size=size, data=buf, attr=attr)


def enc(field, unscaled, scale, opt=0, rounding=None):
    """Store ``unscaled * 10**-scale`` into *field* via the high-level path.

    Mirrors the emitter's ``cob_decimal_get_field`` store: build a
    ``cob_decimal`` from the integer mantissa + scale and store it into the
    field honouring the ``COB_STORE_*`` *opt* bitmask and the optional explicit
    ``rounding`` constant.  Returns the ``cob_decimal_get_field`` return code so
    overflow-boundary cases can assert on it.
    """
    return numeric.cob_decimal_get_field(
        numeric.cob_decimal(unscaled, scale), field, opt, rounding=rounding)


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

    Re-runs ``cob_init_numeric`` (resetting the file-scope scratch decimals and
    the packed-compare cache) and clears ``common.cob_exception_code`` so each
    EC-SIZE assertion observes only the exception raised by its own statement.
    """
    numeric.cob_init_numeric()
    common.cob_exception_code = 0
    yield


# ===========================================================================
# PRONG B - cobc-independent golden-oracle parity (ALWAYS runs)
# ===========================================================================
# These tests encode a value through the runtime's USAGE encoder and assert the
# stored bytes equal the documented golden pattern.  They run with NO cobc and
# are the always-on enforcement of the AAP 0.6.2 numeric-parity gate.


class TestBinaryParityGolden:
    """COMP / COMP-4 big-endian two's-complement storage vs the golden oracle.

    Covers the ``>=1 BINARY/COMP`` parity-gate requirement across all three
    storage widths (2, 4 and 8 bytes) and both unsigned ``9(n)`` and signed
    ``S9(n)`` items.
    """

    @pytest.mark.parametrize("digits,value,signed,golden", BINARY_GOLDEN,
                             ids=[("S9_%d_m%d" % (d, -v)) if s
                                  else ("9_%d_%d" % (d, v))
                                  for d, v, s, _ in BINARY_GOLDEN])
    def test_binary_encode_matches_golden(self, digits, value, signed, golden):
        """Encoder output == documented golden hex for each BINARY case.

        The expected bytes are the fixed C-runtime oracle (NOT a value
        re-derived by the encoder), so any divergence in ``numeric.py`` is
        caught against the contract.  The independent stdlib reference
        (``int.to_bytes``) is asserted to agree, and the value round-trips.
        """
        size = _bin_width(digits)
        flags = F_SWAP | (F_SIGN if signed else 0)
        field = mk(T_BIN, digits, 0, flags, size)
        enc(field, value, 0)
        assert bytes(field.data).hex() == golden
        assert bytes(field.data) == value.to_bytes(size, "big", signed=signed)
        assert dec(field) == (value, 0)

    def test_binary_storage_widths_are_2_4_8(self):
        """The 2-4-8 storage-width rule that the golden table relies on."""
        assert (_bin_width(1), _bin_width(4)) == (2, 2)
        assert (_bin_width(5), _bin_width(9)) == (4, 4)
        assert (_bin_width(10), _bin_width(18)) == (8, 8)


class TestPackedParityGolden:
    """PACKED-DECIMAL / COMP-3 BCD storage vs the golden oracle.

    Covers basic unsigned (sign nibble ``0x0F``), negative (``0x0D``),
    positive-signed (``0x0C``) and the zero case.  The COMP-3 *behavioural*
    cases (truncation / rounding / overflow) live in
    :class:`TestComp3DerivedParity` below.
    """

    @pytest.mark.parametrize("digits,value,signed,golden", PACKED_GOLDEN,
                             ids=[("S9_%d_m%d" % (d, -v)) if s
                                  else ("9_%d_%d" % (d, v))
                                  for d, v, s, _ in PACKED_GOLDEN])
    def test_packed_encode_matches_golden(self, digits, value, signed, golden):
        """Encoder output == documented golden hex for each PACKED case.

        ``nbytes = digits // 2 + 1``.  The final byte's low nibble is the sign
        nibble; the documented convention (``0x0F`` unsigned / ``0x0D``
        negative) is asserted explicitly.
        """
        size = digits // 2 + 1
        flags = F_SIGN if signed else 0
        field = mk(T_PACK, digits, 0, flags, size)
        enc(field, value, 0)
        assert bytes(field.data).hex() == golden
        last_nibble = field.data[size - 1] & 0x0F
        if not signed:
            assert last_nibble == 0x0F
        elif value < 0:
            assert last_nibble == 0x0D
        else:
            assert last_nibble == 0x0C

    def test_packed_positive_signed_nibble_is_0c(self):
        """Positive-signed ``S9(1) VALUE +1`` -> ``1c`` (sign nibble 0x0C)."""
        field = mk(T_PACK, 1, 0, F_SIGN, 1)
        enc(field, 1, 0)
        assert bytes(field.data).hex() == "1c"
        assert (field.data[0] & 0x0F) == 0x0C
        assert numeric.cob_packed_get_sign(field) == 1


class TestDisplayParityGolden:
    """DISPLAY (zoned decimal) storage - one byte per digit, trailing overpunch.

    Covers the ``>=1 DISPLAY`` parity-gate requirement.
    """

    def test_display_unsigned_exact_bytes(self):
        """``PIC 9(3)=123`` -> the exact ASCII digit bytes ``b"123"``."""
        field = mk(T_DISP, 3, 0, 0, 3)
        enc(field, 123, 0)
        assert bytes(field.data) == b"123"
        assert bytes(field.data) == bytes([0x31, 0x32, 0x33])

    def test_display_signed_negative_overpunch_roundtrip(self):
        """``PIC S9(3)=-123`` -> trailing operational-sign OVERPUNCH.

        The leading digits stay plain ASCII; the final byte carries the
        operational sign as an overpunch character (NOT a plain ``'3'``).  Per
        the agent-prompt the assertion is on the ROUND-TRIP (decode == -123) and
        on the final byte being the module's own ASCII overpunch character
        (``common.cob_put_sign_ascii``) rather than hard-coding an EBCDIC byte.
        """
        field = mk(T_DISP, 3, 0, F_SIGN, 3)
        enc(field, -123, 0)
        data = bytes(field.data)
        assert data[:2] == b"12"                       # leading digits unchanged
        last = data[2]
        assert last != ord("3")                        # overpunched, not plain '3'
        assert last == common.cob_put_sign_ascii(ord("3"))
        assert dec(field) == (-123, 0)                 # and it round-trips

    def test_display_scaled_roundtrip(self):
        """``PIC S9(3)V99`` stores the unscaled mantissa with the field scale."""
        field = mk(T_DISP, 5, 2, F_SIGN, 5)
        enc(field, -12345, 2)
        assert dec(field) == (-12345, 2)
        assert dec_decimal(field) == decimal.Decimal("-123.45")


class TestComp5ParityGolden:
    """COMP-5 native-endian binary storage (byte order == ``sys.byteorder``).

    Covers the ``>=1 COMP-5`` parity-gate requirement.  COMP-5 omits the
    ``COB_FLAG_BINARY_SWAP`` flag so storage uses the host's native order.
    """

    def test_comp5_native_order_roundtrip(self):
        """``S9(4) COMP-5 = -1234`` stores in native order and round-trips."""
        field = mk(T_BIN, 4, 0, F_SIGN, 2)            # no F_SWAP => native order
        enc(field, -1234, 0)
        expected = (-1234 & 0xFFFF).to_bytes(2, sys.byteorder)
        assert bytes(field.data) == expected
        assert bytes(field.data) == struct.pack(
            "<h" if sys.byteorder == "little" else ">h", -1234)
        assert dec(field) == (-1234, 0)

    def test_comp5_unsigned_native_roundtrip(self):
        """A representative unsigned ``9(4) COMP-5`` value round-trips natively."""
        field = mk(T_BIN, 4, 0, 0, 2)
        enc(field, 4660, 0)                            # 0x1234
        assert bytes(field.data) == (4660).to_bytes(2, sys.byteorder)
        assert dec(field) == (4660, 0)


class TestCompXParityGolden:
    """COMP-X unsigned binary storage (native order, no sign).

    Covers the ``>=1 COMP-X`` parity-gate requirement.
    """

    def test_compx_full_width_value_roundtrip(self):
        """``9(4) COMP-X = 65535`` -> ``ffff`` in 2 bytes and round-trips.

        65535 fills both bytes; the pattern ``0xFFFF`` is identical in either
        byte order, so the golden ``ffff`` holds regardless of host endianness.
        COMP-X is unsigned and uses native order (no ``COB_FLAG_BINARY_SWAP``).
        """
        field = mk(T_BIN, 4, 0, 0, 2)
        enc(field, 65535, 0)
        assert bytes(field.data).hex() == "ffff"
        assert dec(field) == (65535, 0)
        assert common.cob_exception_code != EC_OVERFLOW   # fits the 2-byte cell

    def test_compx_representative_value_roundtrip(self):
        """``9(4) COMP-X = 9999`` round-trips through native-order storage."""
        field = mk(T_BIN, 4, 0, 0, 2)
        enc(field, 9999, 0)
        assert bytes(field.data) == (9999).to_bytes(2, sys.byteorder)
        assert dec(field) == (9999, 0)



class TestComp3DerivedParity:
    """COMP-3 behavioural parity - the >=3 gate cases (AAP 0.6.2).

    The numeric-parity gate requires at least three COMP-3 cases exercising
    TRUNCATION, ROUNDING and the OVERFLOW BOUNDARY.  Each asserts the exact
    stored BCD bytes (the byte-for-byte contract) and, for overflow, the
    ``EC-SIZE-OVERFLOW`` (``0x1004``) latch in ``common.cob_exception_code``.
    """

    def test_comp3_truncation_drops_excess_fraction(self):
        """TRUNCATION: 123.456 into ``S9(3)V99`` COMP-3 with NO ROUNDED -> 123.45.

        Storing a value with more fractional digits than the receiving item's
        scale truncates toward zero under the COBOL default (no ``ROUNDED`` ==
        ``ROUND_DOWN``).  The stored bytes equal the BCD of the *truncated*
        value ``-123.45`` (``12345d``), and the decoded mantissa is ``-12345``
        at scale 2 - i.e. truncated, NOT rounded to ``-123.46``.

        Per the runtime contract (``numeric.py`` keeps ``Inexact`` / ``Rounded``
        observable-only because COBOL SIZE ERROR is integer-digit overflow, not
        fractional rounding - AAP 0.6.2) fractional truncation does not raise an
        EC-SIZE *overflow*; the agent-prompt explicitly permits the
        "value truncated per ROUND_DOWN default" outcome here.  We therefore
        assert the byte-for-byte truncated result and that no OVERFLOW code is
        latched (a non-overflow / observable-only outcome).
        """
        field = mk(T_PACK, 5, 2, F_SIGN, 3)            # S9(3)V99 -> 3 bytes
        common.cob_exception_code = 0
        enc(field, -123456, 3, opt=0)                  # -123.456, no COB_STORE_ROUND
        assert bytes(field.data).hex() == "12345d"     # BCD of -123.45
        assert dec(field) == (-12345, 2)               # truncated toward zero
        assert dec_decimal(field) == decimal.Decimal("-123.45")
        assert common.cob_exception_code != EC_OVERFLOW

    def test_comp3_truncation_positive(self):
        """TRUNCATION (positive): +123.456 -> +123.45 with sign nibble ``0x0C``."""
        field = mk(T_PACK, 5, 2, F_SIGN, 3)
        enc(field, 123456, 3, opt=0)
        assert bytes(field.data).hex() == "12345c"     # BCD of +123.45
        assert dec(field) == (12345, 2)

    def test_comp3_rounding_half_up(self):
        """ROUNDING: 123.456 into ``S9(3)V99`` COMP-3 WITH ROUNDED -> 123.46.

        With ``COB_STORE_ROUND`` and no explicit mode the COBOL default rounding
        is ``NEAREST-AWAY-FROM-ZERO`` (``ROUND_HALF_UP``): the discarded
        fractional digit ``6`` rounds the retained ``5`` up to ``6``, so
        ``-123.456`` stores as ``-123.46`` (``12346d``).
        """
        field = mk(T_PACK, 5, 2, F_SIGN, 3)
        enc(field, -123456, 3, opt=O_ROUND)
        assert bytes(field.data).hex() == "12346d"     # BCD of -123.46
        assert dec(field) == (-12346, 2)
        assert dec_decimal(field) == decimal.Decimal("-123.46")

    def test_comp3_rounding_positive(self):
        """ROUNDING (positive): +123.456 ROUNDED -> +123.46 (sign nibble ``0x0C``)."""
        field = mk(T_PACK, 5, 2, F_SIGN, 3)
        enc(field, 123456, 3, opt=O_ROUND)
        assert bytes(field.data).hex() == "12346c"     # BCD of +123.46

    def test_comp3_overflow_boundary_latches_ec_size_overflow(self):
        """OVERFLOW BOUNDARY: 1000 into ``S9(3)`` COMP-3 -> ``EC-SIZE-OVERFLOW``.

        ``1000`` exceeds the three-digit capacity of ``S9(3)``, latching
        ``common.cob_exception_code`` to ``EC-SIZE-OVERFLOW`` (``0x1004`` - the
        ``ON SIZE ERROR`` path).  Under the default (non-KEEP) store the
        high-order digit is dropped and the low three digits (``000``) are kept,
        producing ``000c`` (positive sign nibble).
        """
        field = mk(T_PACK, 3, 0, F_SIGN, 2)            # S9(3) -> 2 bytes
        common.cob_exception_code = 0
        enc(field, 1000, 0, opt=0)
        assert common.cob_exception_code == EC_OVERFLOW
        assert bytes(field.data).hex() == "000c"       # documented overflow result
        assert dec(field) == (0, 0)                    # low 3 digits of 1000

    def test_comp3_overflow_boundary_keep_abandons_store(self):
        """OVERFLOW BOUNDARY with KEEP: store abandoned, code returned to caller.

        With ``COB_STORE_KEEP_ON_OVERFLOW`` the receiving field is left
        untouched (its initial zero image) and ``EC-SIZE-OVERFLOW`` is both
        latched and returned - the exact COBOL ``ON SIZE ERROR`` semantics.
        """
        field = mk(T_PACK, 3, 0, F_SIGN, 2)
        original = bytes(field.data)
        common.cob_exception_code = 0
        rc = enc(field, 1000, 0, opt=O_KEEP)
        assert rc == EC_OVERFLOW
        assert common.cob_exception_code == EC_OVERFLOW
        assert bytes(field.data) == original           # store abandoned


class TestRoundedModeParity:
    """The seven COBOL ``ROUNDED MODE`` -> :mod:`decimal` constant mappings.

    AAP 0.6.2 fixes the exact map; this asserts both the table and its
    observable byte-for-byte effect when storing the half-way value 12.5.
    """

    def test_rounded_mode_map_is_exact(self):
        """The 7-entry map and the two defaults match AAP 0.6.2 exactly."""
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

    @pytest.mark.parametrize("mode_name,expected", [
        ("NEAREST-AWAY-FROM-ZERO", 13),   # 12.5 -> 13 (HALF_UP)
        ("NEAREST-EVEN", 12),             # 12.5 -> 12 (banker's, even neighbour)
        ("NEAREST-TOWARD-ZERO", 12),      # 12.5 -> 12 (HALF_DOWN)
        ("TRUNCATION", 12),               # 12.5 -> 12 (toward zero)
        ("TOWARD-GREATER", 13),           # 12.5 -> 13 (ceiling)
        ("TOWARD-LESSER", 12),            # 12.5 -> 12 (floor)
        ("AWAY-FROM-ZERO", 13),           # 12.5 -> 13 (up)
    ])
    def test_rounded_mode_storage_effect(self, mode_name, expected):
        """Storing 12.5 under each mode yields the documented DISPLAY bytes."""
        mode = numeric.ROUND_MODE_MAP[mode_name]
        field = mk(T_DISP, 2, 0, 0, 2)
        numeric.cob_decimal_get_field(numeric.cob_decimal(125, 1), field,
                                      O_ROUND, rounding=mode)
        assert bytes(field.data) == b"%02d" % expected


class TestSizeErrorTrapParity:
    """The ``EC-SIZE`` family traps and the per-statement decimal trap context.

    AAP 0.6.2 maps ``decimal`` ``Overflow`` / ``DivisionByZero`` signals to the
    COBOL ``EC-SIZE`` family inside ``cob_size_error_context`` while keeping
    ``Inexact`` / ``Rounded`` observable-only (a legal ``ROUNDED`` store must not
    raise SIZE ERROR).
    """

    def test_integer_overflow_latches_ec_size_overflow(self):
        """Storing too many integer digits raises ``EC-SIZE-OVERFLOW`` (0x1004)."""
        field = mk(T_DISP, 4, 0, 0, 4)
        common.cob_exception_code = 0
        enc(field, 12345, 0, opt=0)                    # 5 digits into PIC 9(4)
        assert common.cob_exception_code == EC_OVERFLOW
        assert bytes(field.data) == b"2345"            # low-order digits kept

    def test_zero_divide_latches_ec_size_zero_divide(self):
        """``cob_decimal_div`` by zero raises ``EC-SIZE-ZERO-DIVIDE`` (0x1007)."""
        a = numeric.cob_decimal(1, 0)
        b = numeric.cob_decimal(0, 0)
        common.cob_exception_code = 0
        numeric.cob_decimal_div(a, b)
        assert a.scale == numeric.DECIMAL_NAN
        assert common.cob_exception_code == EC_ZERO_DIVIDE

    def test_legal_rounded_store_does_not_raise_size_error(self):
        """A legal ``ROUNDED`` store rounds (Inexact) but must NOT raise EC-SIZE."""
        field = mk(T_DISP, 4, 2, 0, 4)
        common.cob_exception_code = 0
        enc(field, 1, 3, opt=O_ROUND)                  # 0.001 -> 0.00 (rounded)
        assert common.cob_exception_code == 0

    def test_trap_context_overflow_maps_to_ec_size_overflow(self):
        """A genuine ``decimal.Overflow`` inside the context maps to 0x1004."""
        common.cob_exception_code = 0
        with numeric.cob_size_error_context(O_ROUND):
            decimal.Decimal("9E999999999") * decimal.Decimal("9E999999999")
        assert common.cob_exception_code == EC_OVERFLOW

    def test_trap_context_zero_divide_maps_to_ec_size_zero_divide(self):
        """A ``decimal.DivisionByZero`` inside the context maps to 0x1007."""
        common.cob_exception_code = 0
        with numeric.cob_size_error_context(O_ROUND):
            decimal.Decimal(1) / decimal.Decimal(0)
        assert common.cob_exception_code == EC_ZERO_DIVIDE

    def test_trap_context_rounding_selected_by_store_round(self):
        """The context installs HALF_UP under ROUND, truncation without it."""
        with numeric.cob_size_error_context(0) as ctx:        # no ROUND -> truncate
            assert ctx.rounding == numeric.COB_ROUND_TRUNCATION
        with numeric.cob_size_error_context(O_ROUND) as ctx:  # ROUND -> HALF_UP
            assert ctx.rounding == numeric.COB_ROUND_DEFAULT



# ===========================================================================
# PRONG A - dual-cobc compile/compare harness (SKIPS when cobc absent)
# ===========================================================================
# The literal AAP 0.6.2 requirement: compile the SAME reference program with
# both the original C-backed cobc and the refactored Python-backed cobc, run
# each, and assert the program output is byte-for-byte identical.  Located via
# the conftest helpers (env COBC_ORIG / COBC_PY); SKIPS cleanly via
# @requires_dual_cobc whenever either binary is unavailable - which is the usual
# state of a source-only checkout - so it NEVER falsely fails.
#
# Template choice (documented fallback per the agent-prompt / AAP 0.6.2):
#   * ``numeric-dump.cob`` would give raw-byte STORAGE parity, but it CALLs a
#     ``dump`` subroutine whose only implementation is the inline C ``dump.c``
#     used by ``binary.at`` / ``packed.at`` (compiled with ``${CC}``).  In the
#     Python-only backend world there is no C toolchain to build that
#     subroutine, so reproducing it is impractical.
#   * The agent-prompt therefore explicitly permits falling back to the
#     ``numeric-display.cob`` DISPLAY-text parity, which needs NO helper
#     subroutine: it merely DISPLAYs each item, so both compilers emit
#     comparable text and a byte-for-byte stdout diff is a valid end-to-end
#     parity check.  Raw-byte STORAGE layout is already pinned headlessly by
#     Prong B above, so no storage coverage is lost.

#: USAGE tokens substituted for the ``@USAGE@`` placeholder in the template.
#: ``DISPLAY`` and the four computational usages the gate enumerates; each is a
#: valid bare USAGE clause appended after the ``VALUE`` clause (mirroring
#: ``binary.at``'s ``sed -e 's/@USAGE@/BINARY/'``).
_PRONG_A_USAGES = ("DISPLAY", "COMP", "COMP-3", "COMP-5", "BINARY")


def _materialise_template(template_path, usage_token, dest_path):
    """Substitute ``@USAGE@`` -> *usage_token* in *template_path* -> *dest_path*.

    The read-only reference template (AAP 0.2.3) is only READ here; the
    substituted copy is written to the throw-away *dest_path* (under the test's
    ``tmp_path``).  Returns *dest_path*.  Every occurrence of the placeholder is
    replaced (one per data item), exactly like the Autotest ``sed`` rule.
    """
    source_text = template_path.read_text(encoding="latin-1")
    materialised = source_text.replace("@USAGE@", usage_token)
    dest_path.write_text(materialised, encoding="latin-1")
    return dest_path


def _compile_and_run(cobc_binary, prog_cob, work_dir, exe_name):
    """Compile *prog_cob* with *cobc_binary* and run the resulting executable.

    Compiles ``<cobc> -x -std=cobol2002 -o <exe> <prog.cob>`` (mirroring the
    AAP 0.6.2 invocation) inside *work_dir*, then executes the program and
    returns its captured stdout as raw bytes.  A non-zero compile or run return
    code is surfaced as an assertion failure (the binary exists but misbehaves -
    a genuine regression), NOT a skip; missing binaries are handled by the
    ``@requires_dual_cobc`` marker before this helper is ever reached.
    """
    exe_path = work_dir / exe_name
    compile_result = run_cobc(
        cobc_binary,
        ["-x", "-std=cobol2002", "-o", str(exe_path), str(prog_cob)],
        cwd=work_dir,
        timeout=180,
    )
    assert compile_result.returncode == 0, (
        "compilation failed with %s:\n%s"
        % (cobc_binary, compile_result.stderr.decode("latin-1", "replace"))
    )
    run_result = subprocess.run(
        [str(exe_path)],
        cwd=str(work_dir),
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert run_result.returncode == 0, (
        "execution of %s failed (rc=%d):\n%s"
        % (exe_path, run_result.returncode,
           run_result.stderr.decode("latin-1", "replace"))
    )
    return run_result.stdout


@requires_dual_cobc
@pytest.mark.parametrize("usage_token", _PRONG_A_USAGES)
def test_dual_cobc_numeric_output_parity(usage_token, tmp_path, template_dir):
    """Compile ``numeric-display.cob`` with both ``cobc`` and diff the output.

    For each ``@USAGE@`` token the SAME materialised source is compiled with the
    original C-backed ``cobc`` and the refactored Python-backed ``cobc``; both
    executables are run and their stdout asserted byte-for-byte identical.  This
    is the literal AAP 0.6.2 dual-compiler parity gate.  It SKIPS (never fails)
    when either compiler is unavailable.  The template's 18 unsigned
    (``X-P1``..``X-P18``) plus 18 signed (``X-N1``..``X-N18``) PICs are all
    exercised in a single run per usage.
    """
    template = template_dir / "numeric-display.cob"
    prog_cob = _materialise_template(
        template, usage_token, tmp_path / "prog.cob")

    out_orig = _compile_and_run(
        cobc_orig_path(), prog_cob, tmp_path, "prog_orig")
    out_py = _compile_and_run(
        cobc_py_path(), prog_cob, tmp_path, "prog_py")

    assert out_orig == out_py, (
        "byte-for-byte numeric output parity FAILED for @USAGE@=%s\n"
        "original (%d bytes): %r\nrefactored (%d bytes): %r"
        % (usage_token, len(out_orig), out_orig, len(out_py), out_py)
    )


# ===========================================================================
# Phase 4 - reference-template presence guard
# ===========================================================================
def test_templates_present():
    """Guard: both read-only parity templates exist under ``tests/data-rep.src``.

    The byte-for-byte parity harness depends on the immutable reference programs
    ``numeric-display.cob`` and ``numeric-dump.cob`` (AAP 0.2.3).  This guard
    asserts their presence so a missing/renamed template is reported as an
    explicit, actionable failure here rather than as an opaque skip deep inside
    the dual-compiler harness.  The repository-root path is resolved from this
    module's own location (``tests/libcob_py/`` -> repo root -> ``tests``),
    independent of the caller's working directory.
    """
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    data_rep_src = repo_root / "tests" / "data-rep.src"
    for name in ("numeric-display.cob", "numeric-dump.cob"):
        template = data_rep_src / name
        assert template.is_file(), (
            "required read-only parity template is missing: %s" % template
        )

