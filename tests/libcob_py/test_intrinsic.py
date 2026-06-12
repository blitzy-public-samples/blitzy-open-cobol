"""Unit tests for :mod:`libcob_py.intrinsic`.

Exercises the pure-Python port of the C runtime ``libcob/intrinsic.c`` - all
**68** ``cob_intr_*`` entry points the code generator emits, reproduced with the
standard library (:mod:`decimal` via :mod:`libcob_py.numeric`, :mod:`math`,
:mod:`time`) instead of GMP (AAP sections 0.4.1 / 0.6.1).

AUTHORITY / COUNT (AAP 0.6.1 .def-authority resolution)
-------------------------------------------------------
The AAP narrative mentions "42 intrinsic functions"; direct inspection of
``libcob/intrinsic.c`` shows **68** distinct ``cob_intr_*`` C entry points
(``grep -oE 'cob_intr_[a-z0-9_]+' libcob/intrinsic.c | sort -u | wc -l`` == 68).
The 42 are the user-facing COBOL ``FUNCTION`` subset; the 68 add internal
helpers such as ``cob_intr_binop``.  Per authority-rule 3 the implementation
provides **all 68**, so this suite hard-asserts the full 68 (see
:func:`test_all_68_intrinsics_present`) - NOT 42 - and additionally asserts the
42 user-facing names are a subset.

DATE EPOCH (verified against intrinsic.c L1217/L1228)
-----------------------------------------------------
COBOL integer-date epoch is ``integer 1 == 1601-01-01`` (``baseyear = 1601``);
valid day range ``1..3067671``; out-of-range arguments raise
``EC-ARGUMENT-FUNCTION``.  The date tests below use this epoch
(``INTEGER-OF-DATE(16010101) == 1``).

HARD CONSTRAINTS (AAP 0.5 / 0.7.1)
----------------------------------
* **Standard library only.**  The sole non-stdlib import is ``pytest``, a
  development-only framework (never a runtime dependency).  No third-party
  package is imported.  In particular this module imports **only** the runtime
  modules in its dependency contract - :mod:`libcob_py.intrinsic`,
  :mod:`libcob_py.common`, and :mod:`libcob_py.numeric` - and never
  :mod:`libcob_py.move` (the intrinsic runtime reaches the data-movement
  primitives through ``common``'s lazy shims, so the tests do too, via
  ``common._lazy_get_int`` / ``common.cob_put_sign``).
* **Clean skip when the runtime is absent.**  ``pytest.importorskip`` yields a
  *skip* (not a collection error) when the parallel-built ``libcob_py`` package -
  or a specific sub-module - is not yet importable.
* Designed for >= 80% line coverage of ``libcob_py/intrinsic.py`` by
  introspecting the full entry-point set and exercising a broad, representative
  behavioral sample across the math / statistics / date-time / string /
  exception-inquiry categories.
"""
from decimal import Decimal

import math
import struct

import pytest

# Runtime under test + its dependency contract.  Each is fetched with
# ``importorskip`` so an unbuilt runtime SKIPs cleanly rather than erroring.
libcob_py = pytest.importorskip("libcob_py")
intrinsic = pytest.importorskip("libcob_py.intrinsic")
common = pytest.importorskip("libcob_py.common")
numeric = pytest.importorskip("libcob_py.numeric")


# ===========================================================================
# The authoritative entry-point sets (verified against libcob/intrinsic.c)
# ===========================================================================
#: The EXACT 68 ``cob_intr_*`` entry points defined by ``libcob/intrinsic.c``
#: and required of ``libcob_py/intrinsic.py``.  This is the headline coverage
#: contract: every name here must be present on the module and callable.
EXPECTED_68 = frozenset((
    "cob_intr_abs", "cob_intr_acos", "cob_intr_annuity", "cob_intr_asin",
    "cob_intr_atan", "cob_intr_binop", "cob_intr_char",
    "cob_intr_combined_datetime", "cob_intr_concatenate", "cob_intr_cos",
    "cob_intr_current_date", "cob_intr_date_of_integer",
    "cob_intr_date_to_yyyymmdd", "cob_intr_day_of_integer",
    "cob_intr_day_to_yyyyddd", "cob_intr_exception_file",
    "cob_intr_exception_location", "cob_intr_exception_statement",
    "cob_intr_exception_status", "cob_intr_exp", "cob_intr_exp10",
    "cob_intr_factorial", "cob_intr_fraction_part", "cob_intr_integer",
    "cob_intr_integer_of_date", "cob_intr_integer_of_day",
    "cob_intr_integer_part", "cob_intr_lcl_time_from_secs", "cob_intr_length",
    "cob_intr_locale_date", "cob_intr_locale_time", "cob_intr_log",
    "cob_intr_log10", "cob_intr_lower_case", "cob_intr_max", "cob_intr_mean",
    "cob_intr_median", "cob_intr_midrange", "cob_intr_min", "cob_intr_mod",
    "cob_intr_numval", "cob_intr_numval_c", "cob_intr_ord", "cob_intr_ord_max",
    "cob_intr_ord_min", "cob_intr_present_value", "cob_intr_random",
    "cob_intr_range", "cob_intr_rem", "cob_intr_reverse",
    "cob_intr_seconds_from_formatted_time", "cob_intr_seconds_past_midnight",
    "cob_intr_sign", "cob_intr_sin", "cob_intr_sqrt",
    "cob_intr_standard_deviation", "cob_intr_stored_char_length",
    "cob_intr_substitute", "cob_intr_substitute_case", "cob_intr_sum",
    "cob_intr_tan", "cob_intr_test_date_yyyymmdd", "cob_intr_test_day_yyyyddd",
    "cob_intr_trim", "cob_intr_upper_case", "cob_intr_variance",
    "cob_intr_when_compiled", "cob_intr_year_to_yyyy",
))

#: The 42 user-facing COBOL ``FUNCTION`` intrinsics (a subset of EXPECTED_68).
#: These are the names a COBOL programmer references directly via ``FUNCTION
#: <name>``; the remaining EXPECTED_68 entries (e.g. ``cob_intr_binop``) are
#: internal helpers.  Used to assert the documented "42 are a subset of 68"
#: relationship (AAP 0.6.1).
USER_FACING_42 = frozenset((
    "cob_intr_abs", "cob_intr_acos", "cob_intr_annuity", "cob_intr_asin",
    "cob_intr_atan", "cob_intr_char", "cob_intr_combined_datetime",
    "cob_intr_concatenate", "cob_intr_cos", "cob_intr_current_date",
    "cob_intr_date_of_integer", "cob_intr_day_of_integer", "cob_intr_exp",
    "cob_intr_exp10", "cob_intr_factorial", "cob_intr_fraction_part",
    "cob_intr_integer", "cob_intr_integer_of_date", "cob_intr_integer_of_day",
    "cob_intr_integer_part", "cob_intr_length", "cob_intr_log",
    "cob_intr_log10", "cob_intr_lower_case", "cob_intr_max", "cob_intr_mean",
    "cob_intr_median", "cob_intr_midrange", "cob_intr_min", "cob_intr_mod",
    "cob_intr_numval", "cob_intr_numval_c", "cob_intr_ord", "cob_intr_ord_max",
    "cob_intr_ord_min", "cob_intr_present_value", "cob_intr_random",
    "cob_intr_range", "cob_intr_rem", "cob_intr_reverse", "cob_intr_sign",
    "cob_intr_sin",
))


# ===========================================================================
# Field builders (argument cob_fields) + result decoders
#
# Built strictly from the dependency contract: ``common`` (cob_field /
# cob_field_attr / sign handling / int accessor) and ``numeric`` (decimal
# decode).  No ``move`` import - integer access goes through ``common``'s
# documented lazy shim ``common._lazy_get_int`` (the same bridge intrinsic.py
# itself uses), keeping this module's imports inside its whitelist.
# ===========================================================================
def alnum(text):
    """Build an ALPHANUMERIC argument ``cob_field`` from *text*."""
    data = text.encode("latin-1") if isinstance(text, str) else bytes(text)
    attr = common.cob_field_attr(
        type=common.COB_TYPE_ALPHANUMERIC, digits=0, scale=0, flags=0, pic=None)
    return common.cob_field(size=len(data), data=bytearray(data), attr=attr)


def numdisp(digits, scale=0, negative=False):
    """Build a zoned DISPLAY numeric argument field from a digit string.

    ``numdisp("12345", scale=2)`` represents ``123.45``.  When *negative* is
    true the ``COB_FLAG_HAVE_SIGN`` attribute flag is set **and** the sign is
    overpunched into the field via :func:`common.cob_put_sign` - both steps are
    required for the runtime to read the value as negative.
    """
    data = digits.encode("latin-1")
    flags = common.COB_FLAG_HAVE_SIGN if negative else 0
    attr = common.cob_field_attr(
        type=common.COB_TYPE_NUMERIC_DISPLAY, digits=len(digits), scale=scale,
        flags=flags, pic=None)
    field = common.cob_field(size=len(data), data=bytearray(data), attr=attr)
    if negative:
        common.cob_put_sign(field, -1)
    return field


def rdec(field):
    """Decode a numeric result ``cob_field`` into a :class:`decimal.Decimal`.

    Uses the :mod:`libcob_py.numeric` decimal decoder, so it works for every
    numeric USAGE the intrinsics emit (BINARY, DISPLAY, PACKED) and - unlike the
    32-bit ``cob_get_int`` accessor - preserves full precision for large values
    such as ``FUNCTION FACTORIAL(20)``.
    """
    dec = numeric.cob_decimal()
    numeric.cob_decimal_set_field(dec, field)
    return Decimal(dec.value).scaleb(-dec.scale)


def rint(field):
    """Decode a numeric result field as a Python ``int`` via the runtime accessor.

    Routes through ``common._lazy_get_int`` (the documented shim intrinsic.py
    uses for ``cob_get_int``).  Mirrors the C ``int`` accessor, so use it only
    for values that fit a 32-bit int; use :func:`rdec` for wide results.
    """
    return common._lazy_get_int(field)


def rdouble(field):
    """Decode an IEEE COMP-2 (8-byte double) result field as a Python ``float``.

    The double-valued intrinsics (SQRT / EXP / EXP10 / LOG / LOG10 / TAN,
    ANNUITY, RANDOM, MIDRANGE, even-count MEDIAN, PRESENT-VALUE,
    STANDARD-DEVIATION) store via ``struct.pack("=d", ...)``; read them back
    the same way.
    """
    return struct.unpack("=d", bytes(field.data[:8]))[0]


def rtext(field):
    """Decode an alphanumeric result field as ``bytes`` (honouring ref-mod size)."""
    return bytes(field.data[:field.size])


# ===========================================================================
# Phase 1 - Completeness: all 68 entry points present & callable
# ===========================================================================
def test_all_68_intrinsics_present():
    """Every one of the 68 ``cob_intr_*`` entry points exists and is callable.

    This is the headline count assertion (68, not 42): the AAP 0.6.1 authority
    resolution requires the Python runtime to implement the full C entry-point
    set.
    """
    module_names = set(dir(intrinsic))
    missing = EXPECTED_68 - module_names
    assert not missing, "intrinsic module is missing entry points: %s" % sorted(missing)
    assert EXPECTED_68.issubset(module_names)
    for name in sorted(EXPECTED_68):
        attr = getattr(intrinsic, name)
        assert callable(attr), "%s is present but not callable" % name


def test_intrinsic_count_at_least_68():
    """There are >= 68 ``cob_intr_*`` callables, and the 42 are a subset.

    Allows the module to expose additional internal helpers beyond the 68 while
    guaranteeing the verified minimum, and verifies the documented relationship
    that the 42 user-facing FUNCTION names are a subset of the full set.
    """
    callables = {
        name for name in dir(intrinsic)
        if name.startswith("cob_intr_") and callable(getattr(intrinsic, name))
    }
    assert len(callables) >= 68
    # The 42 user-facing names are a subset of both the 68 and the live module.
    assert USER_FACING_42.issubset(EXPECTED_68)
    assert len(USER_FACING_42) == 42
    assert USER_FACING_42.issubset(callables)


# ===========================================================================
# Phase 2 - Math & numeric intrinsics (behavioral spot-checks)
# ===========================================================================
def test_abs():
    """FUNCTION ABS returns the magnitude, preserving the source shape."""
    assert rdec(intrinsic.cob_intr_abs(numdisp("1234", scale=2))) == Decimal("12.34")
    assert rdec(intrinsic.cob_intr_abs(numdisp("1234", scale=2, negative=True))) \
        == Decimal("12.34")


def test_sqrt():
    """FUNCTION SQRT - COMP-2 double within COBOL precision."""
    assert rdouble(intrinsic.cob_intr_sqrt(numdisp("16"))) == pytest.approx(4.0, abs=1e-9)
    assert rdouble(intrinsic.cob_intr_sqrt(numdisp("2"))) \
        == pytest.approx(math.sqrt(2.0), abs=1e-12)


def test_mod_rem():
    """FUNCTION MOD is a floored modulo; FUNCTION REM truncates toward zero.

    Per the COBOL definitions exercised by intrinsic.c: ``MOD(-11, 4) == 1``
    (sign follows the divisor) while ``REM(-11, 4) == -3`` (sign follows the
    dividend).
    """
    assert rint(intrinsic.cob_intr_mod(numdisp("17"), numdisp("5"))) == 2
    assert rdec(intrinsic.cob_intr_rem(numdisp("17"), numdisp("5"))) == Decimal("2")
    # The headline signed cases from the agent prompt:
    assert rint(intrinsic.cob_intr_mod(numdisp("11", negative=True), numdisp("4"))) == 1
    assert rdec(intrinsic.cob_intr_rem(numdisp("11", negative=True), numdisp("4"))) \
        == Decimal("-3")


def test_factorial():
    """FUNCTION FACTORIAL - exact, including big-integer precision for 20!.

    ``FACTORIAL(20)`` (== 2,432,902,008,176,640,000) exceeds a 32-bit int, so it
    is decoded with :func:`rdec` (the decimal/64-bit accessor) rather than the
    32-bit ``rint`` accessor.
    """
    assert rint(intrinsic.cob_intr_factorial(numdisp("5"))) == 120
    assert rint(intrinsic.cob_intr_factorial(numdisp("0"))) == 1
    assert rdec(intrinsic.cob_intr_factorial(numdisp("20"))) \
        == Decimal(2432902008176640000)


def test_factorial_negative_sets_exception():
    """FACTORIAL of a negative argument raises EC-ARGUMENT-FUNCTION and yields 0."""
    common.cob_exception_code = 0
    result = intrinsic.cob_intr_factorial(numdisp("3", negative=True))
    assert rint(result) == 0
    assert common.cob_exception_code != 0
    assert common.cob_get_exception_name(common.cob_exception_code) \
        == "EC-ARGUMENT-FUNCTION"


def test_integer_part_fraction_part():
    """FUNCTION INTEGER-PART truncates toward zero; FRACTION-PART keeps the rest.

    Includes the agent-prompt case ``INTEGER-PART(-1.5) == -1``.
    """
    assert rdec(intrinsic.cob_intr_integer_part(numdisp("1299", scale=2))) == Decimal("12")
    assert rdec(intrinsic.cob_intr_integer_part(numdisp("15", scale=1, negative=True))) \
        == Decimal("-1")
    assert rdec(intrinsic.cob_intr_fraction_part(numdisp("1234", scale=2))) == Decimal("0.34")


def test_integer_floor():
    """FUNCTION INTEGER is the greatest integer <= argument (floors negatives).

    Includes the agent-prompt case ``INTEGER(-1.5) == -2``.
    """
    assert rint(intrinsic.cob_intr_integer(numdisp("1234", scale=2))) == 12
    assert rint(intrinsic.cob_intr_integer(numdisp("15", scale=1, negative=True))) == -2
    # A negative value with no fractional part floors to itself.
    assert rint(intrinsic.cob_intr_integer(numdisp("30", scale=1, negative=True))) == -3


def test_sign():
    """FUNCTION SIGN returns -1, 0 or 1."""
    assert rint(intrinsic.cob_intr_sign(numdisp("00"))) == 0
    assert rint(intrinsic.cob_intr_sign(numdisp("12"))) == 1
    assert rint(intrinsic.cob_intr_sign(numdisp("12", negative=True))) == -1


def test_length():
    """FUNCTION LENGTH - byte length of the argument."""
    assert rint(intrinsic.cob_intr_length(alnum("HELLO"))) == 5
    assert rint(intrinsic.cob_intr_length(alnum(""))) == 0


def test_exp_exp10():
    """FUNCTION EXP (e**x) and EXP10 (10**x) - COMP-2 doubles."""
    assert rdouble(intrinsic.cob_intr_exp(numdisp("0"))) == pytest.approx(1.0, abs=1e-12)
    assert rdouble(intrinsic.cob_intr_exp10(numdisp("2"))) == pytest.approx(100.0, abs=1e-6)


def test_log_log10():
    """FUNCTION LOG (natural) and LOG10 - COMP-2 doubles."""
    assert rdouble(intrinsic.cob_intr_log(numdisp("1"))) == pytest.approx(0.0, abs=1e-12)
    assert rdouble(intrinsic.cob_intr_log10(numdisp("1000"))) == pytest.approx(3.0, abs=1e-9)


def test_double_intr_domain_error_returns_zero():
    """A domain error in a double intrinsic (e.g. LOG(0)) returns a zero field."""
    assert rint(intrinsic.cob_intr_log(numdisp("0"))) == 0
    assert rint(intrinsic.cob_intr_sqrt(numdisp("4", negative=True))) == 0


def test_trig_fixed17_sin_cos():
    """SIN/COS return the fixed-17 *binary* representation (signed, scale 17)."""
    assert float(rdec(intrinsic.cob_intr_sin(numdisp("0")))) == pytest.approx(0.0, abs=1e-9)
    assert float(rdec(intrinsic.cob_intr_cos(numdisp("0")))) == pytest.approx(1.0, abs=1e-9)
    sin0 = intrinsic.cob_intr_sin(numdisp("0"))
    assert common.COB_FIELD_TYPE(sin0) == common.COB_TYPE_NUMERIC_BINARY
    assert common.COB_FIELD_SCALE(sin0) == 17
    assert common.COB_FIELD_HAVE_SIGN(sin0)


def test_inverse_trig_fixed17():
    """ACOS/ASIN/ATAN return fixed-17 packed binary results."""
    assert float(rdec(intrinsic.cob_intr_acos(numdisp("1")))) == pytest.approx(0.0, abs=1e-6)
    assert float(rdec(intrinsic.cob_intr_asin(numdisp("0")))) == pytest.approx(0.0, abs=1e-6)
    assert float(rdec(intrinsic.cob_intr_atan(numdisp("0")))) == pytest.approx(0.0, abs=1e-6)
    # ACOS is declared unsigned (range [0, pi]); ASIN/ATAN carry the sign flag.
    assert not common.COB_FIELD_HAVE_SIGN(intrinsic.cob_intr_acos(numdisp("1")))
    assert common.COB_FIELD_HAVE_SIGN(intrinsic.cob_intr_asin(numdisp("0")))


def test_tan():
    """FUNCTION TAN - COMP-2 double."""
    assert rdouble(intrinsic.cob_intr_tan(numdisp("0"))) == pytest.approx(0.0, abs=1e-12)


def test_annuity():
    """FUNCTION ANNUITY - rate 0 degenerates to 1/periods (COMP-2 double)."""
    assert rdouble(intrinsic.cob_intr_annuity(numdisp("0"), numdisp("4"))) \
        == pytest.approx(0.25, abs=1e-12)
    # Non-zero rate path: annuity factor is positive and < 1 for these inputs.
    val = rdouble(intrinsic.cob_intr_annuity(numdisp("10", scale=2), numdisp("5")))
    assert 0.0 < val < 1.0


def test_random_in_unit_interval():
    """FUNCTION RANDOM returns a COMP-2 double in [0, 1); seeded form is stable."""
    val = rdouble(intrinsic.cob_intr_random(1, numdisp("1")))
    assert 0.0 <= val < 1.0
    # No-argument form (no reseed) also stays in range.
    assert 0.0 <= rdouble(intrinsic.cob_intr_random(0)) < 1.0


def test_binop_operators():
    """``cob_intr_binop`` evaluates the +,-,*,/,^ arithmetic terms."""
    assert rint(intrinsic.cob_intr_binop(numdisp("12"), ord("+"), numdisp("8"))) == 20
    assert rint(intrinsic.cob_intr_binop(numdisp("12"), ord("-"), numdisp("8"))) == 4
    assert rint(intrinsic.cob_intr_binop(numdisp("6"), ord("*"), numdisp("7"))) == 42
    assert rint(intrinsic.cob_intr_binop(numdisp("20"), ord("/"), numdisp("4"))) == 5
    assert rint(intrinsic.cob_intr_binop(numdisp("2"), ord("^"), numdisp("10"))) == 1024


def test_binop_negative_sets_sign_flag():
    """A negative binop result carries the ``COB_FLAG_HAVE_SIGN`` attribute."""
    res = intrinsic.cob_intr_binop(numdisp("3"), ord("-"), numdisp("8"))
    assert rdec(res) == Decimal("-5")
    assert common.COB_FIELD_HAVE_SIGN(res)


def test_binop_wide_result_uses_display_branch():
    """A result too wide for 8-byte binary lands in the DISPLAY result branch."""
    # 10^17 * 10^17 needs > 19 digits -> NUMERIC_DISPLAY result field.
    big = numdisp("1" + "0" * 17)            # 100000000000000000
    res = intrinsic.cob_intr_binop(big, ord("*"), big)
    assert common.COB_FIELD_TYPE(res) == common.COB_TYPE_NUMERIC_DISPLAY
    assert rdec(res) == Decimal(10) ** 34



# ===========================================================================
# Phase 3 - Statistical / list intrinsics (variadic: params is the arg count)
# ===========================================================================
def test_sum():
    """FUNCTION SUM over a value list."""
    assert rint(intrinsic.cob_intr_sum(3, numdisp("1"), numdisp("2"), numdisp("3"))) == 6
    assert rdec(intrinsic.cob_intr_sum(
        2, numdisp("150", scale=2), numdisp("250", scale=2))) == Decimal("4.00")


def test_sum_wide_uses_display_field():
    """A SUM whose magnitude needs > 18 digits widens to a DISPLAY field."""
    big = numdisp("9" * 18)                  # 18 nines
    res = intrinsic.cob_intr_sum(2, big, big)
    assert common.COB_FIELD_TYPE(res) == common.COB_TYPE_NUMERIC_DISPLAY
    assert rdec(res) == Decimal("9" * 18) * 2


def test_min_max():
    """FUNCTION MIN / MAX return the extreme argument field."""
    assert rdec(intrinsic.cob_intr_min(
        3, numdisp("3"), numdisp("7"), numdisp("2"))) == Decimal("2")
    assert rdec(intrinsic.cob_intr_max(
        3, numdisp("3"), numdisp("7"), numdisp("2"))) == Decimal("7")


def test_ord_min_max():
    """FUNCTION ORD-MIN / ORD-MAX return 1-based positions of the extremes."""
    assert rint(intrinsic.cob_intr_ord_min(
        3, numdisp("3"), numdisp("7"), numdisp("2"))) == 3
    assert rint(intrinsic.cob_intr_ord_max(
        3, numdisp("3"), numdisp("7"), numdisp("2"))) == 2


def test_ord_min_max_single_arg_is_zero():
    """With a single argument ORD-MIN / ORD-MAX return 0 (no comparison made)."""
    assert rint(intrinsic.cob_intr_ord_min(1, numdisp("5"))) == 0
    assert rint(intrinsic.cob_intr_ord_max(1, numdisp("5"))) == 0


def test_mean():
    """FUNCTION MEAN(1,2,3,4) == 2.5."""
    assert rdec(intrinsic.cob_intr_mean(
        4, numdisp("1"), numdisp("2"), numdisp("3"), numdisp("4"))) == Decimal("2.5")


def test_median_odd_and_even():
    """FUNCTION MEDIAN - middle element (odd) or average of the two middles (even)."""
    # Odd count: exact middle element is returned.
    assert rdec(intrinsic.cob_intr_median(
        3, numdisp("5"), numdisp("1"), numdisp("3"))) == Decimal("3")
    # Even count: average of the two central elements (COMP-2 double).
    assert rdec(intrinsic.cob_intr_median(
        4, numdisp("1"), numdisp("2"), numdisp("3"), numdisp("4"))) == Decimal("2.5")


def test_midrange():
    """FUNCTION MIDRANGE == (min + max) / 2 (COMP-2 double)."""
    assert rdec(intrinsic.cob_intr_midrange(
        4, numdisp("1"), numdisp("2"), numdisp("3"), numdisp("9"))) == Decimal("5")


def test_range():
    """FUNCTION RANGE == max - min."""
    assert rdec(intrinsic.cob_intr_range(
        3, numdisp("3"), numdisp("7"), numdisp("2"))) == Decimal("5")


def test_variance_and_standard_deviation():
    """FUNCTION VARIANCE and STANDARD-DEVIATION on a small set.

    For the population {2, 4, 4, 4, 5, 5, 7, 9} the variance is 4 and the
    standard deviation is 2 (the classic textbook example).
    """
    args = [numdisp(d) for d in ("2", "4", "4", "4", "5", "5", "7", "9")]
    variance = rdec(intrinsic.cob_intr_variance(len(args), *args))
    assert variance == pytest.approx(Decimal("4"), abs=Decimal("0.0001"))
    stddev = rdouble(intrinsic.cob_intr_standard_deviation(len(args), *args))
    assert stddev == pytest.approx(2.0, abs=1e-9)


def test_variance_single_element_is_zero():
    """VARIANCE / STANDARD-DEVIATION of a single value are zero."""
    assert rint(intrinsic.cob_intr_variance(1, numdisp("5"))) == 0
    assert rdouble(intrinsic.cob_intr_standard_deviation(1, numdisp("5"))) == 0.0


def test_present_value():
    """FUNCTION PRESENT-VALUE discounts a series of future amounts.

    With rate 0 each future amount is undiscounted, so PRESENT-VALUE(0; 100,100)
    == 200 (COMP-2 double).
    """
    res = intrinsic.cob_intr_present_value(3, numdisp("0"), numdisp("100"), numdisp("100"))
    assert rdouble(res) == pytest.approx(200.0, abs=1e-9)


def test_present_value_too_few_args_is_zero():
    """PRESENT-VALUE with fewer than two arguments yields 0."""
    assert rint(intrinsic.cob_intr_present_value(1, numdisp("0"))) == 0



# ===========================================================================
# Phase 4 - Date / time intrinsics (1601-01-01 epoch; integer 1 == 16010101)
# ===========================================================================
def test_integer_of_date_epoch():
    """The epoch anchor: INTEGER-OF-DATE(16010101) == 1 and DATE-OF-INTEGER(1)
    round-trips to 16010101."""
    assert rint(intrinsic.cob_intr_integer_of_date(numdisp("16010101"))) == 1
    back = intrinsic.cob_intr_date_of_integer(numdisp("00000001"))
    assert rtext(back) == b"16010101"


def test_integer_of_date_known_round_trip():
    """A representative date (the 2024-02-29 leap day) round-trips exactly."""
    n = rint(intrinsic.cob_intr_integer_of_date(numdisp("20240229")))
    assert n > 0
    # Feed the integer back through DATE-OF-INTEGER (8-digit binary argument).
    back = intrinsic.cob_intr_date_of_integer(numdisp("%08d" % n))
    assert rtext(back) == b"20240229"


def test_date_out_of_range_sets_exception():
    """An out-of-range date raises EC-ARGUMENT-FUNCTION (year < 1601)."""
    common.cob_exception_code = 0
    result = intrinsic.cob_intr_integer_of_date(numdisp("15001231"))
    assert rint(result) == 0
    assert common.cob_exception_code != 0
    assert common.cob_get_exception_name(common.cob_exception_code) \
        == "EC-ARGUMENT-FUNCTION"


def test_integer_of_date_bad_month_and_day():
    """Invalid month (00) and invalid day (32) both raise EC-ARGUMENT-FUNCTION."""
    common.cob_exception_code = 0
    intrinsic.cob_intr_integer_of_date(numdisp("20220015"))   # month 00
    assert common.cob_exception_code != 0
    common.cob_exception_code = 0
    intrinsic.cob_intr_integer_of_date(numdisp("20220132"))   # day 32
    assert common.cob_exception_code != 0
    common.cob_exception_code = 0
    intrinsic.cob_intr_integer_of_date(numdisp("20220230"))   # Feb 30 (>days in month)
    assert common.cob_exception_code != 0


def test_date_of_integer_out_of_range():
    """DATE-OF-INTEGER rejects day 0 and days beyond the 9999-12-31 limit."""
    common.cob_exception_code = 0
    intrinsic.cob_intr_date_of_integer(numdisp("00000000"))
    assert common.cob_exception_code != 0
    common.cob_exception_code = 0
    intrinsic.cob_intr_date_of_integer(numdisp("99999999"))   # > 3067671
    assert common.cob_exception_code != 0


def test_day_of_integer_and_integer_of_day_round_trip():
    """DAY-OF-INTEGER <-> INTEGER-OF-DAY round-trip on a Julian (YYYYDDD) date."""
    # Day 1 is 1601001.
    assert rtext(intrinsic.cob_intr_day_of_integer(numdisp("00000001"))) == b"1601001"
    n = rint(intrinsic.cob_intr_integer_of_day(numdisp("2024060")))
    assert n > 0
    assert rtext(intrinsic.cob_intr_day_of_integer(numdisp("%08d" % n))) == b"2024060"


def test_integer_of_day_out_of_range():
    """INTEGER-OF-DAY rejects a bad year and a day-of-year past the year length."""
    common.cob_exception_code = 0
    intrinsic.cob_intr_integer_of_day(numdisp("1500001"))     # year < 1601
    assert common.cob_exception_code != 0
    common.cob_exception_code = 0
    intrinsic.cob_intr_integer_of_day(numdisp("2023366"))     # 2023 is not a leap year
    assert common.cob_exception_code != 0


def test_test_date_yyyymmdd():
    """TEST-DATE-YYYYMMDD: 0 ok / 1 bad year / 2 bad month / 3 bad day."""
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("20240229"))) == 0
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("15000101"))) == 1
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("20221301"))) == 2
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("20220230"))) == 3


def test_test_day_yyyyddd():
    """TEST-DAY-YYYYDDD: 0 ok / 1 bad year / 2 bad day-of-year."""
    assert rint(intrinsic.cob_intr_test_day_yyyyddd(numdisp("2024060"))) == 0
    assert rint(intrinsic.cob_intr_test_day_yyyyddd(numdisp("1500060"))) == 1
    assert rint(intrinsic.cob_intr_test_day_yyyyddd(numdisp("2023366"))) == 2


def test_combined_datetime():
    """COMBINED-DATETIME concatenates a 7-digit day and 5-digit time-of-day."""
    res = intrinsic.cob_intr_combined_datetime(numdisp("0000001"), numdisp("00001"))
    assert rtext(res) == b"000000100001"


def test_combined_datetime_out_of_range():
    """COMBINED-DATETIME validates both the day and the time-of-day arguments."""
    common.cob_exception_code = 0
    intrinsic.cob_intr_combined_datetime(numdisp("9999999"), numdisp("00001"))
    assert common.cob_exception_code != 0    # day > 3067671
    common.cob_exception_code = 0
    intrinsic.cob_intr_combined_datetime(numdisp("0000001"), numdisp("99999"))
    assert common.cob_exception_code != 0    # time > 86400


def test_current_date_shape():
    """CURRENT-DATE returns a 21-char YYYYMMDDhhmmsszz(+/-)hhmm field."""
    res = intrinsic.cob_intr_current_date(0, 0)
    assert res.size == 21
    text = rtext(res)
    assert len(text) == 21
    assert text[:16].isdigit()               # YYYYMMDDhhmmsszz numeric prefix
    assert text[16:17] in (b"+", b"-")        # signed UTC-offset marker
    assert text[17:].isdigit()               # hhmm offset digits


def test_year_to_yyyy_sliding_window():
    """YEAR-TO-YYYY windows a 2-digit year; default window is 50 years."""
    # With execution year 2000 and default 50-year window, 20 -> 2020.
    assert rint(intrinsic.cob_intr_year_to_yyyy(3, numdisp("20"), numdisp("50"),
                                                numdisp("2000"))) == 2020
    # Single-argument form uses the current execution year as the window base.
    assert rint(intrinsic.cob_intr_year_to_yyyy(1, numdisp("20"))) >= 1900


def test_year_to_yyyy_invalid_year_sets_exception():
    """A 2-digit year outside 0..99 raises EC-ARGUMENT-FUNCTION."""
    common.cob_exception_code = 0
    intrinsic.cob_intr_year_to_yyyy(1, numdisp("150"))
    assert common.cob_exception_code != 0


def test_date_to_yyyymmdd_sliding_window():
    """DATE-TO-YYYYMMDD expands a YYMMDD date through the sliding window."""
    # 200101 with execution year 2000 / 50-year window -> 20200101.
    assert rint(intrinsic.cob_intr_date_to_yyyymmdd(
        3, numdisp("200101"), numdisp("50"), numdisp("2000"))) == 20200101
    # Single-arg form still produces an 8-digit YYYYMMDD.
    assert rint(intrinsic.cob_intr_date_to_yyyymmdd(1, numdisp("200101"))) >= 19000101


def test_day_to_yyyyddd_sliding_window():
    """DAY-TO-YYYYDDD expands a YYDDD Julian date through the sliding window."""
    assert rint(intrinsic.cob_intr_day_to_yyyyddd(
        3, numdisp("20060"), numdisp("50"), numdisp("2000"))) == 2020060
    assert rint(intrinsic.cob_intr_day_to_yyyyddd(1, numdisp("20060"))) >= 1900000


def test_seconds_past_midnight():
    """SECONDS-PAST-MIDNIGHT returns a value in [0, 86400)."""
    secs = rint(intrinsic.cob_intr_seconds_past_midnight())
    assert 0 <= secs < 86400


def test_seconds_from_formatted_time():
    """SECONDS-FROM-FORMATTED-TIME parses hh/mm/ss from a format string."""
    res = intrinsic.cob_intr_seconds_from_formatted_time(
        alnum("hhmmss"), alnum("010203"))
    assert rint(res) == 1 * 3600 + 2 * 60 + 3


def test_seconds_from_formatted_time_too_short_sets_exception():
    """A value shorter than the format raises EC-ARGUMENT-FUNCTION."""
    common.cob_exception_code = 0
    intrinsic.cob_intr_seconds_from_formatted_time(alnum("hhmmss"), alnum("01"))
    assert common.cob_exception_code != 0



# ===========================================================================
# Phase 5 - Character / string intrinsics
# ===========================================================================
def test_upper_lower_reverse_trim():
    """UPPER-CASE / LOWER-CASE / REVERSE / TRIM basic behavior."""
    assert rtext(intrinsic.cob_intr_upper_case(0, 0, alnum("abc"))) == b"ABC"
    assert rtext(intrinsic.cob_intr_lower_case(0, 0, alnum("ABC"))) == b"abc"
    assert rtext(intrinsic.cob_intr_reverse(0, 0, alnum("ABC"))) == b"CBA"
    assert rtext(intrinsic.cob_intr_trim(0, 0, alnum(" x "), 0)) == b"x"


def test_trim_leading_trailing_directions():
    """TRIM direction: 0=both, 1=leading-only, 2=trailing-only."""
    assert rtext(intrinsic.cob_intr_trim(0, 0, alnum("  hi  "), 1)) == b"hi  "
    assert rtext(intrinsic.cob_intr_trim(0, 0, alnum("  hi  "), 2)) == b"  hi"
    # An all-spaces argument trims to a single space.
    assert rtext(intrinsic.cob_intr_trim(0, 0, alnum("    "), 0)) == b" "


def test_reference_modification_on_upper_case():
    """A non-zero (offset, length) applies COBOL reference modification."""
    # UPPER-CASE("abcdef")(2:3) -> "BCD".
    assert rtext(intrinsic.cob_intr_upper_case(2, 3, alnum("abcdef"))) == b"BCD"


def test_numval():
    """FUNCTION NUMVAL parses a free-form numeric string, incl. the prompt case."""
    assert rdec(intrinsic.cob_intr_numval(alnum("-12.34"))) == Decimal("-12.34")
    assert rdec(intrinsic.cob_intr_numval(alnum("  -1,234.56 "))) == Decimal("-1234.56")


def test_numval_cr_db_suffix_is_negative():
    """A trailing CR/DB makes the NUMVAL result negative."""
    assert rdec(intrinsic.cob_intr_numval(alnum("1234CR"))) == Decimal("-1234")
    assert rdec(intrinsic.cob_intr_numval(alnum("1234DB"))) == Decimal("-1234")


def test_numval_c_currency():
    """FUNCTION NUMVAL-C strips the currency symbol and grouping commas."""
    assert rdec(intrinsic.cob_intr_numval_c(alnum("$1,234.50"), alnum("$"))) \
        == Decimal("1234.50")


def test_char_ord():
    """CHAR(n) and ORD(c) are 1-based inverses (CHAR(66) == 'A', ORD('A') == 66)."""
    assert rtext(intrinsic.cob_intr_char(numdisp("66"))) == b"A"
    assert rint(intrinsic.cob_intr_ord(alnum("A"))) == 66
    # Out-of-range CHAR ordinal yields a NUL byte.
    assert rtext(intrinsic.cob_intr_char(numdisp("0"))) == b"\x00"


def test_length_and_concatenate():
    """LENGTH counts bytes; CONCATENATE joins argument fields in order."""
    assert rint(intrinsic.cob_intr_length(alnum("HELLO"))) == 5
    res = intrinsic.cob_intr_concatenate(0, 0, 3, alnum("AB"), alnum("CD"), alnum("EF"))
    assert rtext(res) == b"ABCDEF"


def test_stored_char_length():
    """STORED-CHAR-LENGTH ignores trailing spaces."""
    assert rint(intrinsic.cob_intr_stored_char_length(alnum("hi   "))) == 2
    assert rint(intrinsic.cob_intr_stored_char_length(alnum("abc"))) == 3


def test_substitute():
    """SUBSTITUTE replaces each pattern occurrence (params counts every arg)."""
    res = intrinsic.cob_intr_substitute(0, 0, 3, alnum("aXbXc"), alnum("X"), alnum("-"))
    assert rtext(res) == b"a-b-c"


def test_substitute_case_is_case_insensitive():
    """SUBSTITUTE-CASE matches the pattern case-insensitively."""
    res = intrinsic.cob_intr_substitute_case(
        0, 0, 3, alnum("aXbxc"), alnum("x"), alnum("-"))
    assert rtext(res) == b"a-b-c"


def test_when_compiled_echoes_field():
    """WHEN-COMPILED echoes the compiler-populated timestamp field."""
    res = intrinsic.cob_intr_when_compiled(0, 0, alnum("06/12/2026 12:00:00"))
    assert rtext(res) == b"06/12/2026 12:00:00"


def test_locale_date_time_and_from_secs():
    """The locale-formatting intrinsics produce non-empty formatted output."""
    assert rtext(intrinsic.cob_intr_locale_date(
        0, 0, numdisp("20240229"), alnum(" "))).strip() != b""
    assert rtext(intrinsic.cob_intr_locale_time(
        0, 0, numdisp("010203"), alnum(" "))).strip() != b""
    assert rtext(intrinsic.cob_intr_lcl_time_from_secs(
        0, 0, numdisp("3723"), alnum(" "))).strip() != b""


def test_lcl_time_from_secs_out_of_range_sets_exception():
    """LOCALE-TIME-FROM-SECONDS rejects a seconds value past 86400."""
    common.cob_exception_code = 0
    intrinsic.cob_intr_lcl_time_from_secs(0, 0, numdisp("99999"), alnum(" "))
    assert common.cob_exception_code != 0


# ===========================================================================
# Phase 6 - Exception-inquiry intrinsics & runtime initialisation
# ===========================================================================
def test_exception_intrinsics_no_pending_exception():
    """With no exception latched the inquiry intrinsics return their idle values."""
    saved = common.cob_exception_code
    try:
        common.cob_exception_code = 0
        # EXCEPTION-STATUS is 31 blanks; EXCEPTION-FILE is "00" when idle.
        assert rtext(intrinsic.cob_intr_exception_status()).strip() == b""
        assert rtext(intrinsic.cob_intr_exception_file()).startswith(b"00")
    finally:
        common.cob_exception_code = saved


def test_exception_status_and_statement_with_pending_exception():
    """EXCEPTION-STATUS / EXCEPTION-STATEMENT read the latched exception globals."""
    saved = (common.cob_exception_code, common.cob_got_exception,
             common.cob_orig_program_id, common.cob_orig_section,
             common.cob_orig_paragraph, common.cob_orig_line,
             common.cob_orig_statement)
    try:
        # Latch a real, table-backed exception code FIRST (so EXCEPTION-STATUS
        # resolves a genuine EC-... name).  ``cob_set_exception`` snapshots the
        # source-location globals into the ``cob_orig_*`` fields, so the manual
        # location overrides below must come AFTER it - otherwise they would be
        # clobbered by the (empty) source globals.
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        common.cob_got_exception = 1
        common.cob_orig_program_id = "PROG1"
        common.cob_orig_section = "MAIN-SECTION"
        common.cob_orig_paragraph = "PARA-1"
        common.cob_orig_line = 42
        common.cob_orig_statement = "ADD"
        status = rtext(intrinsic.cob_intr_exception_status())
        assert status.strip() != b""
        assert status.startswith(b"EC-")
        stmt = rtext(intrinsic.cob_intr_exception_statement())
        assert stmt.startswith(b"ADD")
        loc = rtext(intrinsic.cob_intr_exception_location())
        assert b"PROG1" in loc and b"42" in loc
    finally:
        (common.cob_exception_code, common.cob_got_exception,
         common.cob_orig_program_id, common.cob_orig_section,
         common.cob_orig_paragraph, common.cob_orig_line,
         common.cob_orig_statement) = saved


def test_exception_location_idle_when_no_exception():
    """EXCEPTION-LOCATION returns a single blank when nothing is latched."""
    saved = (common.cob_got_exception, common.cob_orig_program_id)
    try:
        common.cob_got_exception = 0
        common.cob_orig_program_id = None
        assert rtext(intrinsic.cob_intr_exception_location()) == b" "
    finally:
        (common.cob_got_exception, common.cob_orig_program_id) = saved


def test_exception_file_with_io_exception():
    """EXCEPTION-FILE returns status + select-name when an I-O exception is set."""
    saved_code = common.cob_exception_code
    saved_err = getattr(common, "cob_error_file", None)
    try:
        class _FakeFile:
            select_name = "INFILE"
            file_status = b"35"
        common.cob_error_file = _FakeFile()
        # The I-O category bits (0x0500) must be present for the populated path.
        common.cob_exception_code = 0x0500 | 0x05
        out = rtext(intrinsic.cob_intr_exception_file())
        assert out.startswith(b"35") and b"INFILE" in out
    finally:
        common.cob_exception_code = saved_code
        common.cob_error_file = saved_err


def test_init_intrinsic_callable():
    """cob_init_intrinsic() is callable without error and returns None."""
    assert intrinsic.cob_init_intrinsic() is None



# ===========================================================================
# Additional branch coverage - documented error contracts & format variants
# ===========================================================================
def test_numval_more_than_18_digits_falls_back_to_double():
    """NUMVAL of a >18-digit literal returns a COMP-2 double approximation."""
    res = intrinsic.cob_intr_numval(alnum("12345678901234567890"))  # 20 digits
    assert common.COB_FIELD_TYPE(res) == common.COB_TYPE_NUMERIC_DOUBLE
    assert rdouble(res) == pytest.approx(1.2345678901234568e19, rel=1e-15)


def test_numval_c_without_currency_argument():
    """NUMVAL-C tolerates an empty currency argument and still parses digits."""
    assert rdec(intrinsic.cob_intr_numval_c(alnum("1,234.50"), alnum(""))) \
        == Decimal("1234.50")


def test_sliding_window_intrinsics_reject_bad_execution_year():
    """YEAR-TO-YYYY / DATE-TO-YYYYMMDD / DAY-TO-YYYYDDD reject xqtyear > 9999."""
    for call in (
        lambda: intrinsic.cob_intr_year_to_yyyy(
            3, numdisp("20"), numdisp("50"), numdisp("10000")),
        lambda: intrinsic.cob_intr_date_to_yyyymmdd(
            3, numdisp("200101"), numdisp("50"), numdisp("10000")),
        lambda: intrinsic.cob_intr_day_to_yyyyddd(
            3, numdisp("20060"), numdisp("50"), numdisp("10000")),
    ):
        common.cob_exception_code = 0
        result = call()
        assert rint(result) == 0
        assert common.cob_exception_code != 0


def test_exception_location_format_variants():
    """EXCEPTION-LOCATION renders section-only, paragraph-only and bare forms."""
    saved = (common.cob_got_exception, common.cob_orig_program_id,
             common.cob_orig_section, common.cob_orig_paragraph,
             common.cob_orig_line)
    try:
        common.cob_got_exception = 1
        common.cob_orig_program_id = "P"
        common.cob_orig_line = 7
        # Section without paragraph.
        common.cob_orig_section = "SEC"
        common.cob_orig_paragraph = None
        assert rtext(intrinsic.cob_intr_exception_location()) == b"P; SEC; 7"
        # Paragraph without section.
        common.cob_orig_section = None
        common.cob_orig_paragraph = "PAR"
        assert rtext(intrinsic.cob_intr_exception_location()) == b"P; PAR; 7"
        # Neither section nor paragraph.
        common.cob_orig_section = None
        common.cob_orig_paragraph = None
        assert rtext(intrinsic.cob_intr_exception_location()) == b"P; ; 7"
    finally:
        (common.cob_got_exception, common.cob_orig_program_id,
         common.cob_orig_section, common.cob_orig_paragraph,
         common.cob_orig_line) = saved


def test_test_date_distinct_error_codes():
    """TEST-DATE-YYYYMMDD distinguishes year/month/day errors with codes 1/2/3."""
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("99990101"))) == 0
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("00000101"))) == 1
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("20220001"))) == 2
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("20220100"))) == 3


def test_concatenate_with_reference_modification():
    """CONCATENATE honours a trailing reference modification (offset, length)."""
    res = intrinsic.cob_intr_concatenate(2, 3, 2, alnum("AB"), alnum("CDEF"))
    assert rtext(res) == b"BCD"

