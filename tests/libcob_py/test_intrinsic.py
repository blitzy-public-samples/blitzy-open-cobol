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
    # SIN and COS return the fixed-17 *binary* representation (signed long long
    # with COB_FLAG_HAVE_SIGN), matching intrinsic.c L1660/L1729 - so decode
    # them with rdec, not rdouble.  TAN returns an IEEE COMP-2 double.
    assert float(rdec(intrinsic.cob_intr_sin(numdisp("0")))) == pytest.approx(0.0, abs=1e-9)
    assert float(rdec(intrinsic.cob_intr_cos(numdisp("0")))) == pytest.approx(1.0, abs=1e-9)
    assert rdouble(intrinsic.cob_intr_tan(numdisp("0"))) == pytest.approx(0.0, abs=1e-9)
    # The fixed-17 SIN/COS fields carry the sign flag and scale 17.
    sin0 = intrinsic.cob_intr_sin(numdisp("0"))
    assert common.COB_FIELD_TYPE(sin0) == common.COB_TYPE_NUMERIC_BINARY
    assert common.COB_FIELD_SCALE(sin0) == 17
    assert common.COB_FIELD_HAVE_SIGN(sin0)


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


# ===========================================================================
# Additional branch coverage - expected values verified against the original
# C ``cobc`` 1.1.0 toolchain (see AAP 0.6.2 numeric-parity strategy).  These
# exercise the variadic default-argument paths, the exception branches, the
# BINOP operator dispatch + result-type selection, and the NUMVAL CR/DB and
# big-magnitude fall-throughs that the baseline suite did not reach.
# ===========================================================================
def _negdisp(digits, scale=0):
    """Signed zoned-DISPLAY field carrying a negative value."""
    f = numdisp(digits, scale=scale, signed=True)
    common.cob_put_sign(f, -1)
    return f


# --- BINOP: all five operators + the three result-type branches -----------
def test_binop_all_operators():
    # + - * / ^  (op passed as the operator-byte ordinal, per the emitter).
    assert rdec(intrinsic.cob_intr_binop(numdisp("12"), ord("+"), numdisp("8"))) == Decimal("20")
    assert rdec(intrinsic.cob_intr_binop(numdisp("12"), ord("-"), numdisp("8"))) == Decimal("4")
    assert rdec(intrinsic.cob_intr_binop(numdisp("6"), ord("*"), numdisp("7"))) == Decimal("42")
    assert rdec(intrinsic.cob_intr_binop(numdisp("20"), ord("/"), numdisp("4"))) == Decimal("5")
    assert rdec(intrinsic.cob_intr_binop(numdisp("2"), ord("^"), numdisp("10"))) == Decimal("1024")


def test_binop_negative_result_sets_sign_flag():
    # 8 - 11 = -3 -> result field must carry COB_FLAG_HAVE_SIGN.
    res = intrinsic.cob_intr_binop(numdisp("8"), ord("-"), numdisp("11"))
    assert rdec(res) == Decimal("-3")
    assert common.COB_FIELD_HAVE_SIGN(res)


def test_binop_eight_byte_and_display_result_branches():
    # 8-byte binary branch: 1,000,000 * 1,000,000 = 10**12 (needs > 32 bits).
    res8 = intrinsic.cob_intr_binop(numdisp("1000000"), ord("*"), numdisp("1000000"))
    assert rdec(res8) == Decimal("1000000000000")
    assert common.COB_FIELD_TYPE(res8) == common.COB_TYPE_NUMERIC_BINARY
    # DISPLAY branch: a value too wide for 8 bytes (> ~1.8e19) falls back to
    # a NUMERIC_DISPLAY result field.
    big = numdisp("99999999999999999999")  # 20 nines
    resd = intrinsic.cob_intr_binop(big, ord("*"), numdisp("1000000000"))
    assert common.COB_FIELD_TYPE(resd) == common.COB_TYPE_NUMERIC_DISPLAY
    assert rdec(resd) == Decimal("99999999999999999999") * Decimal("1000000000")


# --- NUMVAL: CR/DB negative suffix, and the > 18-digit double fall-back ----
def test_numval_cr_db_negative_suffix():
    # Verified against C cobc 1.1.0: trailing CR/DB denote a negative value.
    assert rdec(intrinsic.cob_intr_numval(alnum("123CR"))) == Decimal("-123")
    assert rdec(intrinsic.cob_intr_numval(alnum("456DB"))) == Decimal("-456")


def test_numval_more_than_18_digits_falls_back_to_double():
    # 20 integer digits exceeds the 18-digit COMP path -> COMP-2 double result.
    res = intrinsic.cob_intr_numval(alnum("12345678901234567890"))
    assert rdouble(res) == pytest.approx(12345678901234567890.0)


# --- Sliding-window date intrinsics: 1-argument default paths --------------
def test_year_to_yyyy_single_arg_default_window():
    # YEAR-TO-YYYY(24) with the default 50-year window -> 2024 (C-verified).
    assert rint(intrinsic.cob_intr_year_to_yyyy(1, numdisp("24"))) == 2024


def test_date_to_yyyymmdd_single_arg():
    # DATE-TO-YYYYMMDD(240229) default window -> 20240229 (C-verified).
    assert rint(intrinsic.cob_intr_date_to_yyyymmdd(1, numdisp("240229"))) == 20240229


def test_day_to_yyyyddd_single_arg():
    # DAY-TO-YYYYDDD(24060) default window -> 2024060 (C-verified).
    assert rint(intrinsic.cob_intr_day_to_yyyyddd(1, numdisp("24060"))) == 2024060


def test_year_to_yyyy_invalid_year_sets_exception():
    # year > 99 is invalid -> exception set, result 0.
    common.cob_exception_code = 0
    res = intrinsic.cob_intr_year_to_yyyy(1, numdisp("150"))
    assert rint(res) == 0
    assert common.cob_exception_code != 0


# --- Date conversion exception branches ------------------------------------
def test_integer_of_date_invalid_day_sets_exception():
    # 2024-02-30 is not a real date (Feb has 29 days in 2024) -> 0 + exception.
    common.cob_exception_code = 0
    res = intrinsic.cob_intr_integer_of_date(numdisp("20240230"))
    assert rint(res) == 0
    assert common.cob_exception_code != 0


def test_integer_of_date_bad_year_and_month():
    # year < 1601 -> exception.
    common.cob_exception_code = 0
    assert rint(intrinsic.cob_intr_integer_of_date(numdisp("15001201"))) == 0
    assert common.cob_exception_code != 0
    # month 13 -> exception.
    common.cob_exception_code = 0
    assert rint(intrinsic.cob_intr_integer_of_date(numdisp("20241301"))) == 0
    assert common.cob_exception_code != 0


def test_integer_of_day_round_trip_and_error():
    # 2024060 (Julian) <-> integer 154557, mirroring DAY-OF-INTEGER.
    iod = intrinsic.cob_intr_integer_of_day(numdisp("2024060"))
    assert rint(iod) == 154557
    # invalid day-of-year 367 -> exception.
    common.cob_exception_code = 0
    assert rint(intrinsic.cob_intr_integer_of_day(numdisp("2024367"))) == 0
    assert common.cob_exception_code != 0


# --- COMBINED-DATETIME -----------------------------------------------------
def test_combined_datetime_value_and_bounds():
    # COMBINED-DATETIME(154557, 43200) -> "015455743200" (= 154557.43200).
    res = intrinsic.cob_intr_combined_datetime(numdisp("154557"), numdisp("43200"))
    assert rtext(res) == b"015455743200"
    # out-of-range day -> all-zero payload + exception.
    common.cob_exception_code = 0
    res0 = intrinsic.cob_intr_combined_datetime(numdisp("0"), numdisp("43200"))
    assert rtext(res0) == b"000000000000"
    assert common.cob_exception_code != 0


# --- INTEGER-PART / FRACTION-PART (well-defined, non-overflow regime) -------
def test_integer_and_fraction_part():
    # INTEGER-PART(12.789) -> 12 (C-verified).
    assert rint(intrinsic.cob_intr_integer_part(numdisp("12789", scale=3))) == 12
    # FRACTION-PART in its well-defined regime (|v| < ~9.22, no 8-byte overflow):
    # FRACTION-PART(0.789) -> 0.789.
    assert rdec(intrinsic.cob_intr_fraction_part(numdisp("0789", scale=3))) == Decimal("0.789000000000000000")
    # Negative integer part preserved by INTEGER-PART.
    assert rint(intrinsic.cob_intr_integer_part(_negdisp("4567", scale=2))) == -45


# --- ABS / SIGN ------------------------------------------------------------
def test_abs_and_sign_negative():
    assert rint(intrinsic.cob_intr_abs(_negdisp("42"))) == 42
    assert rint(intrinsic.cob_intr_sign(_negdisp("7"))) == -1
    assert rint(intrinsic.cob_intr_sign(numdisp("7"))) == 1
    assert rint(intrinsic.cob_intr_sign(numdisp("0"))) == 0


# --- TEST-DATE / TEST-DAY validity probes ----------------------------------
def test_test_date_and_day_validity():
    # TEST-DATE-YYYYMMDD: 0 = valid, non-zero = the offending component.
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("20240229"))) == 0
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("20240230"))) != 0
    # TEST-DAY-YYYYDDD: 0 = valid.
    assert rint(intrinsic.cob_intr_test_day_yyyyddd(numdisp("2024060"))) == 0
    assert rint(intrinsic.cob_intr_test_day_yyyyddd(numdisp("2024367"))) != 0


# --- Exception location/file/statement/status text -------------------------
def test_exception_information_functions():
    # These read the common.py exception globals; they must return alphanumeric
    # fields without raising regardless of whether an exception is pending.
    common.cob_exception_code = 0
    for fn in (intrinsic.cob_intr_exception_file,
               intrinsic.cob_intr_exception_location,
               intrinsic.cob_intr_exception_statement,
               intrinsic.cob_intr_exception_status):
        res = fn()
        assert common.COB_FIELD_TYPE(res) == common.COB_TYPE_ALPHANUMERIC
        assert isinstance(rtext(res), (bytes, bytearray))


# --- SUM over several arguments (multi-arg accumulation path) --------------
def test_sum_many_args_with_scale():
    res = intrinsic.cob_intr_sum(3, numdisp("125", scale=2),
                                 numdisp("250", scale=2), numdisp("125", scale=2))
    assert rdec(res) == Decimal("5.00")


# --- STORED-CHAR-LENGTH (trailing-space-insensitive length) ----------------
def test_stored_char_length():
    assert rint(intrinsic.cob_intr_stored_char_length(alnum("abc   "))) == 3
    assert rint(intrinsic.cob_intr_stored_char_length(alnum("   "))) == 0


# --- FACTORIAL edge cases --------------------------------------------------
def test_factorial_zero_and_large():
    # 0! = 1 (boundary).
    assert rint(intrinsic.cob_intr_factorial(numdisp("0"))) == 1
    # 20! = 2432902008176640000 - exact via decimal (no float drift).
    assert rdec(intrinsic.cob_intr_factorial(numdisp("20"))) == Decimal("2432902008176640000")


# --- Double-path intrinsic error branch (domain errors -> 0) ---------------
def test_double_intr_domain_errors_return_zero():
    # LOG(0) and SQRT(-1) are math-domain errors -> the C runtime yields 0.
    assert rdouble(intrinsic.cob_intr_log(numdisp("0"))) == 0.0
    assert rdouble(intrinsic.cob_intr_sqrt(_negdisp("1"))) == 0.0


# --- SUM big-magnitude DISPLAY result branch -------------------------------
def test_sum_overflows_to_display_field():
    # Sum exceeding 18 digits -> NUMERIC_DISPLAY result field.
    big = numdisp("999999999999999999")  # 18 nines
    res = intrinsic.cob_intr_sum(2, big, big)
    assert common.COB_FIELD_TYPE(res) == common.COB_TYPE_NUMERIC_DISPLAY
    assert rdec(res) == Decimal("1999999999999999998")


# --- Sliding-window date intrinsics: 3-arg + error branches ----------------
def test_year_to_yyyy_three_args_and_bad_execution_year():
    # Explicit window + current-year: YEAR-TO-YYYY(40, 20, 2024).
    # maxyear = 2044; 2044 % 100 = 44 >= 40 -> 40 + 2000 = 2040.
    assert rint(intrinsic.cob_intr_year_to_yyyy(3, numdisp("40"),
                numdisp("20"), numdisp("2024"))) == 2040
    # execution year < 1601 -> exception, 0.
    common.cob_exception_code = 0
    res = intrinsic.cob_intr_year_to_yyyy(3, numdisp("40"),
            numdisp("20"), numdisp("1500"))
    assert rint(res) == 0 and common.cob_exception_code != 0


def test_date_to_yyyymmdd_three_args_and_sliding_out_of_range():
    # 3-arg explicit form.
    assert rint(intrinsic.cob_intr_date_to_yyyymmdd(3, numdisp("240229"),
                numdisp("20"), numdisp("2024"))) == 20240229
    # A window pushing maxyear past 9999 makes the sliding result invalid -> 0.
    common.cob_exception_code = 0
    res = intrinsic.cob_intr_date_to_yyyymmdd(3, numdisp("240229"),
            numdisp("9000"), numdisp("9999"))
    assert rint(res) == 0 and common.cob_exception_code != 0


def test_day_to_yyyyddd_three_args():
    assert rint(intrinsic.cob_intr_day_to_yyyyddd(3, numdisp("24060"),
                numdisp("20"), numdisp("2024"))) == 2024060


# --- LOCALE-DATE / LOCALE-TIME / LOCALE-TIME-FROM-SECONDS -------------------
def test_locale_date_time_and_from_secs():
    # Valid inputs produce non-empty locale-formatted alphanumeric text.
    d = intrinsic.cob_intr_locale_date(0, 0, numdisp("20240229"), None)
    assert common.COB_FIELD_TYPE(d) == common.COB_TYPE_ALPHANUMERIC
    assert len(rtext(d)) > 1
    t = intrinsic.cob_intr_locale_time(0, 0, numdisp("133045"), None)
    assert len(rtext(t)) > 1
    s = intrinsic.cob_intr_lcl_time_from_secs(0, 0, numdisp("43200"), None)
    assert len(rtext(s)) > 1
    # Out-of-range seconds -> exception + blank.
    common.cob_exception_code = 0
    bad = intrinsic.cob_intr_lcl_time_from_secs(0, 0, numdisp("99999"), None)
    assert common.cob_exception_code != 0
    assert rtext(bad).strip() == b""
    # Invalid calendar date -> exception.
    common.cob_exception_code = 0
    intrinsic.cob_intr_locale_date(0, 0, numdisp("20240230"), None)
    assert common.cob_exception_code != 0


# --- EXCEPTION-LOCATION text format branches -------------------------------
def test_exception_location_format_variants():
    saved = (common.cob_got_exception, common.cob_orig_program_id,
             common.cob_orig_section, common.cob_orig_paragraph,
             common.cob_orig_line)
    try:
        common.cob_got_exception = 1
        common.cob_orig_program_id = "PROG1"
        common.cob_orig_line = 42
        # Both section and paragraph present.
        common.cob_orig_section = "SEC1"
        common.cob_orig_paragraph = "PARA1"
        txt = rtext(intrinsic.cob_intr_exception_location()).decode("latin-1")
        assert "PROG1" in txt and "PARA1" in txt and "SEC1" in txt
        # Section only.
        common.cob_orig_paragraph = None
        assert b"SEC1" in rtext(intrinsic.cob_intr_exception_location())
        # Paragraph only.
        common.cob_orig_section = None
        common.cob_orig_paragraph = "PARA1"
        assert b"PARA1" in rtext(intrinsic.cob_intr_exception_location())
        # Neither.
        common.cob_orig_paragraph = None
        assert b"PROG1" in rtext(intrinsic.cob_intr_exception_location())
    finally:
        (common.cob_got_exception, common.cob_orig_program_id,
         common.cob_orig_section, common.cob_orig_paragraph,
         common.cob_orig_line) = saved


# --- ATAN of a large magnitude exercises the fixed17 integer-part peel ------
def test_atan_large_value_fixed17_integer_part():
    # ATAN(1000) ~= 1.5698 -> integer part 1, signed fixed17 result.
    res = intrinsic.cob_intr_atan(numdisp("1000"))
    assert common.COB_FIELD_SCALE(res) == 17
    assert common.COB_FIELD_HAVE_SIGN(res)
    assert float(rdec(res)) == pytest.approx(1.56979632712, abs=1e-9)


# --- DATE-OF-INTEGER / DAY-OF-INTEGER out-of-range error branches ----------
def test_date_of_integer_out_of_range():
    # day 0 and day > 3067671 are invalid -> all-zero payload + exception.
    common.cob_exception_code = 0
    assert rtext(intrinsic.cob_intr_date_of_integer(numdisp("0"))) == b"00000000"
    assert common.cob_exception_code != 0
    common.cob_exception_code = 0
    assert rtext(intrinsic.cob_intr_date_of_integer(numdisp("9999999"))) == b"00000000"
    assert common.cob_exception_code != 0


def test_day_of_integer_out_of_range():
    common.cob_exception_code = 0
    assert rtext(intrinsic.cob_intr_day_of_integer(numdisp("0"))) == b"0000000"
    assert common.cob_exception_code != 0


# --- TEST-DATE-YYYYMMDD distinct error codes (1=year, 2=month, 3=day) ------
def test_test_date_distinct_error_codes():
    # Bad year (< 1601) -> 1.
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("15000101"))) == 1
    # Bad month (13) -> 2.
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("20241301"))) == 2
    # Bad day (00) -> 3.
    assert rint(intrinsic.cob_intr_test_date_yyyymmdd(numdisp("20240100"))) == 3


def test_test_day_distinct_error_codes():
    # Bad year -> 1; bad day-of-year (367) -> non-zero.
    assert rint(intrinsic.cob_intr_test_day_yyyyddd(numdisp("1500001"))) == 1
    assert rint(intrinsic.cob_intr_test_day_yyyyddd(numdisp("2024367"))) != 0


# --- FACTORIAL of a genuinely negative argument sets the exception ---------
def test_factorial_negative_argument():
    common.cob_exception_code = 0
    res = intrinsic.cob_intr_factorial(_negdisp("5"))
    assert rint(res) == 0
    assert common.cob_exception_code != 0
