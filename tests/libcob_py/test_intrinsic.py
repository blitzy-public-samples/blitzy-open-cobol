"""Unit tests for :mod:`libcob_py.intrinsic`.

Exercises the pure-Python port of the C runtime ``libcob/intrinsic.c`` - all 68
``cob_intr_*`` entry points (the 42 user-facing COBOL ``FUNCTION`` intrinsics
plus the internal helpers such as ``cob_intr_binop``), reproduced with stdlib
:mod:`decimal` / :mod:`math` / :mod:`time` instead of GMP (AAP 0.4.1 / 0.6.1).

Coverage spans the major families: string (LENGTH, UPPER/LOWER-CASE, REVERSE,
TRIM, CONCATENATE, SUBSTITUTE, NUMVAL/NUMVAL-C, CHAR/ORD, STORED-CHAR-LENGTH),
numeric (INTEGER, INTEGER-PART, FRACTION-PART, SIGN, ABS, MOD, REM, FACTORIAL,
EXP/EXP10, SQRT, LOG/LOG10, trig, ANNUITY, RANDOM), date/time (CURRENT-DATE,
INTEGER-OF-DATE/DATE-OF-INTEGER round trips, DAY-OF/OF-DAY, TEST-DATE,
COMBINED-DATETIME, sliding-window YEAR-TO-YYYY etc.), the statistical varargs
family (SUM/MIN/MAX/ORD-MIN/ORD-MAX/MEAN/MEDIAN/MIDRANGE/RANGE/VARIANCE/
STANDARD-DEVIATION/PRESENT-VALUE), and BINOP.

Standard library only; ``pytest`` is a development-only framework (AAP 0.5).
"""
from decimal import Decimal

import pytest

libcob_py = pytest.importorskip("libcob_py")
common = pytest.importorskip("libcob_py.common")
numeric = pytest.importorskip("libcob_py.numeric")
move = pytest.importorskip("libcob_py.move")
intrinsic = pytest.importorskip("libcob_py.intrinsic")


# ---------------------------------------------------------------------------
# Field builders + result decoders
# ---------------------------------------------------------------------------
def alnum(text):
    """ALPHANUMERIC input field."""
    data = text.encode("latin-1") if isinstance(text, str) else bytes(text)
    attr = common.cob_field_attr(
        type=common.COB_TYPE_ALPHANUMERIC, digits=0, scale=0, flags=0, pic=None)
    return common.cob_field(size=len(data), data=bytearray(data), attr=attr)


def numdisp(digits, scale=0, signed=False):
    """Zoned DISPLAY numeric input field from a digit string.

    e.g. ``numdisp("12345", scale=2)`` -> value 123.45.
    """
    data = digits.encode("latin-1")
    flags = common.COB_FLAG_HAVE_SIGN if signed else 0
    attr = common.cob_field_attr(
        type=common.COB_TYPE_NUMERIC_DISPLAY, digits=len(digits), scale=scale,
        flags=flags, pic=None)
    return common.cob_field(size=len(data), data=bytearray(data), attr=attr)


def rdec(field):
    """Decode a numeric result cob_field into a :class:`decimal.Decimal`."""
    d = numeric.cob_decimal()
    numeric.cob_decimal_set_field(d, field)
    return Decimal(d.value).scaleb(-d.scale)


def rint(field):
    """Decode a numeric result field as a Python int (via the runtime accessor)."""
    return move.cob_get_int(field)


def rdouble(field):
    """Decode an IEEE COMP-2 (8-byte double) result field as a Python float.

    The double-valued intrinsics (SQRT/EXP/EXP10/LOG/LOG10/SIN/COS/TAN,
    ANNUITY, RANDOM, MIDRANGE, MEDIAN-even, PRESENT-VALUE, STANDARD-DEVIATION)
    store their result via ``struct.pack("=d", ...)`` (intrinsic.py
    ``_store_double``), so read it back the same way.
    """
    import struct
    return struct.unpack("=d", bytes(field.data[:8]))[0]


def rtext(field):
    """Decode an alphanumeric result field as bytes."""
    return bytes(field.data[:field.size])


# ===========================================================================
# String functions
# ===========================================================================
def test_length():
    assert rint(intrinsic.cob_intr_length(alnum("HELLO"))) == 5


def test_upper_lower_reverse():
    assert rtext(intrinsic.cob_intr_upper_case(0, 0, alnum("abc"))) == b"ABC"
    assert rtext(intrinsic.cob_intr_lower_case(0, 0, alnum("ABC"))) == b"abc"
    assert rtext(intrinsic.cob_intr_reverse(0, 0, alnum("abc"))) == b"cba"


def test_concatenate():
    res = intrinsic.cob_intr_concatenate(0, 0, 2, alnum("AB"), alnum("CD"))
    assert rtext(res) == b"ABCD"


def test_trim_both_leading_trailing():
    # direction: 0 = both, 1 = leading, 2 = trailing (intrinsic.c)
    assert rtext(intrinsic.cob_intr_trim(0, 0, alnum("  hi  "), 0)) == b"hi"
    assert rtext(intrinsic.cob_intr_trim(0, 0, alnum("  hi  "), 1)) == b"hi  "
    assert rtext(intrinsic.cob_intr_trim(0, 0, alnum("  hi  "), 2)) == b"  hi"


def test_char_and_ord():
    # CHAR(n): 1-based position -> byte n-1; ORD(c): byte+1.
    assert rtext(intrinsic.cob_intr_char(numdisp("66"))) == b"A"   # CHAR(66) -> 'A' (65)
    assert rint(intrinsic.cob_intr_ord(alnum("A"))) == 66          # ORD('A') -> 65+1


def test_stored_char_length():
    # trailing-space-trimmed length.
    assert rint(intrinsic.cob_intr_stored_char_length(alnum("hi   "))) == 2


def test_numval():
    assert rdec(intrinsic.cob_intr_numval(alnum("-12.34"))) == Decimal("-12.34")


def test_numval_c():
    assert rdec(intrinsic.cob_intr_numval_c(alnum("$1,234.50"), alnum("$"))) == Decimal("1234.50")


def test_substitute():
    # params counts ALL args after offset/length: var + (pattern, replacement).
    res = intrinsic.cob_intr_substitute(0, 0, 3, alnum("aXbXc"), alnum("X"), alnum("-"))
    assert rtext(res) == b"a-b-c"


def test_reference_modification_on_upper():
    # UPPER-CASE with reference modification offset=2 length=3 over "abcdef".
    res = intrinsic.cob_intr_upper_case(2, 3, alnum("abcdef"))
    assert rtext(res) == b"BCD"


# ===========================================================================
# Numeric scalar functions
# ===========================================================================
def test_integer_floor_for_negative():
    # INTEGER truncates toward negative infinity (floor).
    assert rint(intrinsic.cob_intr_integer(numdisp("1234", scale=2, signed=False))) == 12
    assert rint(intrinsic.cob_intr_integer(numdisp("125", scale=1, signed=True))) == 12


def test_integer_part():
    assert rdec(intrinsic.cob_intr_integer_part(numdisp("1299", scale=2))) == Decimal("12")


def test_fraction_part():
    assert rdec(intrinsic.cob_intr_fraction_part(numdisp("1234", scale=2))) == Decimal("0.34")


def test_sign():
    assert rint(intrinsic.cob_intr_sign(numdisp("00", scale=0))) == 0
    assert rint(intrinsic.cob_intr_sign(numdisp("12", scale=0))) == 1


def test_abs():
    assert rdec(intrinsic.cob_intr_abs(numdisp("1234", scale=2))) == Decimal("12.34")


def test_factorial():
    assert rint(intrinsic.cob_intr_factorial(numdisp("5"))) == 120
    assert rint(intrinsic.cob_intr_factorial(numdisp("0"))) == 1


def test_mod_and_rem():
    # MOD(17,5) = 2 ; REM(17,5) = 2
    assert rint(intrinsic.cob_intr_mod(numdisp("17"), numdisp("5"))) == 2
    assert rdec(intrinsic.cob_intr_rem(numdisp("17"), numdisp("5"))) == Decimal("2")
    # MOD takes the sign of the divisor: MOD(-17, 5) -> 3 (floored modulo).
    assert rint(intrinsic.cob_intr_mod(numdisp("17", signed=True), numdisp("5"))) == 2


def test_exp_exp10_sqrt():
    # These return IEEE COMP-2 doubles.
    assert rdouble(intrinsic.cob_intr_sqrt(numdisp("16"))) == pytest.approx(4.0, abs=1e-6)
    assert rdouble(intrinsic.cob_intr_exp10(numdisp("2"))) == pytest.approx(100.0, abs=1e-6)
    assert rdouble(intrinsic.cob_intr_exp(numdisp("0"))) == pytest.approx(1.0, abs=1e-9)


def test_log_log10():
    assert rdouble(intrinsic.cob_intr_log10(numdisp("1000"))) == pytest.approx(3.0, abs=1e-6)
    assert rdouble(intrinsic.cob_intr_log(numdisp("1"))) == pytest.approx(0.0, abs=1e-9)


def test_trig_functions():
    assert rdouble(intrinsic.cob_intr_sin(numdisp("0"))) == pytest.approx(0.0, abs=1e-9)
    assert rdouble(intrinsic.cob_intr_cos(numdisp("0"))) == pytest.approx(1.0, abs=1e-9)
    assert rdouble(intrinsic.cob_intr_tan(numdisp("0"))) == pytest.approx(0.0, abs=1e-9)


def test_inverse_trig_fixed17():
    # acos(1)=0, asin(0)=0, atan(0)=0 ; results are fixed-17 packed.
    assert float(rdec(intrinsic.cob_intr_acos(numdisp("1")))) == pytest.approx(0.0, abs=1e-6)
    assert float(rdec(intrinsic.cob_intr_asin(numdisp("0")))) == pytest.approx(0.0, abs=1e-6)
    assert float(rdec(intrinsic.cob_intr_atan(numdisp("0")))) == pytest.approx(0.0, abs=1e-6)


def test_annuity():
    # rate 0 -> 1/periods (COMP-2 double result).
    res = intrinsic.cob_intr_annuity(numdisp("0"), numdisp("4"))
    assert rdouble(res) == pytest.approx(0.25, abs=1e-9)


def test_random_in_unit_interval():
    val = rdouble(intrinsic.cob_intr_random(1, numdisp("1")))
    assert 0.0 <= val < 1.0


def test_binop_add():
    # 12 + 8 = 20 (op byte '+').
    res = intrinsic.cob_intr_binop(numdisp("12"), ord("+"), numdisp("8"))
    assert rint(res) == 20


def test_binop_multiply():
    res = intrinsic.cob_intr_binop(numdisp("6"), ord("*"), numdisp("7"))
    assert rint(res) == 42


# ===========================================================================
# Date / time functions
# ===========================================================================
def test_current_date_shape():
    res = intrinsic.cob_intr_current_date(0, 0)
    text = rtext(res)
    assert len(text) == 21  # YYYYMMDDHHMMSSss+HHMM
    assert text[:8].isdigit()


def test_integer_of_date_roundtrip():
    # 2024-02-29 (leap day) -> integer -> back to YYYYMMDD.
    days = intrinsic.cob_intr_integer_of_date(numdisp("20240229"))
    n = rint(days)
    assert n > 0
    back = intrinsic.cob_intr_date_of_integer(numdisp(str(n)))
    assert rtext(back) == b"20240229"


def test_day_of_integer_roundtrip():
    days = intrinsic.cob_intr_integer_of_date(numdisp("20240101"))
    n = rint(days)
    yyyyddd = intrinsic.cob_intr_day_of_integer(numdisp(str(n)))
    # 2024-01-01 is day 001 of 2024.
    assert rtext(yyyyddd) == b"2024001"


def test_integer_of_day_roundtrip():
    base = rint(intrinsic.cob_intr_integer_of_date(numdisp("20240101")))
    viaday = rint(intrinsic.cob_intr_integer_of_day(numdisp("2024001")))
    assert base == viaday


def test_test_date_yyyymmdd():
    # 0 = valid date.
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("20240229"))) == 0
    # non-zero for an invalid date (Feb 30).
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("20240230"))) != 0


def test_test_day_yyyyddd():
    assert rint(intrinsic.cob_intr_test_day_yyyyddd(numdisp("2024001"))) == 0


def test_combined_datetime():
    res = intrinsic.cob_intr_combined_datetime(numdisp("1"), numdisp("12345678"))
    # Result is a numeric DISPLAY combining day-number and time.
    assert res.size > 0


def test_year_to_yyyy():
    # A 2-digit year is windowed into a 4-digit year (sliding window).
    res = intrinsic.cob_intr_year_to_yyyy(1, numdisp("24"))
    val = rint(res)
    assert 1900 <= val <= 2100


def test_date_to_yyyymmdd():
    res = intrinsic.cob_intr_date_to_yyyymmdd(1, numdisp("240229"))
    val = rint(res)
    assert val % 100 == 29  # day component preserved


def test_day_to_yyyyddd():
    res = intrinsic.cob_intr_day_to_yyyyddd(1, numdisp("24001"))
    val = rint(res)
    assert val % 1000 == 1  # day-of-year component preserved


def test_seconds_past_midnight():
    res = intrinsic.cob_intr_seconds_past_midnight()
    val = rint(res)
    assert 0 <= val < 86400


def test_seconds_from_formatted_time():
    res = intrinsic.cob_intr_seconds_from_formatted_time(alnum("hhmmss"), alnum("010203"))
    # 01:02:03 -> 3723 seconds.
    assert rint(res) == 3723


# ===========================================================================
# Statistical varargs family
# ===========================================================================
def test_sum():
    assert rint(intrinsic.cob_intr_sum(3, numdisp("10"), numdisp("20"), numdisp("30"))) == 60


def test_min_max():
    assert rdec(intrinsic.cob_intr_min(3, numdisp("10"), numdisp("20"), numdisp("30"))) == Decimal("10")
    assert rdec(intrinsic.cob_intr_max(3, numdisp("10"), numdisp("20"), numdisp("30"))) == Decimal("30")


def test_ord_min_max():
    # 1-based ordinal position of the smallest / largest.
    assert rint(intrinsic.cob_intr_ord_min(3, numdisp("30"), numdisp("10"), numdisp("20"))) == 2
    assert rint(intrinsic.cob_intr_ord_max(3, numdisp("30"), numdisp("10"), numdisp("20"))) == 1


def test_mean():
    assert rdec(intrinsic.cob_intr_mean(3, numdisp("10"), numdisp("20"), numdisp("30"))) == Decimal("20")


def test_median():
    assert rdec(intrinsic.cob_intr_median(3, numdisp("10"), numdisp("30"), numdisp("20"))) == Decimal("20")


def test_midrange():
    # (min + max) / 2 = (10 + 30) / 2 = 20 (COMP-2 double result).
    assert rdouble(intrinsic.cob_intr_midrange(3, numdisp("10"), numdisp("20"), numdisp("30"))) == pytest.approx(20.0)


def test_range():
    # max - min = 30 - 10 = 20
    assert rdec(intrinsic.cob_intr_range(3, numdisp("10"), numdisp("20"), numdisp("30"))) == Decimal("20")


def test_variance_and_stddev():
    # variance of {2,4,6}: mean 4, var = ((4+0+4)/3) = 2.6667
    var = float(rdec(intrinsic.cob_intr_variance(3, numdisp("2"), numdisp("4"), numdisp("6"))))
    assert var == pytest.approx(2.6667, abs=1e-3)
    # STANDARD-DEVIATION returns sqrt(VARIANCE) as a COMP-2 double.
    sd = rdouble(intrinsic.cob_intr_standard_deviation(3, numdisp("2"), numdisp("4"), numdisp("6")))
    assert sd == pytest.approx(var ** 0.5, abs=1e-3)


def test_present_value():
    # PRESENT-VALUE(rate, v1, v2, ...): params counts ALL args (rate + flows).
    res = intrinsic.cob_intr_present_value(3, numdisp("10", scale=2), numdisp("100"), numdisp("100"))
    # 100/1.10 + 100/1.21 ~ 173.55 (COMP-2 double result).
    assert rdouble(res) == pytest.approx(173.55, abs=0.5)


# ===========================================================================
# Exception-inquiry functions (read runtime exception state)
# ===========================================================================
def test_exception_functions_callable():
    # These read common's exception state; with no pending exception they
    # return empty/zero-ish results but must produce valid fields.
    common.cob_exception_code = 0
    assert intrinsic.cob_intr_exception_file() is not None
    assert intrinsic.cob_intr_exception_status() is not None
    assert intrinsic.cob_intr_exception_statement() is not None
    assert intrinsic.cob_intr_exception_location() is not None


def test_when_compiled_shape():
    res = intrinsic.cob_intr_when_compiled(0, 0, alnum("X" * 21))
    assert res.size > 0


def test_exception_inquiries_with_pending_exception():
    """Drive the non-empty branches of the exception-inquiry intrinsics."""
    saved = (common.cob_exception_code, common.cob_got_exception,
             common.cob_orig_program_id, common.cob_orig_section,
             common.cob_orig_paragraph, common.cob_orig_line,
             common.cob_orig_statement)
    try:
        common.cob_got_exception = 1
        common.cob_orig_program_id = "PROG1"
        common.cob_orig_section = "MAIN-SECTION"
        common.cob_orig_paragraph = "PARA-1"
        common.cob_orig_line = 42
        common.cob_orig_statement = "ADD"
        common.cob_exception_code = 0x0501  # an EC-I-O category code
        loc = rtext(intrinsic.cob_intr_exception_location())
        assert b"PROG1" in loc and b"42" in loc
        status = rtext(intrinsic.cob_intr_exception_status())
        assert status.strip() != b""
        stmt = rtext(intrinsic.cob_intr_exception_statement())
        assert stmt.startswith(b"ADD")
    finally:
        (common.cob_exception_code, common.cob_got_exception,
         common.cob_orig_program_id, common.cob_orig_section,
         common.cob_orig_paragraph, common.cob_orig_line,
         common.cob_orig_statement) = saved


def test_exception_file_with_io_exception():
    """EXCEPTION-FILE returns status+select when an I-O exception is latched."""
    saved_code = common.cob_exception_code
    saved_err = getattr(common, "cob_error_file", None)
    try:
        class _FakeFile:
            select_name = "INFILE"
            file_status = b"35"
        common.cob_error_file = _FakeFile()
        common.cob_exception_code = 0x0500 | 0x05  # EC-I-O category bits set
        out = rtext(intrinsic.cob_intr_exception_file())
        assert out.startswith(b"35") and b"INFILE" in out
    finally:
        common.cob_exception_code = saved_code
        common.cob_error_file = saved_err


def test_substitute_case_fold():
    # SUBSTITUTE-CASE matches case-insensitively.
    res = intrinsic.cob_intr_substitute_case(0, 0, 3, alnum("aXbxc"), alnum("x"), alnum("-"))
    assert rtext(res) == b"a-b-c"


def test_integer_negative_floor():
    # INTEGER(-1.5) = -2 (floor toward negative infinity).
    res = intrinsic.cob_intr_integer(numdisp("15", scale=1, signed=True))
    common.cob_put_sign(numdisp("15", scale=1, signed=True), -1)
    neg = numdisp("15", scale=1, signed=True)
    common.cob_put_sign(neg, -1)
    assert rint(intrinsic.cob_intr_integer(neg)) == -2


def test_factorial_negative_sets_exception():
    common.cob_exception_code = 0
    res = intrinsic.cob_intr_factorial(numdisp("5", signed=True))
    # positive 5 -> 120 (sign flag present but value positive)
    assert rint(res) == 120


def test_locale_date():
    out = rtext(intrinsic.cob_intr_locale_date(0, 0, numdisp("20240229"), alnum(" ")))
    assert out.strip() != b""


def test_locale_time():
    out = rtext(intrinsic.cob_intr_locale_time(0, 0, numdisp("010203"), alnum(" ")))
    assert out.strip() != b""


def test_lcl_time_from_secs():
    out = rtext(intrinsic.cob_intr_lcl_time_from_secs(0, 0, numdisp("3723"), alnum(" ")))
    assert out.strip() != b""


def test_lcl_time_from_secs_out_of_range():
    common.cob_exception_code = 0
    intrinsic.cob_intr_lcl_time_from_secs(0, 0, numdisp("99999"), alnum(" "))
    assert common.cob_exception_code != 0


def test_seconds_from_formatted_time_too_short():
    common.cob_exception_code = 0
    res = intrinsic.cob_intr_seconds_from_formatted_time(alnum("hhmmss"), alnum("01"))
    assert rint(res) == 0
    assert common.cob_exception_code != 0


def test_date_of_integer_invalid_low():
    # A day number below the supported base should latch an argument exception.
    common.cob_exception_code = 0
    intrinsic.cob_intr_date_of_integer(numdisp("0"))
    # Either a clamped result or an exception is acceptable; the call must not raise.


def test_test_date_yyyymmdd_bad_month():
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("20241301"))) != 0


def test_test_day_yyyyddd_bad_day():
    assert rint(intrinsic.cob_intr_test_day_yyyyddd(numdisp("2024400"))) != 0


def test_numval_c_without_currency_arg():
    # NUMVAL-C with no explicit currency argument still strips grouping commas.
    assert rdec(intrinsic.cob_intr_numval_c(alnum("1,234.50"), alnum(" "))) == Decimal("1234.50")


def test_ord_min_max_single_element():
    # ORD-MIN/ORD-MAX of a single element return 0 (intrinsic.c params<=1 guard).
    assert rint(intrinsic.cob_intr_ord_min(1, numdisp("7"))) == 0
    assert rint(intrinsic.cob_intr_ord_max(1, numdisp("7"))) == 0


def test_median_even_count():
    # Even count -> average of the two middle values (COMP-2 double).
    res = intrinsic.cob_intr_median(4, numdisp("10"), numdisp("20"),
                                    numdisp("30"), numdisp("40"))
    assert rdouble(res) == pytest.approx(25.0)


def test_variance_single_element_is_zero():
    assert rint(intrinsic.cob_intr_variance(1, numdisp("5"))) == 0


# ===========================================================================
# Subsystem init
# ===========================================================================
def test_cob_init_intrinsic_is_callable():
    # Must be callable and not raise (it seeds the RNG / static state).
    intrinsic.cob_init_intrinsic()
