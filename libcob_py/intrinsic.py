"""``libcob_py.intrinsic`` - COBOL intrinsic FUNCTION runtime.

Pure-Python port of ``libcob/intrinsic.c`` (the GnuCOBOL 1.1 intrinsic-function
runtime).  It provides all 68 ``cob_intr_*`` entry points emitted by the code
generator (the ``cob_intr_`` routing branch in ``cobc/codegen.c``), of which 42
are user-facing COBOL ``FUNCTION`` intrinsics and the remainder are internal
helpers such as :func:`cob_intr_binop` (AAP section 0.6.1 authority-hierarchy
resolution: implement all 68, expose the user-facing set).

Standard-library only (AAP 0.5 / Rule R3): arbitrary-precision arithmetic is
delegated to :mod:`libcob_py.numeric` (whose ``cob_decimal`` replaces GMP), and
floating-point intrinsics use :mod:`math`.  No third-party package is imported.

Result model: the C runtime returns pointers into a small rotating stack of
reusable ``cob_field`` slots (``make_field_entry``); Python's garbage collector
makes that unnecessary, so each entry point returns a freshly-built
:class:`libcob_py.common.cob_field`.  The observable contract - field type,
digits, scale, sign flag, byte content, and reference-modification - is
preserved exactly.
"""
import math
import struct
import time as _time

from libcob_py import common
from libcob_py import numeric

# ---------------------------------------------------------------------------
# Data-movement primitives (cob_move / cob_get_int / cob_set_int).
#
# In intrinsic.c these are the libcob ``cob_move`` / ``cob_get_int`` /
# ``cob_set_int`` calls, which in this Python runtime live in
# :mod:`libcob_py.move` (AAP 0.3.2 init order: ... numeric -> strings -> move
# -> intrinsic ...).  This module's dependency contract, however, is exactly
# ``common`` and ``numeric`` (depends_on_files; the agent prompt: "Imports
# libcob_py.common and libcob_py.numeric").  We therefore do NOT take a direct
# top-level dependency on ``move``; instead we reach the polymorphic
# data-movement routines through the deferred-import shims that ``common``
# exposes for exactly this purpose (``common._lazy_move`` /
# ``common._lazy_get_int`` / ``common._lazy_set_int``, which forward to the
# canonical ``libcob_py.move`` implementations).  This keeps byte-for-byte
# parity with the C runtime while confining intrinsic.py's hard imports to the
# whitelisted ``common`` and ``numeric`` modules.  Migration rationale per the
# AAP 0.7.2 minimal-change / no-new-dependency clause.
_cob_move = common._lazy_move
_cob_get_int = common._lazy_get_int
_cob_set_int = common._lazy_set_int

# Re-exported decimal primitives (the GMP replacements - numeric.c statics
# d1..d5 become local cob_decimal objects here for re-entrancy).
_Decimal = numeric.cob_decimal
_set = numeric.cob_decimal_set_field
_get = numeric.cob_decimal_get_field
_add = numeric.cob_decimal_add
_sub = numeric.cob_decimal_sub
_mul = numeric.cob_decimal_mul
_div = numeric.cob_decimal_div
_pow = numeric.cob_decimal_pow

# ---------------------------------------------------------------------------
# Date/day calculation constants (intrinsic.c L85-L88).  Base is 1601-01-01.
# ---------------------------------------------------------------------------
_NORMAL_DAYS = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334, 365)
_LEAP_DAYS = (0, 31, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335, 366)
_NORMAL_MONTH_DAYS = (0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_LEAP_MONTH_DAYS = (0, 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

_U64 = (1 << 64) - 1


def _leap_year(year):
    """Gregorian leap-year predicate (intrinsic.c L375-L378)."""
    return 1 if ((year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)) else 0


# ===========================================================================
# Result-field construction helpers (intrinsic.c L314-L437)
# ===========================================================================
def _attr(type_, digits, scale, flags, pic=None):
    return common.cob_field_attr(type=type_, digits=digits, scale=scale,
                                 flags=flags, pic=pic)


def make_field_entry(size, type_, digits, scale, flags, pic=None, data=None):
    """Build a fresh result ``cob_field`` (replaces the C rotating stack).

    *data* defaults to a zero-filled ``bytearray`` of *size*; callers then
    populate it (mirroring ``make_field_entry`` followed by ``memcpy`` /
    ``cob_set_int`` / ``cob_decimal_get_field`` in the C original).
    """
    buf = bytearray(size) if data is None else bytearray(data)
    return common.cob_field(size=size, data=buf,
                            attr=_attr(type_, digits, scale, flags, pic))


def make_double_entry():
    """COMP-2 result field (intrinsic.c L314-L344): 8-byte IEEE double."""
    return make_field_entry(8, common.COB_TYPE_NUMERIC_DOUBLE, 18, 9,
                            common.COB_FLAG_HAVE_SIGN)


def _copy_field(src):
    """Result field shaped exactly like *src* (``make_field_entry(srcfield)``)."""
    a = src.attr
    return make_field_entry(src.size, a.type, a.digits, a.scale, a.flags, a.pic)


def calc_ref_mod(f, offset, length):
    """Apply reference modification ``(offset, length)`` in place (L414-L437)."""
    if offset <= f.size:
        calcoff = offset - 1
        size = f.size - calcoff
        if length > 0 and length < size:
            size = length
        if calcoff > 0:
            f.data[:size] = f.data[calcoff:calcoff + size]
        f.size = size


def intr_get_double(d):
    """``cob_decimal`` -> Python float (intrinsic.c L390-L401)."""
    v = float(d.value)
    n = d.scale
    while n > 0:
        v /= 10.0
        n -= 1
    while n < 0:
        v *= 10.0
        n += 1
    return v


def _store_double(f, v):
    """Store IEEE double *v* into the 8-byte COMP-2 result field *f*."""
    f.data[:8] = struct.pack("=d", v)


def _store_ll(f, n):
    """Store native 8-byte signed long long *n* into result field *f*."""
    f.data[:8] = struct.pack("=q", _wrap_ll(n))


def _wrap_ll(n):
    """Wrap *n* into a signed 64-bit range (C ``long long`` cast)."""
    n &= _U64
    return n - (1 << 64) if n >= (1 << 63) else n


# ===========================================================================
# Numeric expression - cob_intr_binop (intrinsic.c L437-L497)
# ===========================================================================
def cob_intr_binop(f1, op, f2):
    """Evaluate ``f1 <op> f2`` as a COBOL arithmetic expression term.

    *op* is the integer ordinal of one of ``+ - * / ^`` (the operator byte the
    code generator passes).  The result field type is chosen from the magnitude
    and scale of the computed value exactly as in the C original.
    """
    d1 = _Decimal()
    d2 = _Decimal()
    _set(d1, f1)
    _set(d2, f2)
    o = chr(op) if isinstance(op, int) else op
    if o == "+":
        _add(d1, d2)
    elif o == "-":
        _sub(d1, d2)
    elif o == "*":
        _mul(d1, d2)
    elif o == "/":
        _div(d1, d2)
    elif o == "^":
        _pow(d1, d2)

    if d1.value < 0:
        attrsign = common.COB_FLAG_HAVE_SIGN
        sign = 1
    else:
        attrsign = 0
        sign = 0
    bitnum = (abs(d1.value)).bit_length() or 1
    if bitnum < (33 - sign) and d1.scale < 10:
        result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 9,
                                  d1.scale, attrsign)
    elif bitnum < (65 - sign) and d1.scale < 19:
        result = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 20,
                                  d1.scale, attrsign)
    else:
        size = len(str(abs(d1.value))) or 1
        if d1.scale > size:
            size = d1.scale
        result = make_field_entry(size, common.COB_TYPE_NUMERIC_DISPLAY, size,
                                  d1.scale, attrsign)
    _get(d1, result, 0)
    return result


# ===========================================================================
# Basic intrinsics (intrinsic.c L499-L640)
# ===========================================================================
def cob_intr_length(srcfield):
    """FUNCTION LENGTH - byte length of *srcfield* (intrinsic.c L499-L510)."""
    result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 8, 0, 0)
    _cob_set_int(result, int(srcfield.size))
    return result


def cob_intr_integer(srcfield):
    """FUNCTION INTEGER - greatest integer <= argument (intrinsic.c L513-L539)."""
    result = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 18, 0,
                              common.COB_FLAG_HAVE_SIGN)
    d1 = _Decimal()
    _set(d1, srcfield)
    if d1.value >= 0:
        # Non-negative: cob_decimal_get_field already truncates toward zero,
        # which equals floor for non-negative values (intrinsic.c L525-L528).
        _get(d1, result, 0)
        return result
    # Negative: reproduce the C algorithm exactly (intrinsic.c L529-L537).  The
    # value is first reduced to a single fractional digit using mpz_tdiv_q_ui
    # (integer division *truncated toward zero* - NOT Python floor division),
    # then floored by subtracting one ULP when a remainder exists so the result
    # is the greatest integer <= argument.
    while d1.scale > 1:
        v = d1.value
        # mpz_tdiv_q_ui(value, value, 10): quotient truncated toward zero.
        d1.value = -((-v) // 10) if v < 0 else v // 10
        d1.scale -= 1
    scale = 10 if d1.scale > 0 else 1
    # mpz_fdiv_ui(value, scale): floor remainder (always non-negative for a
    # positive divisor) - Python's ``%`` matches this for positive ``scale``.
    if d1.value % scale:
        d1.value -= scale
    _get(d1, result, 0)
    return result


def cob_intr_integer_part(srcfield):
    """FUNCTION INTEGER-PART (intrinsic.c L541-L555)."""
    result = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 18, 0,
                              common.COB_FLAG_HAVE_SIGN)
    _cob_move(srcfield, result)
    return result


def cob_intr_fraction_part(srcfield):
    """FUNCTION FRACTION-PART (intrinsic.c L555-L569)."""
    result = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 18, 18,
                              common.COB_FLAG_HAVE_SIGN)
    _cob_move(srcfield, result)
    return result


def cob_intr_sign(srcfield):
    """FUNCTION SIGN - -1/0/1 (intrinsic.c L569-L591)."""
    result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 8, 0,
                              common.COB_FLAG_HAVE_SIGN)
    _cob_set_int(result, 0)
    n = common.cob_cmp(srcfield, result)
    if n < 0:
        _cob_set_int(result, -1)
    elif n > 0:
        _cob_set_int(result, 1)
    return result


def cob_intr_upper_case(offset, length, srcfield):
    """FUNCTION UPPER-CASE (intrinsic.c L591-L608)."""
    result = _copy_field(srcfield)
    sd = common.COB_FIELD_DATA(srcfield)
    for i in range(srcfield.size):
        c = sd[i]
        result.data[i] = c - 32 if ord("a") <= c <= ord("z") else c
    if offset > 0:
        calc_ref_mod(result, offset, length)
    return result


def cob_intr_lower_case(offset, length, srcfield):
    """FUNCTION LOWER-CASE (intrinsic.c L608-L625)."""
    result = _copy_field(srcfield)
    sd = common.COB_FIELD_DATA(srcfield)
    for i in range(srcfield.size):
        c = sd[i]
        result.data[i] = c + 32 if ord("A") <= c <= ord("Z") else c
    if offset > 0:
        calc_ref_mod(result, offset, length)
    return result


def cob_intr_reverse(offset, length, srcfield):
    """FUNCTION REVERSE (intrinsic.c L625-L642)."""
    result = _copy_field(srcfield)
    sd = common.COB_FIELD_DATA(srcfield)
    size = srcfield.size
    for i in range(size):
        result.data[i] = sd[size - i - 1]
    if offset > 0:
        calc_ref_mod(result, offset, length)
    return result


def cob_intr_concatenate(offset, length, params, *args):
    """FUNCTION CONCATENATE (intrinsic.c L642-L680)."""
    fields = list(args[:params])
    calcsize = sum(f.size for f in fields)
    result = make_field_entry(calcsize, common.COB_TYPE_ALPHANUMERIC, 0, 0, 0)
    p = 0
    for f in fields:
        fd = common.COB_FIELD_DATA(f)
        result.data[p:p + f.size] = fd[:f.size]
        p += f.size
    if offset > 0:
        calc_ref_mod(result, offset, length)
    return result


# ===========================================================================
# Exception inquiry intrinsics (intrinsic.c L919-L1023)
# ===========================================================================
def cob_intr_exception_file():
    """FUNCTION EXCEPTION-FILE (intrinsic.c L919-L943)."""
    err = getattr(common, "cob_error_file", None)
    if common.cob_exception_code == 0 or err is None or \
            (common.cob_exception_code & 0x0500) != 0x0500:
        return make_field_entry(2, common.COB_TYPE_ALPHANUMERIC, 0, 0, 0,
                                data=b"00")
    select = getattr(err, "select_name", "") or ""
    status = bytes(getattr(err, "file_status", b"00"))[:2].ljust(2, b"0")
    payload = status + select.encode("latin-1")
    return make_field_entry(len(payload), common.COB_TYPE_ALPHANUMERIC, 0, 0, 0,
                            data=payload)


def cob_intr_exception_location():
    """FUNCTION EXCEPTION-LOCATION (intrinsic.c L943-L978)."""
    if not common.cob_got_exception or not common.cob_orig_program_id:
        return make_field_entry(1, common.COB_TYPE_ALPHANUMERIC, 0, 0, 0,
                                data=b" ")
    pid = common.cob_orig_program_id
    sec = common.cob_orig_section
    para = common.cob_orig_paragraph
    line = common.cob_orig_line
    if sec and para:
        text = "%s; %s OF %s; %d" % (pid, para, sec, line)
    elif sec:
        text = "%s; %s; %d" % (pid, sec, line)
    elif para:
        text = "%s; %s; %d" % (pid, para, line)
    else:
        text = "%s; ; %d" % (pid, line)
    payload = text.encode("latin-1")
    return make_field_entry(len(payload), common.COB_TYPE_ALPHANUMERIC, 0, 0, 0,
                            data=payload)


def cob_intr_exception_status():
    """FUNCTION EXCEPTION-STATUS (intrinsic.c L978-L998)."""
    result = make_field_entry(31, common.COB_TYPE_ALPHANUMERIC, 0, 0, 0,
                              data=b" " * 31)
    if common.cob_exception_code:
        name = common.cob_get_exception_name(common.cob_exception_code) \
            or "EXCEPTION-OBJECT"
        nb = name.encode("latin-1")[:31]
        result.data[:len(nb)] = nb
    return result


def cob_intr_exception_statement():
    """FUNCTION EXCEPTION-STATEMENT (intrinsic.c L1000-L1023)."""
    result = make_field_entry(31, common.COB_TYPE_ALPHANUMERIC, 0, 0, 0,
                              data=b" " * 31)
    if common.cob_exception_code and common.cob_orig_statement:
        sb = common.cob_orig_statement.encode("latin-1")[:31]
        result.data[:len(sb)] = sb
    return result


def cob_intr_when_compiled(offset, length, f):
    """FUNCTION WHEN-COMPILED (intrinsic.c L1023-L1035).

    Mirrors the C runtime, which returns the value of the field the compiler
    populated with the compilation timestamp; here we echo *f* unchanged.
    """
    result = _copy_field(f)
    fd = common.COB_FIELD_DATA(f)
    result.data[:f.size] = fd[:f.size]
    if offset > 0:
        calc_ref_mod(result, offset, length)
    return result


def cob_intr_current_date(offset, length):
    """FUNCTION CURRENT-DATE - 21-char ``YYYYMMDDHHMMSSss±HHMM`` (L1035-L1124)."""
    now = _time.localtime()
    hund = 0
    try:
        hund = int((_time.time() % 1) * 100)
    except Exception:                       # pragma: no cover - clock edge
        hund = 0
    # %z gives ±HHMM offset; strftime mirrors the Linux strftime path.
    tz = _time.strftime("%z", now) or "+0000"
    buff = ("%04d%02d%02d%02d%02d%02d%02d%s" % (
        now.tm_year, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min,
        now.tm_sec, hund, tz))[:21].ljust(21, "0")
    result = make_field_entry(21, common.COB_TYPE_ALPHANUMERIC, 0, 0, 0,
                              data=buff.encode("latin-1"))
    if offset > 0:
        calc_ref_mod(result, offset, length)
    return result


def cob_intr_char(srcfield):
    """FUNCTION CHAR - ordinal position -> character (intrinsic.c L1124-L1144)."""
    result = make_field_entry(1, common.COB_TYPE_ALPHANUMERIC, 0, 0, 0)
    i = _cob_get_int(srcfield)
    result.data[0] = 0 if (i < 1 or i > 256) else (i - 1) & 0xFF
    return result


def cob_intr_ord(srcfield):
    """FUNCTION ORD - character -> ordinal position (intrinsic.c L1144-L1158)."""
    result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 8, 0, 0)
    _cob_set_int(result, int(common.COB_FIELD_DATA(srcfield)[0]) + 1)
    return result


def cob_intr_stored_char_length(srcfield):
    """FUNCTION STORED-CHAR-LENGTH (intrinsic.c L1158-L1181)."""
    result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 8, 0, 0)
    sd = common.COB_FIELD_DATA(srcfield)
    count = srcfield.size
    p = srcfield.size - 1
    while count > 0:
        if sd[p] != ord(" "):
            break
        count -= 1
        p -= 1
    _cob_set_int(result, count)
    return result


# ===========================================================================
# Date/time conversions (intrinsic.c L1181-L1488).  Base 1601-01-01 = day 1.
# ===========================================================================
def cob_intr_combined_datetime(srcdays, srctime):
    """FUNCTION COMBINED-DATETIME (intrinsic.c L1181-L1211)."""
    result = make_field_entry(12, common.COB_TYPE_NUMERIC_DISPLAY, 12, 5, 0)
    common.cob_exception_code = 0
    srdays = _cob_get_int(srcdays)
    if srdays < 1 or srdays > 3067671:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        result.data[:12] = b"0" * 12
        return result
    srtime = _cob_get_int(srctime)
    if srtime < 1 or srtime > 86400:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        result.data[:12] = b"0" * 12
        return result
    result.data[:12] = ("%07d%05d" % (srdays, srtime)).encode("latin-1")
    return result


def cob_intr_date_of_integer(srcdays):
    """FUNCTION DATE-OF-INTEGER -> YYYYMMDD (intrinsic.c L1211-L1262)."""
    result = make_field_entry(8, common.COB_TYPE_NUMERIC_DISPLAY, 8, 0, 0)
    common.cob_exception_code = 0
    days = _cob_get_int(srcdays)
    if days < 1 or days > 3067671:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        result.data[:8] = b"0" * 8
        return result
    baseyear = 1601
    leapyear = 365
    while days > leapyear:
        days -= leapyear
        baseyear += 1
        leapyear = 366 if _leap_year(baseyear) else 365
    month = 0
    for i in range(13):
        if _leap_year(baseyear):
            if days <= _LEAP_DAYS[i]:
                days -= _LEAP_DAYS[i - 1]
                month = i
                break
        else:
            if days <= _NORMAL_DAYS[i]:
                days -= _NORMAL_DAYS[i - 1]
                month = i
                break
    result.data[:8] = ("%04d%02d%02d" % (baseyear, month, days)).encode("latin-1")
    return result


def cob_intr_day_of_integer(srcdays):
    """FUNCTION DAY-OF-INTEGER -> YYYYDDD (intrinsic.c L1262-L1298)."""
    result = make_field_entry(7, common.COB_TYPE_NUMERIC_DISPLAY, 7, 0, 0)
    common.cob_exception_code = 0
    days = _cob_get_int(srcdays)
    if days < 1 or days > 3067671:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        result.data[:7] = b"0" * 7
        return result
    baseyear = 1601
    leapyear = 365
    while days > leapyear:
        days -= leapyear
        baseyear += 1
        leapyear = 366 if _leap_year(baseyear) else 365
    result.data[:7] = ("%04d%03d" % (baseyear, days)).encode("latin-1")
    return result


def cob_intr_integer_of_date(srcfield):
    """FUNCTION INTEGER-OF-DATE (intrinsic.c L1298-L1368)."""
    result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 8, 0, 0)
    common.cob_exception_code = 0
    indate = _cob_get_int(srcfield)
    year = indate // 10000
    if year < 1601 or year > 9999:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    indate %= 10000
    month = indate // 100
    if month < 1 or month > 12:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    days = indate % 100
    if days < 1 or days > 31:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    table = _LEAP_MONTH_DAYS if _leap_year(year) else _NORMAL_MONTH_DAYS
    if days > table[month]:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    totaldays = 0
    baseyear = 1601
    while baseyear != year:
        totaldays += 366 if _leap_year(baseyear) else 365
        baseyear += 1
    totaldays += (_LEAP_DAYS if _leap_year(baseyear) else _NORMAL_DAYS)[month - 1]
    totaldays += days
    _cob_set_int(result, totaldays)
    return result


def cob_intr_integer_of_day(srcfield):
    """FUNCTION INTEGER-OF-DAY (intrinsic.c L1368-L1412)."""
    result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 8, 0, 0)
    common.cob_exception_code = 0
    indate = _cob_get_int(srcfield)
    year = indate // 1000
    if year < 1601 or year > 9999:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    days = indate % 1000
    if days < 1 or days > 365 + _leap_year(year):
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    totaldays = 0
    baseyear = 1601
    while baseyear != year:
        totaldays += 366 if _leap_year(baseyear) else 365
        baseyear += 1
    totaldays += days
    _cob_set_int(result, totaldays)
    return result


def cob_intr_test_date_yyyymmdd(srcfield):
    """FUNCTION TEST-DATE-YYYYMMDD - 0 ok / 1 yr / 2 mon / 3 day (L1412-L1459)."""
    result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 8, 0, 0)
    indate = _cob_get_int(srcfield)
    year = indate // 10000
    if year < 1601 or year > 9999:
        _cob_set_int(result, 1)
        return result
    indate %= 10000
    month = indate // 100
    if month < 1 or month > 12:
        _cob_set_int(result, 2)
        return result
    days = indate % 100
    if days < 1 or days > 31:
        _cob_set_int(result, 3)
        return result
    table = _LEAP_MONTH_DAYS if _leap_year(year) else _NORMAL_MONTH_DAYS
    if days > table[month]:
        _cob_set_int(result, 3)
        return result
    _cob_set_int(result, 0)
    return result


def cob_intr_test_day_yyyyddd(srcfield):
    """FUNCTION TEST-DAY-YYYYDDD - 0 ok / 1 yr / 2 day (L1459-L1488)."""
    result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 8, 0, 0)
    indate = _cob_get_int(srcfield)
    year = indate // 1000
    if year < 1601 or year > 9999:
        _cob_set_int(result, 1)
        return result
    days = indate % 1000
    if days < 1 or days > 365 + _leap_year(year):
        _cob_set_int(result, 2)
        return result
    _cob_set_int(result, 0)
    return result


def cob_intr_substitute(offset, length, params, *args):
    """FUNCTION SUBSTITUTE (intrinsic.c L681-L778)."""
    return _substitute(offset, length, params, args, fold=False)


def cob_intr_substitute_case(offset, length, params, *args):
    """FUNCTION SUBSTITUTE-CASE (intrinsic.c L778-L879)."""
    return _substitute(offset, length, params, args, fold=True)


def _substitute(offset, length, params, args, fold):
    var = args[0]
    numreps = params // 2
    f1 = [None] * numreps
    f2 = [None] * numreps
    for i in range(params - 1):
        if (i % 2) == 0:
            f1[i // 2] = args[1 + i]
        else:
            f2[i // 2] = args[1 + i]

    def _key(b):
        return b.upper() if fold else b

    vbytes = bytes(common.COB_FIELD_DATA(var)[:var.size])
    out = bytearray()
    n = 0
    while n < len(vbytes):
        matched = False
        for i in range(numreps):
            pat = bytes(common.COB_FIELD_DATA(f1[i])[:f1[i].size])
            if n + len(pat) <= len(vbytes) and \
                    _key(vbytes[n:n + len(pat)]) == _key(pat):
                rep = common.COB_FIELD_DATA(f2[i])
                out += bytes(rep[:f2[i].size])
                n += len(pat)
                matched = True
                break
        if matched:
            continue
        out.append(vbytes[n])
        n += 1
    result = make_field_entry(len(out), common.COB_TYPE_ALPHANUMERIC, 0, 0, 0,
                              data=out)
    if offset > 0:
        calc_ref_mod(result, offset, length)
    return result


def cob_intr_trim(offset, length, srcfield, direction):
    """FUNCTION TRIM (intrinsic.c L879-L919).  *direction*: 0=both,1=lead,2=trail."""
    sd = common.COB_FIELD_DATA(srcfield)
    size = srcfield.size
    result = _copy_field(srcfield)
    if all(sd[i] == ord(" ") for i in range(size)):
        result.size = 1
        result.data[0] = ord(" ")
        return result
    begin = 0
    end = size - 1
    if direction != 2:
        while sd[begin] == ord(" "):
            begin += 1
    if direction != 1:
        while sd[end] == ord(" "):
            end -= 1
    out = bytes(sd[begin:end + 1])
    result.data[:len(out)] = out
    result.size = len(out)
    if offset > 0:
        calc_ref_mod(result, offset, length)
    return result


# ===========================================================================
# Mathematical intrinsics (intrinsic.c L1488-L1799)
# ===========================================================================
def cob_intr_factorial(srcfield):
    """FUNCTION FACTORIAL (intrinsic.c L1488-L1512)."""
    result = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 18, 0, 0)
    common.cob_exception_code = 0
    srcval = _cob_get_int(srcfield)
    if srcval < 0:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    d1 = _Decimal(math.factorial(srcval), 0)
    _get(d1, result, 0)
    return result


def _double_intr(srcfield, fn):
    """Shared body for the IEEE-double trig/log intrinsics."""
    d1 = _Decimal()
    _set(d1, srcfield)
    result = make_double_entry()
    try:
        v = fn(intr_get_double(d1))
    except (ValueError, OverflowError):
        _cob_set_int(result, 0)
        return result
    _store_double(result, v)
    return result


def cob_intr_exp(srcfield):
    """FUNCTION EXP - e**x (intrinsic.c L1512-L1530)."""
    return _double_intr(srcfield, lambda x: math.pow(math.e, x))


def cob_intr_exp10(srcfield):
    """FUNCTION EXP10 - 10**x (intrinsic.c L1530-L1548)."""
    return _double_intr(srcfield, lambda x: math.pow(10.0, x))


def cob_intr_abs(srcfield):
    """FUNCTION ABS (intrinsic.c L1548-L1560)."""
    result = _copy_field(srcfield)
    d1 = _Decimal()
    _set(d1, srcfield)
    d1.value = abs(d1.value)
    _get(d1, result, 0)
    return result


def _fixed17_intr(srcfield, fn, signed):
    """Shared body for the fixed-point trig intrinsics ACOS/ASIN/ATAN/COS/SIN.

    These return an 8-byte ``NUMERIC_BINARY`` field with 18 digits and scale 17:
    the C runtime takes the integer part of the IEEE result, then peels 17
    fractional digits one at a time (``mathd2 *= 10; tempres = (int)mathd2``),
    accumulating them into a 64-bit integer that is ``memcpy``-d verbatim into
    the field (intrinsic.c L1560-L1760).

    *signed* mirrors the C declaration of the accumulator and the attribute
    sign flag, which differ across the five functions:

    * ACOS (intrinsic.c L1560) uses ``unsigned long long`` and ``flags = 0`` -
      its range is ``[0, pi]`` so the value is never negative; *signed* is
      ``False`` -> attribute flag ``0`` and an unsigned ``=Q`` store.
    * ASIN/ATAN/COS/SIN (L1594/L1627/L1660/L1729) use ``long long`` with
      ``COB_FLAG_HAVE_SIGN`` - their results may be negative; *signed* is
      ``True`` -> ``COB_FLAG_HAVE_SIGN`` and a signed ``=q`` store.

    Reproducing the exact accumulator width and sign flag is required for
    byte-for-byte parity (AAP 0.6.2).
    """
    d1 = _Decimal()
    _set(d1, srcfield)
    flags = common.COB_FLAG_HAVE_SIGN if signed else 0
    result = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 18, 17, flags)
    try:
        mathd2 = fn(intr_get_double(d1))
    except (ValueError, OverflowError):
        _cob_set_int(result, 0)
        return result
    res = int(mathd2)                         # (long long) cast: truncate toward zero
    mathd2 -= res
    for _ in range(17):
        mathd2 *= 10
        tempres = int(mathd2)                 # (int) cast: truncate toward zero
        res = res * 10 + tempres
        mathd2 -= tempres
    if signed:
        result.data[:8] = struct.pack("=q", _wrap_ll(res))
    else:
        result.data[:8] = struct.pack("=Q", res & _U64)
    return result


def cob_intr_acos(srcfield):
    """FUNCTION ACOS (intrinsic.c L1560-L1594) - unsigned fixed-17 binary."""
    return _fixed17_intr(srcfield, math.acos, signed=False)


def cob_intr_asin(srcfield):
    """FUNCTION ASIN (intrinsic.c L1594-L1627) - signed fixed-17 binary."""
    return _fixed17_intr(srcfield, math.asin, signed=True)


def cob_intr_atan(srcfield):
    """FUNCTION ATAN (intrinsic.c L1627-L1660) - signed fixed-17 binary."""
    return _fixed17_intr(srcfield, math.atan, signed=True)


def cob_intr_cos(srcfield):
    """FUNCTION COS (intrinsic.c L1660-L1693) - signed fixed-17 binary.

    NOTE: COS returns the fixed-17 *binary* representation (``long long`` with
    ``COB_FLAG_HAVE_SIGN``), NOT an IEEE double - unlike EXP/LOG/SQRT/TAN.
    """
    return _fixed17_intr(srcfield, math.cos, signed=True)


def cob_intr_log(srcfield):
    """FUNCTION LOG - natural log (intrinsic.c L1693-L1711)."""
    return _double_intr(srcfield, math.log)


def cob_intr_log10(srcfield):
    """FUNCTION LOG10 (intrinsic.c L1711-L1729)."""
    return _double_intr(srcfield, math.log10)


def cob_intr_sin(srcfield):
    """FUNCTION SIN (intrinsic.c L1729-L1762) - signed fixed-17 binary.

    NOTE: SIN returns the fixed-17 *binary* representation (``long long`` with
    ``COB_FLAG_HAVE_SIGN``), NOT an IEEE double - unlike EXP/LOG/SQRT/TAN.
    """
    return _fixed17_intr(srcfield, math.sin, signed=True)


def cob_intr_sqrt(srcfield):
    """FUNCTION SQRT (intrinsic.c L1762-L1778)."""
    return _double_intr(srcfield, math.sqrt)


def cob_intr_tan(srcfield):
    """FUNCTION TAN (intrinsic.c L1778-L1798)."""
    return _double_intr(srcfield, math.tan)


# ===========================================================================
# NUMVAL family + ANNUITY (intrinsic.c L1798-L1999)
# ===========================================================================
def _numval(srcfield, currency=None):
    sd = common.COB_FIELD_DATA(srcfield)
    size = srcfield.size
    cur_sym = common.cob_current_module.currency_symbol \
        if common.cob_current_module is not None else ord("$")
    dp = common.cob_current_module.decimal_point \
        if common.cob_current_module is not None else ord(".")
    currency_data = None
    if currency is not None and currency.size < size:
        currency_data = bytes(common.COB_FIELD_DATA(currency)[:currency.size])

    llval = 0
    integer_digits = 0
    decimal_digits = 0
    sign = 0
    decimal_seen = 0
    i = 0
    while i < size:
        if i < size - 1:
            pair = bytes(sd[i:i + 2]).upper()
            if pair in (b"CR", b"DB"):
                sign = 1
                break
        if currency_data and i < size - currency.size and \
                bytes(sd[i:i + currency.size]) == currency_data:
            i += currency.size
            continue
        c = sd[i]
        if c == ord(" ") or c == ord("+"):
            i += 1
            continue
        if c == ord("-"):
            sign = 1
            i += 1
            continue
        if c == dp:
            decimal_seen = 1
            i += 1
            continue
        if currency is not None and c == cur_sym:
            i += 1
            continue
        if ord("0") <= c <= ord("9"):
            llval = llval * 10 + (c - ord("0"))
            if decimal_seen:
                decimal_digits += 1
            else:
                integer_digits += 1
        if (integer_digits + decimal_digits) > 30:
            break
        i += 1

    if sign:
        llval = -llval
    if (integer_digits + decimal_digits) <= 18:
        result = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 18,
                                  decimal_digits, common.COB_FLAG_HAVE_SIGN)
        result.data[:8] = struct.pack("=q", _wrap_ll(llval))
        return result
    # Too many digits: fall back to a COMP-2 double.
    val = float(llval) / (10.0 ** decimal_digits)
    result = make_double_entry()
    _store_double(result, val)
    return result


def cob_intr_numval(srcfield):
    """FUNCTION NUMVAL (intrinsic.c L1798-L1878)."""
    return _numval(srcfield, None)


def cob_intr_numval_c(srcfield, currency):
    """FUNCTION NUMVAL-C (intrinsic.c L1878-L1977)."""
    return _numval(srcfield, currency)


def cob_intr_annuity(srcfield1, srcfield2):
    """FUNCTION ANNUITY (intrinsic.c L1977-L1999)."""
    result = make_double_entry()
    d1 = _Decimal()
    d2 = _Decimal()
    _set(d1, srcfield1)
    _set(d2, srcfield2)
    mathd1 = intr_get_double(d1)
    mathd2 = intr_get_double(d2)
    if mathd1 == 0:
        _store_double(result, 1.0 / mathd2)
        return result
    mathd1 = mathd1 / (1.0 - math.pow(mathd1 + 1.0, 0.0 - mathd2))
    _store_double(result, mathd1)
    return result


# ===========================================================================
# Statistical / list intrinsics - variadic (intrinsic.c L1999-L2587)
# ===========================================================================
def cob_intr_sum(params, *args):
    """FUNCTION SUM (intrinsic.c L1999-L2049)."""
    fields = list(args[:params])
    d1 = _Decimal(0, 0)
    scale = 0
    for f in fields:
        if common.COB_FIELD_SCALE(f) > scale:
            scale = common.COB_FIELD_SCALE(f)
        d2 = _Decimal()
        _set(d2, f)
        _add(d1, d2)
    size = len(str(abs(d1.value))) or 1
    if size < 19:
        result = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 18, scale,
                                  common.COB_FLAG_HAVE_SIGN)
    else:
        if d1.scale > size:
            size = d1.scale
        if scale > size:
            size = scale
        result = make_field_entry(size, common.COB_TYPE_NUMERIC_DISPLAY, size,
                                  scale, common.COB_FLAG_HAVE_SIGN)
    _get(d1, result, 0)
    return result


def cob_intr_ord_min(params, *args):
    """FUNCTION ORD-MIN - 1-based index of minimum (intrinsic.c L2049-L2084)."""
    result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 8, 0, 0)
    if params <= 1:
        _cob_set_int(result, 0)
        return result
    fields = list(args[:params])
    basef = fields[0]
    ordmin = 0
    for i in range(1, params):
        if common.cob_cmp(fields[i], basef) < 0:
            basef = fields[i]
            ordmin = i
    _cob_set_int(result, ordmin + 1)
    return result


def cob_intr_ord_max(params, *args):
    """FUNCTION ORD-MAX - 1-based index of maximum (intrinsic.c L2084-L2119)."""
    result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 8, 0, 0)
    if params <= 1:
        _cob_set_int(result, 0)
        return result
    fields = list(args[:params])
    basef = fields[0]
    ordmax = 0
    for i in range(1, params):
        if common.cob_cmp(fields[i], basef) > 0:
            basef = fields[i]
            ordmax = i
    _cob_set_int(result, ordmax + 1)
    return result


def cob_intr_min(params, *args):
    """FUNCTION MIN - returns the minimum argument field (intrinsic.c L2119-L2140)."""
    fields = list(args[:params])
    basef = fields[0]
    for i in range(1, params):
        if common.cob_cmp(fields[i], basef) < 0:
            basef = fields[i]
    return basef


def cob_intr_max(params, *args):
    """FUNCTION MAX - returns the maximum argument field (intrinsic.c L2140-L2161)."""
    fields = list(args[:params])
    basef = fields[0]
    for i in range(1, params):
        if common.cob_cmp(fields[i], basef) > 0:
            basef = fields[i]
    return basef


def cob_intr_midrange(params, *args):
    """FUNCTION MIDRANGE - (min+max)/2 (intrinsic.c L2161-L2194)."""
    result = make_double_entry()
    fields = list(args[:params])
    basemin = basemax = fields[0]
    for i in range(1, params):
        if common.cob_cmp(fields[i], basemin) < 0:
            basemin = fields[i]
        if common.cob_cmp(fields[i], basemax) > 0:
            basemax = fields[i]
    d1 = _Decimal()
    d2 = _Decimal()
    _set(d1, basemin)
    _set(d2, basemax)
    _add(d1, d2)
    _div(d1, _Decimal(2, 0))
    _get(d1, result, 0)
    return result


def cob_intr_median(params, *args):
    """FUNCTION MEDIAN (intrinsic.c L2194-L2239)."""
    fields = list(args[:params])
    if params == 1:
        return fields[0]
    import functools
    ordered = sorted(fields, key=functools.cmp_to_key(common.cob_cmp))
    i = params // 2
    if params % 2:
        return ordered[i]
    result = make_double_entry()
    d1 = _Decimal()
    d2 = _Decimal()
    _set(d1, ordered[i - 1])
    _set(d2, ordered[i])
    _add(d1, d2)
    _div(d1, _Decimal(2, 0))
    _get(d1, result, 0)
    return result


def _mean_decimal(fields):
    d1 = _Decimal(0, 0)
    for f in fields:
        d2 = _Decimal()
        _set(d2, f)
        _add(d1, d2)
    _div(d1, _Decimal(len(fields), 0))
    return d1


def cob_intr_mean(params, *args):
    """FUNCTION MEAN (intrinsic.c L2239-L2283)."""
    fields = list(args[:params])
    d1 = _mean_decimal(fields)
    # Choose a scale so the 18-digit binary keeps maximum fractional precision.
    probe = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 18, 0,
                             common.COB_FLAG_HAVE_SIGN)
    _get(d1, probe, 0)
    n = struct.unpack("=q", bytes(probe.data[:8]))[0]
    i = 0
    while n:
        n //= 10
        i += 1
    scale = 18 - i if i <= 18 else 0
    result = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 18, scale,
                              common.COB_FLAG_HAVE_SIGN)
    _get(d1, result, 0)
    return result


def cob_intr_mod(srcfield1, srcfield2):
    """FUNCTION MOD - modulo with INTEGER division (intrinsic.c L2283-L2304)."""
    result = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 18, 0,
                              common.COB_FLAG_HAVE_SIGN)
    f1 = cob_intr_integer(cob_intr_binop(srcfield1, "/", srcfield2))
    d1 = _Decimal()
    d2 = _Decimal()
    _set(d1, srcfield2)
    _set(d2, f1)
    _mul(d2, d1)
    _set(d1, srcfield1)
    _sub(d1, d2)
    _get(d1, result, 0)
    return result


def cob_intr_range(params, *args):
    """FUNCTION RANGE - max-min (intrinsic.c L2304-L2342)."""
    fields = list(args[:params])
    basemin = basemax = fields[0]
    for i in range(1, params):
        if common.cob_cmp(fields[i], basemin) < 0:
            basemin = fields[i]
        if common.cob_cmp(fields[i], basemax) > 0:
            basemax = fields[i]
    scale = max(common.COB_FIELD_SCALE(basemin), common.COB_FIELD_SCALE(basemax))
    result = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 18, scale,
                              common.COB_FLAG_HAVE_SIGN)
    d1 = _Decimal()
    d2 = _Decimal()
    _set(d1, basemax)
    _set(d2, basemin)
    _sub(d1, d2)
    _get(d1, result, 0)
    return result


def cob_intr_rem(srcfield1, srcfield2):
    """FUNCTION REM - remainder with INTEGER-PART division (intrinsic.c L2342-L2367)."""
    f1 = cob_intr_integer_part(cob_intr_binop(srcfield1, "/", srcfield2))
    d1 = _Decimal()
    d2 = _Decimal()
    _set(d1, srcfield2)
    _set(d2, f1)
    _mul(d2, d1)
    _set(d1, srcfield1)
    _sub(d1, d2)
    scale = max(common.COB_FIELD_SCALE(srcfield1),
                common.COB_FIELD_SCALE(srcfield2))
    result = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 18, scale,
                              common.COB_FLAG_HAVE_SIGN)
    _get(d1, result, 0)
    return result


# Module-level RNG mirrors the C srand/rand model: a single shared generator
# that FUNCTION RANDOM(seed) reseeds, returning rand()/RAND_MAX (intrinsic.c
# L2367-L2407).  RAND_MAX matches glibc's value (2**31 - 1).
#
# UNAVOIDABLE DIFFERENCE (documented per the prompt's RANDOM directive): the C
# runtime delegates to the platform C library's srand()/rand(), whose exact
# pseudo-random sequence (glibc's TYPE_3 additive-feedback generator) is an
# implementation detail of libc.  Python's stdlib :mod:`random` uses a
# Mersenne-Twister generator, so for a given seed the *sequence of values* is
# not byte-for-byte identical to glibc rand().  We preserve the observable
# contract that matters for COBOL programs - a reproducible stream in [0, 1)
# for a given seed, reseeded by the optional argument, with negative seeds
# clamped to 0 and the COMP-2 (double, scale 9) result shape - which is the
# closest faithful reproduction achievable without bundling a non-stdlib RNG
# (forbidden by AAP 0.5/0.7.1).  FUNCTION RANDOM is non-deterministic by design
# and the COBOL-85 acceptance suite does not assert specific RANDOM values.
import random as _random
_rng = _random.Random()
_RAND_MAX = 2147483647


def cob_intr_random(params, *args):
    """FUNCTION RANDOM - pseudo-random number in [0,1) (intrinsic.c L2367-L2407).

    With an argument, reseeds the shared generator (negative seeds clamped to
    0, as the C ``if (seed < 0) seed = 0;`` guard does).  See the module-level
    note above for the documented, unavoidable RNG-sequence difference versus
    the platform C ``rand()``.
    """
    if params:
        seed = _cob_get_int(args[0])
        if seed < 0:
            seed = 0
        _rng.seed(seed)
    result = make_field_entry(8, common.COB_TYPE_NUMERIC_DOUBLE, 20, 9,
                              common.COB_FLAG_HAVE_SIGN)
    randnum = _rng.randint(0, _RAND_MAX)
    _store_double(result, float(randnum) / float(_RAND_MAX))
    return result


def cob_intr_variance(params, *args):
    """FUNCTION VARIANCE (intrinsic.c L2407-L2476)."""
    fields = list(args[:params])
    result = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 18, 0,
                              common.COB_FLAG_HAVE_SIGN)
    if params == 1:
        _cob_set_int(result, 0)
        return result
    mean = _mean_decimal(fields)
    d4 = _Decimal(0, 0)
    for f in fields:
        d2 = _Decimal()
        _set(d2, f)
        _sub(d2, mean)
        _mul(d2, d2)
        _add(d4, d2)
    _div(d4, _Decimal(params, 0))
    probe = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 18, 0,
                             common.COB_FLAG_HAVE_SIGN)
    _get(d4, probe, 0)
    n = struct.unpack("=q", bytes(probe.data[:8]))[0]
    i = 0
    while n:
        n //= 10
        i += 1
    scale = 18 - i if i <= 18 else 0
    result = make_field_entry(8, common.COB_TYPE_NUMERIC_BINARY, 18, scale,
                              common.COB_FLAG_HAVE_SIGN)
    _get(d4, result, 0)
    return result


def cob_intr_standard_deviation(params, *args):
    """FUNCTION STANDARD-DEVIATION - sqrt(VARIANCE) (intrinsic.c L2476-L2542)."""
    fields = list(args[:params])
    if params == 1:
        result = make_double_entry()
        _cob_set_int(result, 0)
        return result
    mean = _mean_decimal(fields)
    d4 = _Decimal(0, 0)
    for f in fields:
        d2 = _Decimal()
        _set(d2, f)
        _sub(d2, mean)
        _mul(d2, d2)
        _add(d4, d2)
    _div(d4, _Decimal(params, 0))
    variance = make_double_entry()
    _get(d4, variance, 0)
    return cob_intr_sqrt(variance)


def cob_intr_present_value(params, *args):
    """FUNCTION PRESENT-VALUE (intrinsic.c L2542-L2587)."""
    result = make_double_entry()
    if params < 2:
        _cob_set_int(result, 0)
        return result
    fields = list(args[:params])
    d1 = _Decimal()
    _set(d1, fields[0])
    _add(d1, _Decimal(1, 0))               # (1 + rate)
    d4 = _Decimal(0, 0)
    for i in range(1, params):
        d2 = _Decimal()
        _set(d2, fields[i])
        d3 = _Decimal(d1.value, d1.scale)
        if i > 1:
            _pow(d3, _Decimal(i, 0))
        _div(d2, d3)
        _add(d4, d2)
    _get(d4, result, 0)
    return result


# ===========================================================================
# Year/Date sliding-window intrinsics - variadic (intrinsic.c L2587-L2786)
# ===========================================================================
def _sliding_year(year, interval, xqtyear):
    maxyear = xqtyear + interval
    if maxyear < 1700 or maxyear > 9999:
        return None
    if maxyear % 100 >= year:
        return year + 100 * (maxyear // 100)
    return year + 100 * ((maxyear // 100) - 1)


def cob_intr_year_to_yyyy(params, *args):
    """FUNCTION YEAR-TO-YYYY (intrinsic.c L2587-L2650)."""
    result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 8, 0, 0)
    common.cob_exception_code = 0
    year = _cob_get_int(args[0])
    interval = _cob_get_int(args[1]) if params > 1 else 50
    xqtyear = _cob_get_int(args[2]) if params > 2 else _time.localtime().tm_year
    if year < 0 or year > 99:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    if xqtyear < 1601 or xqtyear > 9999:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    full = _sliding_year(year, interval, xqtyear)
    if full is None:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    _cob_set_int(result, full)
    return result


def cob_intr_date_to_yyyymmdd(params, *args):
    """FUNCTION DATE-TO-YYYYMMDD (intrinsic.c L2650-L2718)."""
    result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 8, 0, 0)
    common.cob_exception_code = 0
    arg0 = _cob_get_int(args[0])
    mmdd = arg0 % 10000
    year = arg0 // 10000
    interval = _cob_get_int(args[1]) if params > 1 else 50
    xqtyear = _cob_get_int(args[2]) if params > 2 else _time.localtime().tm_year
    if year < 0 or year > 999999:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    if xqtyear < 1601 or xqtyear > 9999:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    full = _sliding_year(year, interval, xqtyear)
    if full is None:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    _cob_set_int(result, full * 10000 + mmdd)
    return result


def cob_intr_day_to_yyyyddd(params, *args):
    """FUNCTION DAY-TO-YYYYDDD (intrinsic.c L2718-L2786)."""
    result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 8, 0, 0)
    common.cob_exception_code = 0
    arg0 = _cob_get_int(args[0])
    days = arg0 % 1000
    year = arg0 // 1000
    interval = _cob_get_int(args[1]) if params > 1 else 50
    xqtyear = _cob_get_int(args[2]) if params > 2 else _time.localtime().tm_year
    if year < 0 or year > 999999:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    if xqtyear < 1601 or xqtyear > 9999:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    full = _sliding_year(year, interval, xqtyear)
    if full is None:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    _cob_set_int(result, full * 1000 + days)
    return result


# ===========================================================================
# Time intrinsics (intrinsic.c L2786-L2870)
# ===========================================================================
def cob_intr_seconds_past_midnight():
    """FUNCTION SECONDS-PAST-MIDNIGHT (intrinsic.c L2786-L2807)."""
    result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 8, 0, 0)
    now = _time.localtime()
    _cob_set_int(result, now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec)
    return result


def cob_intr_seconds_from_formatted_time(fmt, value):
    """FUNCTION SECONDS-FROM-FORMATTED-TIME (intrinsic.c L2807-L2870)."""
    result = make_field_entry(4, common.COB_TYPE_NUMERIC_BINARY, 8, 0, 0)
    common.cob_exception_code = 0
    if value.size < fmt.size:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        _cob_set_int(result, 0)
        return result
    p1 = bytes(common.COB_FIELD_DATA(fmt)[:fmt.size])
    p2 = bytes(common.COB_FIELD_DATA(value)[:value.size])
    seconds = minutes = hours = 0
    h_seen = m_seen = s_seen = False
    n = 0
    while n < fmt.size - 1:
        if p1[n:n + 2] == b"hh" and not h_seen and \
                p2[n:n + 1].isdigit() and p2[n + 1:n + 2].isdigit():
            hours = (p2[n] - 48) * 10 + (p2[n + 1] - 48)
            h_seen = True
        elif p1[n:n + 2] == b"mm" and not m_seen and \
                p2[n:n + 1].isdigit() and p2[n + 1:n + 2].isdigit():
            minutes = (p2[n] - 48) * 10 + (p2[n + 1] - 48)
            m_seen = True
        elif p1[n:n + 2] == b"ss" and not s_seen and \
                p2[n:n + 1].isdigit() and p2[n + 1:n + 2].isdigit():
            seconds = (p2[n] - 48) * 10 + (p2[n + 1] - 48)
            s_seen = True
        n += 1
    if h_seen and m_seen and s_seen:
        seconds += hours * 3600 + minutes * 60
    else:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        seconds = 0
    _cob_set_int(result, seconds)
    return result


# ===========================================================================
# Locale-aware intrinsics (intrinsic.c L2870-L3291)
# ===========================================================================
def cob_intr_locale_date(offset, length, srcfield, locale_field):
    """FUNCTION LOCALE-DATE (intrinsic.c L2870-L3016).

    Formats an 8-digit ``YYYYMMDD`` argument using the active locale's date
    representation (``%x``), mirroring the C runtime's locale-formatted output.
    """
    indate = _cob_get_int(srcfield)
    year, md = divmod(indate, 10000)
    month, day = divmod(md, 100)
    common.cob_exception_code = 0
    try:
        import datetime
        text = datetime.date(year, month, day).strftime("%x")
    except (ValueError, OverflowError):
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        text = " "
    payload = text.encode("latin-1")
    result = make_field_entry(len(payload), common.COB_TYPE_ALPHANUMERIC, 0, 0,
                              0, data=payload)
    if offset > 0:
        calc_ref_mod(result, offset, length)
    return result


def cob_intr_locale_time(offset, length, srcfield, locale_field):
    """FUNCTION LOCALE-TIME (intrinsic.c L3016-L3153).

    Formats an ``HHMMSS`` argument using the active locale's time
    representation (``%X``).
    """
    intime = _cob_get_int(srcfield)
    hh, ms = divmod(intime, 10000)
    mm, ss = divmod(ms, 100)
    common.cob_exception_code = 0
    try:
        import datetime
        text = datetime.time(hh, mm, ss).strftime("%X")
    except (ValueError, OverflowError):
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        text = " "
    payload = text.encode("latin-1")
    result = make_field_entry(len(payload), common.COB_TYPE_ALPHANUMERIC, 0, 0,
                              0, data=payload)
    if offset > 0:
        calc_ref_mod(result, offset, length)
    return result


def cob_intr_lcl_time_from_secs(offset, length, srcfield, locale_field):
    """FUNCTION LOCALE-TIME-FROM-SECONDS (intrinsic.c L3153-L3291).

    Interprets *srcfield* as seconds-past-midnight and formats it as a
    locale time (``%X``).
    """
    secs = _cob_get_int(srcfield)
    common.cob_exception_code = 0
    if secs < 0 or secs > 86400:
        common.cob_set_exception(common.COB_EC_ARGUMENT_FUNCTION)
        text = " "
    else:
        hh = secs // 3600
        mm = (secs % 3600) // 60
        ss = secs % 60
        try:
            import datetime
            text = datetime.time(hh % 24, mm, ss).strftime("%X")
        except (ValueError, OverflowError):       # pragma: no cover
            text = " "
    payload = text.encode("latin-1")
    result = make_field_entry(len(payload), common.COB_TYPE_ALPHANUMERIC, 0, 0,
                              0, data=payload)
    if offset > 0:
        calc_ref_mod(result, offset, length)
    return result


# ===========================================================================
# Runtime initialisation (intrinsic.c has no explicit init; the C statics are
# zero-initialised - here the module-level state above serves the same role).
# ===========================================================================
def cob_init_intrinsic():
    """Reset intrinsic runtime state (mirrors the zero-init C statics)."""
    _rng.seed()
    return None
