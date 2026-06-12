"""libcob_py.numeric - COBOL numeric arithmetic and byte-for-byte USAGE storage.

Pure-Python port of the C runtime module ``libcob/numeric.c`` (1777 lines) and
the byte-level inline helpers declared in ``libcob/codegen.h`` (the
``cob_get_numdisp`` / ``cob_get_packed_int`` / ``cob_cmp_packed_int`` /
``cob_add_packed_int`` and the ``cob_cmp_{u,s}NN_binary`` families).  It is the
runtime that the rewritten code emitter (``cobc/codegen.c``) targets for every
arithmetic and numeric-conversion call-site of a compiled COBOL program.

decimal replaces GMP
--------------------
The C runtime performs all value arithmetic with the GNU Multiple Precision
library (GMP ``mpz``) behind a ``cob_decimal`` (an arbitrary-precision integer
*value* plus a decimal *scale*).  This module reproduces that model exactly with
Python's built-in arbitrary-precision ``int`` for the unscaled mantissa - the
faithful, minimal-deviation replacement for ``mpz`` (AAP sections 0.3.3 /
0.7.2) - and uses the standard-library :mod:`decimal` module (``decimal.Decimal``
under a per-operation ``decimal.Context``) for the COBOL ``ROUNDED MODE`` map,
for floating ``COMP-1`` / ``COMP-2`` conversions, and for non-integer
exponentiation.  Python's ``decimal`` precision far exceeds COBOL's 18-31 digit
range, so it fully supersedes GMP and **no** third-party big-number package is
required.

byte-for-byte parity
--------------------
This is the highest-risk runtime module: emitted programs must produce output
*byte-for-byte* identical to the original C toolchain for every
``PICTURE`` / ``USAGE`` / ``COMP`` combination (AAP sections 0.6.2 / 0.7.1).
Two concerns are therefore kept rigorously separate:

* **Value arithmetic** - the ``cob_decimal`` (int mantissa + scale) algebra,
  which mirrors GMP semantics including truncate-toward-zero shifting and the
  37-guard-digit division used by ``cob_decimal_div``.
* **Storage layout** - the exact stored byte pattern for each ``USAGE``
  (zoned DISPLAY, big/little-endian BINARY of widths 1-8 bytes, packed
  BCD COMP-3, COMP-5 native, COMP-X unsigned), matching the immutable field
  layout produced by the front-end ``cobc/field.c``.

Standard library only
---------------------
The only imports are :mod:`decimal`, :mod:`struct`, :mod:`sys` and the sibling
runtime base module :mod:`libcob_py.common`.  ZERO third-party dependencies
(AAP sections 0.5 / 0.7.1).
"""

import contextlib
import decimal
import struct
import sys

from libcob_py import common

# ---------------------------------------------------------------------------
# Constants (numeric.c L40-L48)
# ---------------------------------------------------------------------------

#: Sentinel scale value flagging a "not a number" decimal - reproduces the C
#: ``DECIMAL_NAN`` (-128) marker set on division by zero / size overflow so the
#: condition propagates through subsequent operations (numeric.c L41).
DECIMAL_NAN = -128

#: Upper bound of the precomputed power-of-ten table in the C runtime
#: (``COB_MAX_BINARY`` - numeric.c L48); used for the binary digit-count
#: overflow check in :func:`cob_decimal_get_binary`.
COB_MAX_BINARY = 36

#: Packed-BCD byte values for 00..99 - direct port of the C ``packed_bytes``
#: table (numeric.c L54-L65).  ``packed_bytes[n]`` is the BCD encoding of the
#: two-digit number ``n`` (e.g. ``packed_bytes[45] == 0x45``).
packed_bytes = bytes(((i // 10) << 4) | (i % 10) for i in range(100))

# ---------------------------------------------------------------------------
# COBOL ROUNDED MODE -> decimal rounding-constant map (EXACT - AAP section 0.6.2)
#
# The legacy C runtime implements only HALF_UP (its ``COB_STORE_ROUND`` add-5
# path) and truncation (no ROUNDED -> truncate toward zero).  decimal.Decimal's
# ``ROUND_HALF_UP`` is byte-for-byte identical to the C add-5-then-truncate
# algorithm, and ``ROUND_DOWN`` is identical to the C truncate-toward-zero
# store.  The remaining five modes implement the COBOL-2002 ``ROUNDED MODE``
# clause per the prompt-authoritative mapping (the prompt specifies them where
# the C source is silent).
# ---------------------------------------------------------------------------
ROUND_MODE_MAP = {
    "NEAREST-AWAY-FROM-ZERO": decimal.ROUND_HALF_UP,
    "NEAREST-EVEN": decimal.ROUND_HALF_EVEN,
    "NEAREST-TOWARD-ZERO": decimal.ROUND_HALF_DOWN,
    "TOWARD-GREATER": decimal.ROUND_CEILING,
    "TOWARD-LESSER": decimal.ROUND_FLOOR,
    "TRUNCATION": decimal.ROUND_DOWN,
    "AWAY-FROM-ZERO": decimal.ROUND_UP,
}

#: Default rounding when the COBOL ``ROUNDED`` clause is present but names no
#: mode (COBOL default == NEAREST-AWAY-FROM-ZERO == HALF_UP).
COB_ROUND_DEFAULT = decimal.ROUND_HALF_UP

#: Rounding applied when there is no ``ROUNDED`` clause at all: truncation,
#: i.e. truncate toward zero (matches the C store path's ``shift_decimal``).
COB_ROUND_TRUNCATION = decimal.ROUND_DOWN

# ---------------------------------------------------------------------------
# decimal Overflow / Inexact traps -> EC-SIZE family + ON SIZE ERROR
# (AAP section 0.6.2).
#
# AAP 0.6.2 directs that overflow handling "enables the decimal Overflow and
# Inexact traps within the per-statement context and maps them to the EC-SIZE
# family and the COBOL ON SIZE ERROR path."  Two facts from COBOL semantics
# determine how this is realised faithfully under the byte-for-byte mandate
# (AAP 0.7.1):
#
#   * COBOL SIZE ERROR is raised when the *result does not fit the receiving
#     item's PICTURE* - i.e. its integer-digit capacity is exceeded - and NOT
#     when a fractional digit is rounded away.  ``decimal.Inexact`` (and
#     ``decimal.Rounded``) fire on EVERY rounding store, e.g. any ``ROUNDED``
#     clause, so literally promoting Inexact to an exception would raise SIZE
#     ERROR on perfectly legal COBOL statements and destroy parity.  Inexact /
#     Rounded are therefore left OBSERVABLE-ONLY (trap disabled); the
#     receiving-field digit-capacity check in
#     :func:`cob_decimal_get_display` / :func:`cob_decimal_get_binary` /
#     :func:`cob_decimal_get_packed` is the authoritative, byte-equivalent
#     EC-SIZE-OVERFLOW detector that drives ON SIZE ERROR.
#   * ``decimal.Overflow`` (adjusted exponent > Emax) and
#     ``decimal.DivisionByZero`` are genuine error conditions with no legal
#     COBOL counterpart at COBOL magnitudes (18-31 digits); they are ENABLED as
#     traps so any such signal is surfaced and mapped to the EC-SIZE family
#     rather than silently producing a wrong value.
#
# :data:`COB_SIZE_ERROR_TRAPS` is the explicit, visible trap set and
# :func:`cob_size_error_context` is the per-statement context that installs
# precision + ROUNDED MODE + traps and maps a raised signal to the COBOL
# EC-SIZE exception (and hence ON SIZE ERROR).
# ---------------------------------------------------------------------------

#: decimal signals promoted to a COBOL EC-SIZE exception inside a statement
#: context: Overflow -> EC-SIZE-OVERFLOW, DivisionByZero -> EC-SIZE-ZERO-DIVIDE.
COB_SIZE_ERROR_TRAPS = (decimal.Overflow, decimal.DivisionByZero)

#: Working precision for the per-statement context: well above COBOL's maximum
#: 31 (some dialects 38) significant digits but far below decimal's default
#: Emax, so legal COBOL magnitudes never spuriously raise ``decimal.Overflow``.
COB_STATEMENT_PRECISION = 80


def _map_decimal_signal_to_ec_size(signal_exc):
    """Map a trapped :mod:`decimal` signal to the COBOL EC-SIZE exception.

    ``DivisionByZero`` -> ``EC-SIZE-ZERO-DIVIDE``; every other trapped signal
    (currently ``Overflow``) -> ``EC-SIZE-OVERFLOW``.  Returns the resulting
    ``cob_exception_code`` so callers can short-circuit the store.
    """
    if isinstance(signal_exc, decimal.DivisionByZero):
        common.cob_set_exception(common.COB_EC_SIZE_ZERO_DIVIDE)
    else:
        common.cob_set_exception(common.COB_EC_SIZE_OVERFLOW)
    return common.cob_exception_code


@contextlib.contextmanager
def cob_size_error_context(opt, rounding=None):
    """Per-statement :mod:`decimal` context implementing AAP 0.6.2 traps.

    Installs a :func:`decimal.localcontext` carrying COBOL precision and the
    statement's ROUNDED MODE (HALF_UP when ``COB_STORE_ROUND`` is set and no
    explicit mode is supplied, truncation otherwise), ENABLES the
    Overflow/DivisionByZero traps listed in :data:`COB_SIZE_ERROR_TRAPS`, and
    keeps Inexact/Rounded observable-only (see the module note above).  A
    trapped signal is caught and mapped to the COBOL EC-SIZE exception, so the
    receiving store is skipped exactly as the COBOL ``ON SIZE ERROR`` path
    requires.
    """
    with decimal.localcontext() as ctx:
        ctx.prec = COB_STATEMENT_PRECISION
        if opt & common.COB_STORE_ROUND:
            ctx.rounding = rounding if rounding is not None else COB_ROUND_DEFAULT
        else:
            ctx.rounding = COB_ROUND_TRUNCATION
        # Enable the EC-SIZE-mapped traps; Inexact/Rounded stay observable-only
        # because COBOL SIZE ERROR is integer-digit overflow, not fractional
        # rounding (see the module note above).
        for sig in COB_SIZE_ERROR_TRAPS:
            ctx.traps[sig] = True
        ctx.traps[decimal.Inexact] = False
        ctx.traps[decimal.Rounded] = False
        try:
            yield ctx
        except COB_SIZE_ERROR_TRAPS as exc:
            _map_decimal_signal_to_ec_size(exc)


# ---------------------------------------------------------------------------
# Power-of-ten cache.  The C runtime precomputes ``cob_mpze10[0..35]``; Python
# ``int`` is arbitrary precision so we cache lazily and grow on demand.
# ---------------------------------------------------------------------------
_POW10_CACHE = {0: 1}


def _p10(n):
    """Return ``10 ** n`` for ``n >= 0`` with memoisation (cob_mpze10 analogue)."""
    value = _POW10_CACHE.get(n)
    if value is None:
        value = 10 ** n
        _POW10_CACHE[n] = value
    return value


def _tdiv_q(a, b):
    """Truncate-toward-zero integer quotient (GMP ``mpz_tdiv_q`` semantics).

    Python's ``//`` floors toward negative infinity, whereas GMP - and C
    integer division - truncate toward zero.  Reproducing the exact rounding
    direction is essential for byte-for-byte parity of COBOL ``DIVIDE`` and of
    the internal ``shift_decimal`` down-shifts.
    """
    if b == 0:
        raise ZeroDivisionError("cob_decimal truncate division by zero")
    quotient = abs(a) // abs(b)
    if (a < 0) != (b < 0):
        return -quotient
    return quotient


def _tdiv_r(a, b):
    """Truncate-toward-zero remainder (GMP ``mpz_tdiv_r`` semantics).

    The remainder takes the sign of the dividend *a* (``b`` is a positive
    power of ten in every call site).  Used to keep the low-order ``digits``
    decimal digits of a value on binary store truncation.
    """
    remainder = abs(a) % abs(b)
    return -remainder if a < 0 else remainder


# ===========================================================================
# cob_decimal - the arbitrary-precision COBOL decimal (numeric.c "Decimal
# number" section).  Mirrors the C ``cob_decimal`` struct: an integer *value*
# (the unscaled mantissa, a Python ``int`` replacing the GMP ``mpz_t``) and an
# integer *scale* (the number of fractional digits; ``value * 10**-scale`` is
# the represented quantity).  ``scale == DECIMAL_NAN`` flags an invalid result.
# ===========================================================================

class cob_decimal(object):
    """Arbitrary-precision decimal: integer ``value`` and integer ``scale``.

    The represented quantity is ``value * 10 ** (-scale)``.  This is the direct
    analogue of the C ``cob_decimal`` (GMP ``mpz_t value`` + ``int scale``),
    with Python's native bignum ``int`` standing in for ``mpz_t``.
    """

    __slots__ = ("value", "scale")

    def __init__(self, value=0, scale=0):
        self.value = int(value)
        self.scale = int(scale)

    def __repr__(self):  # pragma: no cover - debugging aid only
        return "cob_decimal(value=%d, scale=%d)" % (self.value, self.scale)


# Scratch decimals reused across statement-level helpers, reproducing the C
# file-scope statics ``cob_d1``..``cob_d4`` (numeric.c L68-L71).  ``cob_d3`` in
# particular must persist between :func:`cob_div_quotient` and
# :func:`cob_div_remainder`, exactly as the C statics do.
cob_d1 = cob_decimal()
cob_d2 = cob_decimal()
cob_d3 = cob_decimal()
cob_d4 = cob_decimal()


def cob_decimal_init(d):
    """Initialise *d* to zero with scale 0 (numeric.c L283-L288).

    The C version allocates GMP storage; here it simply resets the Python
    fields, which is all the int-based model requires.
    """
    d.value = 0
    d.scale = 0


def cob_decimal_set(dst, src):
    """Copy decimal *src* into *dst* (numeric.c L333-L338)."""
    dst.value = src.value
    dst.scale = src.scale


def cob_init_numeric():
    """Module initialiser - the ``cob_init_numeric`` entry point (numeric.c L1463).

    Invoked by :func:`libcob_py.common.cob_init` in the canonical subsystem
    order (``numeric`` is first).  Resets the scratch decimals, primes the
    power-of-ten cache (the C ``cob_mpze10`` table) and installs a generous
    default :mod:`decimal` context so that intermediate quantise operations
    never lose precision for COBOL's 18-31 digit range.
    """
    cob_decimal_init(cob_d1)
    cob_decimal_init(cob_d2)
    cob_decimal_init(cob_d3)
    cob_decimal_init(cob_d4)
    for i in range(COB_MAX_BINARY):
        _p10(i)
    # Reset the cob_cmp_packed scratch image (C ``memset(packed_value, 0, ...)``).
    global _cmp_packed_lastval
    _cmp_packed_lastval = 0
    for k in range(len(_cmp_packed_value)):
        _cmp_packed_value[k] = 0
    # A high working precision keeps decimal.quantize exact for every COBOL
    # picture (max 31 digits) plus the division guard digits.
    ctx = decimal.getcontext()
    if ctx.prec < 64:
        ctx.prec = 64


# ===========================================================================
# BINARY integer load/store (numeric.c L171-L281).
#
# COBOL ``USAGE BINARY`` / ``COMP`` / ``COMP-4`` items are stored big-endian by
# convention, ``COMP-5`` / ``COMP-X`` use native byte order.  ``field.c`` records
# the chosen order in the ``COB_FLAG_BINARY_SWAP`` bit: when set, the stored
# bytes are big-endian; when clear, they are the host's native order.  On the
# little-endian build host this exactly reproduces the C ``#ifndef
# WORDS_BIGENDIAN`` branch (swap == big-endian storage, no-swap == native).
# ===========================================================================

def _order(f):
    """Return the stored byte order for binary field *f* (``'big'``/``'little'``).

    ``COB_FLAG_BINARY_SWAP`` set => big-endian storage; otherwise the host's
    native order (``sys.byteorder``).  See the module/​section docstring.
    """
    return "big" if common.COB_FIELD_BINARY_SWAP(f) else sys.byteorder


def cob_binary_get_int64(f):
    """Load a signed binary field as a Python int (numeric.c L171-L222).

    Reads ``f.size`` bytes (1-8) in the field's stored order with sign
    extension - the faithful equivalent of the C byte-copy + ``COB_BSWAP`` +
    arithmetic-shift dance.
    """
    return int.from_bytes(bytes(f.data[:f.size]), _order(f), signed=True)


def cob_binary_get_uint64(f):
    """Load an unsigned binary field as a Python int (numeric.c L224-L241)."""
    return int.from_bytes(bytes(f.data[:f.size]), _order(f), signed=False)


def _binary_store(f, n):
    """Store integer *n* into the low ``f.size`` bytes of *f* in stored order.

    Reproduces the C ``cob_binary_set_*`` behaviour of copying the low
    ``f->size`` bytes of the (optionally byte-swapped) machine word: the value
    is reduced modulo ``2**(8*size)`` (two's-complement low bytes) and written
    in the field's stored order.
    """
    nbytes = f.size
    mask = (1 << (nbytes * 8)) - 1
    val = n & mask
    f.data[:nbytes] = bytearray(val.to_bytes(nbytes, _order(f)))


def cob_binary_set_int64(f, n):
    """Store signed integer *n* into binary field *f* (numeric.c L261-L281)."""
    _binary_store(f, n)


def cob_binary_set_uint64(f, n):
    """Store unsigned integer *n* into binary field *f* (numeric.c L243-L259)."""
    _binary_store(f, n)


# ===========================================================================
# Floating conversions (numeric.c L342-L366) - COMP-1 / COMP-2 support.
# ===========================================================================

def cob_decimal_set_double(d, v):
    """Set *d* from a C ``double`` *v* (numeric.c L342-L347).

    Mirrors ``mpz_set_d (d->value, v * 1.0e9); d->scale = 9`` - the mantissa is
    the truncated-toward-zero integer ``v * 1e9`` at a fixed scale of 9.
    """
    d.value = int(v * 1.0e9)
    d.scale = 9


def cob_decimal_set_int(d, n):
    """Set decimal *d* from a signed integer *n* with scale 0.

    This is the runtime home of the helper that the C backend used to *emit* as
    a static function inside every generated translation unit::

        static void
        cob_decimal_set_int (cob_decimal *d, const int n)
        {
            mpz_set_si (d->value, n);
            d->scale = 0;
        }

    (see ``cobc/codegen.c`` ``gen_decset`` emission in the original C emitter).
    The Python emitter no longer emits this body; instead the front-end
    (``cobc/typeck.c`` L2046/L2057/L2076) emits a call
    ``numeric.cob_decimal_set_int(d, n)`` and the implementation lives here.
    ``mpz_set_si`` stores the signed value verbatim, so the Python bignum
    assignment is byte-for-byte equivalent.
    """
    d.value = int(n)
    d.scale = 0


def cob_decimal_set_uint(d, n):
    """Set decimal *d* from an unsigned integer *n* with scale 0.

    Runtime home of the C backend's emitted static helper::

        static void
        cob_decimal_set_uint (cob_decimal *d, const unsigned int n)
        {
            mpz_set_ui (d->value, n);
            d->scale = 0;
        }

    The front-end (``cobc/typeck.c`` L2079) emits this only for *unsigned*
    PICTURE operands, where ``cob_get_int`` yields a non-negative magnitude, so
    the value is already in ``[0, 2**31)``.  The C parameter is ``unsigned
    int``; to mirror that 32-bit unsigned conversion exactly (including the
    theoretical negative-wrap edge), the value is masked to 32 bits before being
    stored.  For every value the front-end actually produces this mask is a
    no-op, preserving byte-for-byte parity with ``mpz_set_ui``.
    """
    d.value = int(n) & 0xFFFFFFFF
    d.scale = 0


def cob_decimal_get_double(d):
    """Return *d* as a Python ``float`` (numeric.c L349-L366).

    Reproduces the C loop (repeated divide/multiply by 10) rather than a single
    ``/ 10**scale`` so the floating-point rounding path matches exactly.
    """
    v = float(d.value)
    n = d.scale
    while n > 0:
        v /= 10
        n -= 1
    while n < 0:
        v *= 10
        n += 1
    return v


# ===========================================================================
# Scale shifting / alignment (numeric.c L303-L331).
# ===========================================================================

def shift_decimal(d, n):
    """Multiply *d* by ``10**n`` and adjust its scale (numeric.c L303-L317).

    Positive *n* scales the mantissa up (exact); negative *n* scales it down
    with truncation toward zero, exactly like the C ``mpz_tdiv_q`` down-shift.
    """
    if n == 0:
        return
    if n > 0:
        d.value *= _p10(n)
    else:
        d.value = _tdiv_q(d.value, _p10(-n))
    d.scale += n


def align_decimal(d1, d2):
    """Bring *d1* and *d2* to a common scale (numeric.c L319-L327).

    Always scales the *smaller*-scale operand *up*, so no precision is lost.
    """
    if d1.scale < d2.scale:
        shift_decimal(d1, d2.scale - d1.scale)
    elif d1.scale > d2.scale:
        shift_decimal(d2, d1.scale - d2.scale)


def _decimal_check(d1, d2):
    """Port of the C ``DECIMAL_CHECK`` macro (numeric.c L42-L46).

    If either operand is flagged NaN, mark *d1* NaN and report that the caller
    must short-circuit (the C macro ``return``\\s from the void operation).
    """
    if d1.scale == DECIMAL_NAN or d2.scale == DECIMAL_NAN:
        d1.scale = DECIMAL_NAN
        return True
    return False


# ===========================================================================
# Decimal arithmetic (numeric.c L1041-L1110).
# ===========================================================================

def cob_decimal_add(d1, d2):
    """``d1 += d2`` (numeric.c L1041-L1047)."""
    if _decimal_check(d1, d2):
        return
    align_decimal(d1, d2)
    d1.value += d2.value


def cob_decimal_sub(d1, d2):
    """``d1 -= d2`` (numeric.c L1049-L1055)."""
    if _decimal_check(d1, d2):
        return
    align_decimal(d1, d2)
    d1.value -= d2.value


def cob_decimal_mul(d1, d2):
    """``d1 *= d2`` (numeric.c L1057-L1063); scales add, mantissas multiply."""
    if _decimal_check(d1, d2):
        return
    d1.scale += d2.scale
    d1.value *= d2.value


def cob_decimal_div(d1, d2):
    """``d1 /= d2`` (numeric.c L1065-L1083).

    Division by zero flags *d1* NaN and raises ``EC-SIZE-ZERO-DIVIDE``.  A zero
    dividend yields exact zero.  Otherwise the C algorithm is reproduced
    faithfully: subtract scales, shift up by 37 guard digits (plus the negative
    scale), then truncating integer division.  The 37-digit guard is what makes
    COBOL ``DIVIDE`` results byte-for-byte reproducible.
    """
    if _decimal_check(d1, d2):
        return
    if d2.value == 0:
        d1.scale = DECIMAL_NAN
        common.cob_set_exception(common.COB_EC_SIZE_ZERO_DIVIDE)
        return
    if d1.value == 0:
        d1.scale = 0
        return
    d1.scale -= d2.scale
    shift_decimal(d1, 37 + (-d1.scale if d1.scale < 0 else 0))
    d1.value = _tdiv_q(d1.value, d2.value)


def cob_decimal_pow(d1, d2):
    """``d1 = d1 ** d2`` (numeric.c L1085-L1100).

    Integer non-negative exponents use exact bignum exponentiation (mantissa
    raised, scale multiplied); anything else falls back to the ``double`` path,
    matching the C ``mpz_fits_ulong_p`` branch.
    """
    if _decimal_check(d1, d2):
        return
    if d2.scale == 0 and 0 <= d2.value <= 0xFFFFFFFFFFFFFFFF:
        n = d2.value
        d1.value = d1.value ** n
        d1.scale *= n
    else:
        cob_decimal_set_double(
            d1, pow(cob_decimal_get_double(d1), cob_decimal_get_double(d2)))


def cob_decimal_cmp(d1, d2):
    """Compare *d1* and *d2*; return -1, 0 or 1 (numeric.c L1102-L1107)."""
    align_decimal(d1, d2)
    if d1.value < d2.value:
        return -1
    if d1.value > d2.value:
        return 1
    return 0


# ===========================================================================
# Rounding engine (replaces the C ``COB_STORE_ROUND`` add-5 path; numeric.c
# L988-L1003) using decimal.Decimal so all seven COBOL ROUNDED MODEs are
# supported.  For the two modes the C runtime implements, the result is
# byte-for-byte identical (HALF_UP == add-5-then-truncate, DOWN == truncate).
# ===========================================================================

def _round_value_to_scale(value, from_scale, to_scale, rounding):
    """Re-scale integer mantissa *value* (at *from_scale*) to *to_scale*.

    *value* represents ``value * 10**-from_scale``.  When *to_scale* >=
    *from_scale* the mantissa is extended with trailing zeros (exact).
    Otherwise the surplus low-order digits are removed using the
    :mod:`decimal` *rounding* constant, and the rounded mantissa at *to_scale*
    is returned as a Python ``int``.
    """
    shift = from_scale - to_scale
    if shift <= 0:
        return value * _p10(-shift)
    if value == 0:
        return 0
    with decimal.localcontext() as ctx:
        # Precision must cover every significant digit of the operand so that
        # neither the scaleb nor the quantize step rounds prematurely.
        ctx.prec = len(str(abs(value))) + 2
        ctx.rounding = rounding
        scaled = decimal.Decimal(value).scaleb(-shift)
        rounded = scaled.quantize(decimal.Decimal(1), rounding=rounding)
        return int(rounded)


# ===========================================================================
# DISPLAY (zoned decimal) load/store (numeric.c L368-L456).
#
# A DISPLAY item stores one ASCII byte per digit; the operational sign is
# either overpunched on the leading/trailing digit byte or carried in a
# SEPARATE leading/trailing ``+``/``-`` byte.  Sign decoding/encoding is
# delegated to ``libcob_py.common`` (cob_get_sign / cob_put_sign), which
# normalises overpunched digits in place exactly like the C runtime.
# ===========================================================================

def _display_region(f):
    """Return ``(offset, length)`` of the digit bytes within ``f.data``.

    Excludes a SEPARATE sign byte: a SEPARATE-LEADING sign occupies
    ``f.data[0]`` (digits start at offset 1); a SEPARATE-TRAILING sign occupies
    the final byte; an overpunched (non-separate) sign shares a digit byte and
    therefore leaves the whole field as digits.  Mirrors the C ``COB_FIELD_DATA``
    / ``COB_FIELD_SIZE`` macro pair while operating directly on the backing
    ``bytearray`` (so in-place stores are not lost to slice copies).
    """
    size = common.COB_FIELD_SIZE(f)
    if common.COB_FIELD_SIGN_SEPARATE(f) and common.COB_FIELD_SIGN_LEADING(f):
        return 1, size
    return 0, size


def cob_decimal_set_display(d, f):
    """Load DISPLAY field *f* into decimal *d* (numeric.c L368-L415).

    Handles the figurative HIGH-VALUE (leading byte ``0xFF`` -> ``10**size``)
    and LOW-VALUE (leading byte ``0x00`` -> ``-10**size``) fills, then decodes
    the operational sign and parses the zoned digits.
    """
    offset, size = _display_region(f)
    scale = common.COB_FIELD_SCALE(f)
    if size > 0 and f.data[offset] == 255:
        d.value = _p10(size)
        d.scale = scale
        return
    if size > 0 and f.data[offset] == 0:
        d.value = -_p10(size)
        d.scale = scale
        return
    sign = common.cob_get_sign(f)
    val = 0
    for i in range(size):
        val = val * 10 + common.cob_d2i(f.data[offset + i])
    if sign < 0:
        val = -val
    d.value = val
    d.scale = scale
    common.cob_put_sign(f, sign)


def cob_decimal_get_display(d, f, opt):
    """Store decimal *d* (already at field scale) into DISPLAY field *f*.

    Port of numeric.c L417-L456.  Right-justifies the magnitude digits, zero
    padding on the left.  On overflow raises ``EC-SIZE-OVERFLOW``: with
    ``COB_STORE_KEEP_ON_OVERFLOW`` the store is abandoned and the exception code
    returned (driving ``ON SIZE ERROR``); otherwise the high-order digits are
    truncated.  Finally the sign is written back.
    """
    if d.value > 0:
        sign = 1
    elif d.value < 0:
        sign = -1
    else:
        sign = 0
    digits = str(abs(d.value))
    size = len(digits)
    offset, fsize = _display_region(f)
    diff = fsize - size
    if diff < 0:
        common.cob_set_exception(common.COB_EC_SIZE_OVERFLOW)
        if opt & common.COB_STORE_KEEP_ON_OVERFLOW:
            return common.cob_exception_code
        # Truncate the high-order digits, keeping the low-order ``fsize``.
        f.data[offset:offset + fsize] = digits[size - fsize:].encode("latin-1")
    else:
        f.data[offset:offset + diff] = b"0" * diff
        f.data[offset + diff:offset + fsize] = digits.encode("latin-1")
    common.cob_put_sign(f, sign)
    return 0


# ===========================================================================
# BINARY (COMP / COMP-4 / COMP-5 / COMP-X) load/store (numeric.c L458-L613).
# ===========================================================================

def cob_decimal_set_binary(d, f):
    """Load BINARY field *f* into decimal *d* (numeric.c L458-L506).

    All the C ``#ifdef`` limb-plumbing variants compute the same value: a
    signed field decodes via :func:`cob_binary_get_int64`, an unsigned field
    via :func:`cob_binary_get_uint64`; the scale is the field scale.  Python's
    bignum ``int`` makes the 64-bit split unnecessary.
    """
    if common.COB_FIELD_HAVE_SIGN(f):
        d.value = cob_binary_get_int64(f)
    else:
        d.value = cob_binary_get_uint64(f)
    d.scale = common.COB_FIELD_SCALE(f)


def cob_decimal_get_binary(d, f, opt):
    """Store decimal *d* (already at field scale) into BINARY field *f*.

    Port of numeric.c L508-L613.  Reproduces both overflow checks: the
    bit-width check (value too wide for ``size*8 - sign`` bits) and, when an
    arithmetic ``opt`` is given and the module requests binary truncation, the
    decimal digit-count check (``|value| >= 10**digits``).  On overflow with
    ``COB_STORE_KEEP_ON_OVERFLOW`` nothing is stored and the exception code is
    returned; with ``COB_STORE_TRUNC_ON_OVERFLOW`` the low-order ``digits``
    decimal digits are kept; otherwise the value is reduced modulo
    ``2**(size*8)`` (two's-complement low bytes).
    """
    if d.value == 0:
        f.data[:f.size] = bytes(f.size)
        return 0

    value = d.value
    digits = common.COB_FIELD_DIGITS(f)
    if common.COB_FIELD_HAVE_SIGN(f):
        sign = 1
    else:
        sign = 0
        if value < 0:
            value = -value

    overflow = 0
    bit_mask = (1 << (f.size * 8)) - 1
    bitnum = (f.size * 8) - sign
    if abs(value).bit_length() > bitnum:
        if opt & common.COB_STORE_KEEP_ON_OVERFLOW:
            common.cob_set_exception(common.COB_EC_SIZE_OVERFLOW)
            return common.cob_exception_code
        overflow = 1
        if opt & common.COB_STORE_TRUNC_ON_OVERFLOW:
            value = _tdiv_r(value, _p10(digits))
        else:
            value = value & bit_mask
    elif opt:
        module = common.cob_current_module
        binary_truncate = module.flag_binary_truncate if module is not None else 1
        if binary_truncate and abs(value) >= _p10(digits):
            if opt & common.COB_STORE_KEEP_ON_OVERFLOW:
                common.cob_set_exception(common.COB_EC_SIZE_OVERFLOW)
                return common.cob_exception_code
            overflow = 1
            if opt & common.COB_STORE_TRUNC_ON_OVERFLOW:
                value = _tdiv_r(value, _p10(digits))
            else:
                value = value & bit_mask

    _binary_store(f, value)
    if not overflow:
        return 0
    common.cob_set_exception(common.COB_EC_SIZE_OVERFLOW)
    return common.cob_exception_code


# ===========================================================================
# PACKED-DECIMAL / COMP-3 (numeric.c L616-L940).
#
# Two BCD digits per byte; the units digit occupies the HIGH nibble of the last
# byte and the sign occupies its LOW nibble (0x0C / 0x0F = positive, 0x0D =
# negative; 0x0F is used for unsigned items).  When the digit count is even the
# high nibble of the first byte is an unused zero pad.  Field size in bytes is
# ``digits // 2 + 1``.
# ===========================================================================

def cob_packed_get_sign(f):
    """Return the operational sign of packed field *f* (numeric.c L616-L626).

    ``+1`` / ``-1`` for a signed item (low nibble of the last byte == 0x0D ->
    negative), ``0`` for an unsigned item.
    """
    if not common.COB_FIELD_HAVE_SIGN(f):
        return 0
    return -1 if (f.data[f.size - 1] & 0x0f) == 0x0d else 1


def cob_complement_packed(f):
    """Ten's-complement the packed digits of *f* in place (numeric.c L628-L665).

    Walks the BCD nibbles from the units digit (high nibble of the last byte)
    backward, leaving the sign nibble untouched - a faithful port of the C
    nibble-walk used by :func:`cob_add_packed` when a subtraction underflows.
    """
    data = f.data
    ndigs = common.COB_FIELD_DIGITS(f)
    msn = 1
    emptydig = 1 - (common.COB_FIELD_DIGITS(f) % 2)
    carry = 0
    p = ((ndigs + emptydig) // 2) - (1 - msn)
    while ndigs:
        ndigs -= 1
        if not msn:
            tval = data[p] & 0x0f
        else:
            tval = (data[p] & 0xf0) >> 4
        tval += carry
        if tval > 0:
            carry = 1
            tval = 10 - tval
        else:
            carry = 0
        if not msn:
            data[p] = (data[p] & 0xf0) | tval
            msn = 1
        else:
            data[p] = (data[p] & 0x0f) | (tval << 4)
            msn = 0
            p -= 1


def cob_add_packed(f, ival):
    """Add integer *ival* to packed field *f* in place (numeric.c L667-L763).

    Reproduces the C BCD ripple-add nibble-walk exactly, including the scaling
    of *ival* by the field scale, the subtract-with-borrow path, and the
    ten's-complement / sign-flip fix-up when a subtraction crosses zero.
    """
    data = f.data
    val = ival

    ndigs = common.COB_FIELD_SCALE(f)
    while ndigs > 0:
        ndigs -= 1
        val *= 10

    ndigs = common.COB_FIELD_DIGITS(f)
    if ndigs <= 0:
        return
    sign = cob_packed_get_sign(f)
    msn = 1
    emptydig = 1 - (common.COB_FIELD_DIGITS(f) % 2)
    subtr = 0
    zeroes = 0

    # -x +v = -(x - v), -x -v = -(x + v)
    if sign < 0:
        val = -val
    if val < 0:
        val = -val
        subtr = 1
    p = ((ndigs + emptydig) // 2) - (1 - msn)
    origdigs = ndigs
    carry = 0
    while ndigs:
        ndigs -= 1
        if not msn:
            tval = data[p] & 0x0f
        else:
            tval = (data[p] & 0xf0) >> 4
        if val:
            carry += (val % 10)
            val //= 10
        if subtr:
            tval -= carry
            if tval < 0:
                tval += 10
                carry = 1
            else:
                carry = 0
        else:
            tval += carry
            if tval > 9:
                tval %= 10
                carry = 1
            else:
                carry = 0
        if tval == 0:
            zeroes += 1
        if not msn:
            data[p] = (data[p] & 0xf0) | tval
            msn = 1
        else:
            data[p] = (data[p] & 0x0f) | (tval << 4)
            msn = 0
            p -= 1

    if sign:
        p = f.size - 1
        if origdigs == zeroes:
            data[p] = (data[p] & 0xf0) | 0x0c
        elif subtr and carry:
            cob_complement_packed(f)
            sign = -sign
            if sign < 0:
                data[p] = (data[p] & 0xf0) | 0x0d
            else:
                data[p] = (data[p] & 0xf0) | 0x0c
    elif subtr and carry:
        cob_complement_packed(f)


def cob_decimal_set_packed(d, f):
    """Load packed field *f* into decimal *d* (numeric.c L765-L831).

    The C runtime splits this into a ``< 10`` digit fast path (native int) and a
    wide path (GMP); both compute the same integer, so the single bignum loop
    here is exact.  The redundant ``if (val)`` / ``if (*p)`` C micro-guards are
    dropped because multiplying/adding zero is a no-op.
    """
    data = f.data
    digits = common.COB_FIELD_DIGITS(f)
    sign = cob_packed_get_sign(f)
    p = 0
    if digits % 2 == 0:
        val = data[p] & 0x0f
        digits -= 1
        p += 1
    else:
        val = 0
    while digits > 1:
        val = val * 100 + (data[p] >> 4) * 10 + (data[p] & 0x0f)
        digits -= 2
        p += 1
    val = val * 10 + (data[p] >> 4)
    if sign < 0:
        val = -val
    d.value = val
    d.scale = common.COB_FIELD_SCALE(f)


def cob_decimal_get_packed(d, f, opt):
    """Store decimal *d* (already at field scale) into packed field *f*.

    Port of numeric.c L833-L893.  On overflow raises ``EC-SIZE-OVERFLOW``; with
    ``COB_STORE_KEEP_ON_OVERFLOW`` the store is abandoned, otherwise the
    high-order digits are dropped.  Lays the BCD nibbles right-justified and
    writes the sign nibble into the low nibble of the final byte.
    """
    if d.value > 0:
        sign = 1
    elif d.value < 0:
        sign = -1
    else:
        sign = 0
    digit_bytes = str(abs(d.value)).encode("latin-1")
    size = len(digit_bytes)
    data = f.data
    digits = common.COB_FIELD_DIGITS(f)
    q = 0
    diff = digits - size
    if diff < 0:
        common.cob_set_exception(common.COB_EC_SIZE_OVERFLOW)
        if opt & common.COB_STORE_KEEP_ON_OVERFLOW:
            return common.cob_exception_code
        q += size - digits
        size = digits

    for k in range(f.size):
        data[k] = 0
    p = (digits // 2) - (size // 2)
    pad = 1 - (size % 2)
    i = pad
    n = 0
    while i < size + pad:
        x = common.cob_d2i(digit_bytes[q + n])
        if i % 2 == 0:
            data[p] = x << 4
        else:
            data[p] |= x
            p += 1
        i += 1
        n += 1

    p = f.size - 1
    if not common.COB_FIELD_HAVE_SIGN(f):
        data[p] = (data[p] & 0xf0) | 0x0f
    elif sign < 0:
        data[p] = (data[p] & 0xf0) | 0x0d
    else:
        data[p] = (data[p] & 0xf0) | 0x0c
    return 0


def cob_set_packed_zero(f):
    """Set packed field *f* to zero with the correct sign nibble (numeric.c L895)."""
    for k in range(f.size):
        f.data[k] = 0
    if not common.COB_FIELD_HAVE_SIGN(f):
        f.data[f.size - 1] = 0x0f
    else:
        f.data[f.size - 1] = 0x0c


def cob_set_packed_int(f, val):
    """Store small integer *val* directly into packed field *f* (numeric.c L906-L940)."""
    data = f.data
    if val < 0:
        n = -val
        sign = 1
    else:
        n = val
        sign = 0
    for k in range(f.size):
        data[k] = 0
    p = f.size - 1
    data[p] = (n % 10) << 4
    if not common.COB_FIELD_HAVE_SIGN(f):
        data[p] |= 0x0f
    elif sign:
        data[p] |= 0x0d
    else:
        data[p] |= 0x0c
    n //= 10
    p -= 1
    while n and p >= 0:
        data[p] = packed_bytes[n % 100]
        n //= 100
        p -= 1
    if common.COB_FIELD_DIGITS(f) % 2 == 0:
        data[0] &= 0x0f


# ===========================================================================
# General field dispatch (numeric.c L942-L1039).
# ===========================================================================

def cob_decimal_set_field(d, f):
    """Load any numeric field *f* into decimal *d* by USAGE (numeric.c L942-L967)."""
    ftype = common.COB_FIELD_TYPE(f)
    if ftype == common.COB_TYPE_NUMERIC_BINARY:
        cob_decimal_set_binary(d, f)
    elif ftype == common.COB_TYPE_NUMERIC_PACKED:
        cob_decimal_set_packed(d, f)
    elif ftype == common.COB_TYPE_NUMERIC_FLOAT:
        fval = struct.unpack("=f", bytes(f.data[:4]))[0]
        cob_decimal_set_double(d, float(fval))
    elif ftype == common.COB_TYPE_NUMERIC_DOUBLE:
        dval = struct.unpack("=d", bytes(f.data[:8]))[0]
        cob_decimal_set_double(d, dval)
    else:
        cob_decimal_set_display(d, f)


def cob_decimal_get_field(d, f, opt, rounding=None):
    """Store decimal *d* into field *f* honouring USAGE, rounding and overflow.

    Port of numeric.c L969-L1039.  *opt* is the bitmask of ``COB_STORE_*``
    flags emitted for the COBOL statement.  The optional *rounding* argument is
    an extension point (AAP section 0.6.2): when the COBOL ``ROUNDED MODE``
    clause is present the emitter passes the mapped :mod:`decimal` constant
    (see :data:`ROUND_MODE_MAP`); when omitted, ``COB_STORE_ROUND`` selects
    HALF_UP (byte-for-byte identical to the C add-5 path) and its absence
    selects truncation toward zero.

    A NaN-flagged decimal (e.g. after divide-by-zero) raises
    ``EC-SIZE-OVERFLOW`` and stores nothing.  As in the C runtime the decimal is
    copied to the scratch ``cob_d1`` first (unless it already is ``cob_d1``) so
    callers such as :func:`cob_div_remainder` are not disturbed.
    """
    if d.scale == DECIMAL_NAN:
        common.cob_set_exception(common.COB_EC_SIZE_OVERFLOW)
        return common.cob_exception_code

    if d is not cob_d1:
        cob_decimal_set(cob_d1, d)
        d = cob_d1

    target = common.COB_FIELD_SCALE(f)
    if opt & common.COB_STORE_ROUND:
        mode = rounding if rounding is not None else COB_ROUND_DEFAULT
    else:
        mode = COB_ROUND_TRUNCATION

    # Per-statement decimal context (AAP 0.6.2): enables the Overflow /
    # DivisionByZero traps mapped to the EC-SIZE family.  A trapped signal sets
    # the COBOL exception and leaves ``result`` unset, so the store is skipped
    # exactly as the COBOL ON SIZE ERROR path requires.  The digit-capacity
    # checks inside the get_* stores remain the byte-equivalent SIZE-ERROR
    # detector for the normal COBOL case (integer digits exceed the PICTURE).
    result = None
    with cob_size_error_context(opt, rounding):
        # Round/extend the mantissa to the field scale (replaces the C add-5
        # round followed by the truncating shift_decimal).
        d.value = _round_value_to_scale(d.value, d.scale, target, mode)
        d.scale = target

        ftype = common.COB_FIELD_TYPE(f)
        if ftype == common.COB_TYPE_NUMERIC_BINARY:
            result = cob_decimal_get_binary(d, f, opt)
        elif ftype == common.COB_TYPE_NUMERIC_PACKED:
            result = cob_decimal_get_packed(d, f, opt)
        elif ftype == common.COB_TYPE_NUMERIC_DISPLAY:
            result = cob_decimal_get_display(d, f, opt)
        elif ftype == common.COB_TYPE_NUMERIC_FLOAT:
            f.data[:4] = bytearray(struct.pack("=f", cob_decimal_get_double(d)))
            result = 0
        elif ftype == common.COB_TYPE_NUMERIC_DOUBLE:
            f.data[:8] = bytearray(struct.pack("=d", cob_decimal_get_double(d)))
            result = 0
        else:
            # Default (NUMERIC-EDITED and friends): render to a DISPLAY
            # temporary of the field's digit count, then MOVE it (the
            # editing/insertion lives in libcob_py.move).  ``common._lazy_move``
            # performs the deferred import so numeric.py keeps ``common`` as its
            # only sibling dependency.
            digits = common.COB_FIELD_DIGITS(f)
            temp_attr = common.cob_field_attr(
                type=common.COB_TYPE_NUMERIC_DISPLAY, digits=digits,
                scale=target, flags=common.COB_FLAG_HAVE_SIGN, pic=None)
            temp = common.cob_field(size=digits, data=bytearray(digits),
                                    attr=temp_attr)
            if cob_decimal_get_display(d, temp, opt) == 0:
                common._lazy_move(temp, f)
            result = common.cob_exception_code

    if result is None:
        # A decimal trap fired and was mapped to EC-SIZE; the store was skipped.
        return common.cob_exception_code
    return result


# ===========================================================================
# Optimised in-place DISPLAY arithmetic (numeric.c L1113-L1280).
#
# These operate directly on the field's digit ``bytearray`` over the half-open
# index range ``[offset, offset+size)`` (an explicit offset replaces the C
# pointer arithmetic and keeps the store in place even for a SEPARATE-LEADING
# sign).  Digit bytes are ASCII; ``byte & 0x0F`` recovers the digit value and
# ``ord('0') + value`` re-encodes it.
# ===========================================================================

def display_add_int(data, offset, size, n):
    """Add non-negative integer *n* to the zoned digits ``data[offset:offset+size]``.

    Returns non-zero on overflow off the high end (honouring the module's
    ``flag_binary_truncate``), exactly like numeric.c L1113-L1159.
    """
    sp = offset + size
    carry = 0
    while n > 0:
        i = n % 10
        n //= 10
        sp -= 1
        if sp < offset:
            module = common.cob_current_module
            if module is not None and not module.flag_binary_truncate:
                return 0
            return 1
        total = (data[sp] & 0x0F) + i + carry
        if total > 9:
            carry = 1
            data[sp] = ord("0") + (total % 10)
        else:
            carry = 0
            data[sp] = ord("0") + total
    if carry == 0:
        return 0
    sp -= 1
    while sp >= offset:
        data[sp] += 1
        if data[sp] <= ord("9"):
            return 0
        data[sp] = ord("0")
        sp -= 1
    module = common.cob_current_module
    if module is not None and not module.flag_binary_truncate:
        return 0
    return 1


def display_sub_int(data, offset, size, n):
    """Subtract non-negative integer *n* from the zoned digits.

    Returns non-zero when the subtraction borrows off the high end (the caller
    then takes the nine's-complement path), per numeric.c L1161-L1198.
    """
    sp = offset + size
    carry = 0
    while n > 0:
        i = n % 10
        n //= 10
        sp -= 1
        if sp < offset:
            return 1
        data[sp] -= (i + carry)
        if data[sp] < ord("0"):
            carry = 1
            data[sp] += 10
        else:
            carry = 0
    if carry == 0:
        return 0
    sp -= 1
    while sp >= offset:
        data[sp] -= 1
        if data[sp] >= ord("0"):
            return 0
        data[sp] = ord("9")
        sp -= 1
    return 1


def cob_display_add_int(f, in_):
    """Add integer *in_* to DISPLAY field *f* in place (numeric.c L1200-L1280).

    Scales *in_* to the field scale, then adds (or subtracts, with a
    nine's-complement sign-flip when the magnitude crosses zero).  Restores the
    original digits and raises ``EC-SIZE-OVERFLOW`` on a high-end overflow.
    """
    n = in_
    offset, size = _display_region(f)
    scale = common.COB_FIELD_SCALE(f)
    sign = common.cob_get_sign(f)
    osize = size
    saved = bytes(f.data[offset:offset + osize])
    if sign < 0:
        n = -n
    while scale > 0:
        scale -= 1
        n *= 10
    if scale < 0:
        # PIC 9(n)P(m): scale the integer down (truncating toward zero).
        if -scale < 10:
            while scale:
                scale += 1
                n = _tdiv_q(n, 10)
        else:
            n = 0
    else:
        # PIC 9(n)V9(m): scale is now zero, so this is a faithful no-op that
        # mirrors the C ``size -= scale`` for the fractional case.
        size -= scale

    if n > 0:
        if display_add_int(f.data, offset, size, n) != 0:
            f.data[offset:offset + osize] = saved
            common.cob_put_sign(f, sign)
            common.cob_set_exception(common.COB_EC_SIZE_OVERFLOW)
            return common.cob_exception_code
    elif n < 0:
        if display_sub_int(f.data, offset, size, -n) != 0:
            for i in range(size):
                f.data[offset + i] = common.cob_i2d(
                    9 - common.cob_d2i(f.data[offset + i]))
            display_add_int(f.data, offset, size, 1)
            sign = -sign

    common.cob_put_sign(f, sign)
    return 0


# ===========================================================================
# Statement-level convenience functions (numeric.c L1282-L1461).
# ===========================================================================

def cob_add(f1, f2, opt):
    """``f1 = f1 + f2`` honouring *opt* rounding/overflow (numeric.c L1282-L1289)."""
    cob_decimal_set_field(cob_d1, f1)
    cob_decimal_set_field(cob_d2, f2)
    cob_decimal_add(cob_d1, cob_d2)
    return cob_decimal_get_field(cob_d1, f1, opt)


def cob_sub(f1, f2, opt):
    """``f1 = f1 - f2`` honouring *opt* rounding/overflow (numeric.c L1291-L1298)."""
    cob_decimal_set_field(cob_d1, f1)
    cob_decimal_set_field(cob_d2, f2)
    cob_decimal_sub(cob_d1, cob_d2)
    return cob_decimal_get_field(cob_d1, f1, opt)


def cob_add_int(f, n):
    """``f += n`` with USAGE fast paths (numeric.c L1300-L1325).

    DISPLAY and PACKED items use their dedicated in-place adders; every other
    USAGE goes through the decimal engine.  ``n`` is aligned to the field scale
    before the add (matching the C ``mpz_ui_pow_ui`` scaling for scale > 0).
    """
    if n == 0:
        return 0
    ftype = common.COB_FIELD_TYPE(f)
    if ftype == common.COB_TYPE_NUMERIC_DISPLAY:
        return cob_display_add_int(f, n)
    if ftype == common.COB_TYPE_NUMERIC_PACKED:
        cob_add_packed(f, n)
        return 0
    cob_decimal_set_field(cob_d1, f)
    cob_d2.value = n
    cob_d2.scale = 0
    if cob_d1.scale > 0:
        cob_d2.value *= _p10(cob_d1.scale)
        cob_d2.scale = cob_d1.scale
    cob_d1.value += cob_d2.value
    return cob_decimal_get_field(cob_d1, f, 0)


def cob_sub_int(f, n):
    """``f -= n`` (numeric.c L1327-L1334); implemented as ``cob_add_int(f, -n)``."""
    if n == 0:
        return 0
    return cob_add_int(f, -n)


def cob_div_quotient(dividend, divisor, quotient, opt):
    """Compute ``quotient = dividend / divisor`` and stash the remainder.

    Port of numeric.c L1336-L1365.  The remainder is left in the scratch
    ``cob_d3`` for the matching :func:`cob_div_remainder` call (mirroring the C
    file-scope statics).  A NaN result (divide by zero) propagates to ``cob_d3``
    and the exception code is returned.
    """
    cob_decimal_set_field(cob_d1, dividend)
    cob_decimal_set_field(cob_d2, divisor)
    cob_decimal_set(cob_d3, cob_d1)

    cob_decimal_div(cob_d1, cob_d2)
    if cob_d1.scale == DECIMAL_NAN:
        cob_d3.scale = DECIMAL_NAN
        return common.cob_exception_code

    cob_decimal_set(cob_d4, cob_d1)
    ret = cob_decimal_get_field(cob_d1, quotient, opt)

    # remainder = dividend - (truncated quotient * divisor)
    shift_decimal(cob_d4, common.COB_FIELD_SCALE(quotient) - cob_d4.scale)
    cob_decimal_mul(cob_d4, cob_d2)
    cob_decimal_sub(cob_d3, cob_d4)
    return ret


def cob_div_remainder(fld_remainder, opt):
    """Store the remainder stashed by :func:`cob_div_quotient` (numeric.c L1367-L1371)."""
    return cob_decimal_get_field(cob_d3, fld_remainder, opt)


def cob_cmp_int(f1, n):
    """Compare numeric field *f1* with signed int *n*; -1/0/1 (numeric.c L1373-L1380)."""
    cob_decimal_set_field(cob_d1, f1)
    cob_d2.value = n
    cob_d2.scale = 0
    return cob_decimal_cmp(cob_d1, cob_d2)


def cob_cmp_uint(f1, n):
    """Compare numeric field *f1* with unsigned int *n*; -1/0/1 (numeric.c L1382-L1389)."""
    cob_decimal_set_field(cob_d1, f1)
    cob_d2.value = n
    cob_d2.scale = 0
    return cob_decimal_cmp(cob_d1, cob_d2)


def cob_numeric_cmp(f1, f2):
    """Compare two numeric fields; -1/0/1 (numeric.c L1391-L1397)."""
    cob_decimal_set_field(cob_d1, f1)
    cob_decimal_set_field(cob_d2, f2)
    return cob_decimal_cmp(cob_d1, cob_d2)


# Scratch state for cob_cmp_packed - reproduces the C ``static`` ``lastval`` and
# the file-scope ``packed_value`` buffer (numeric.c L1399-L1461).
_cmp_packed_value = bytearray(20)
_cmp_packed_lastval = 0


def cob_cmp_packed(f, n):
    """Compare packed field *f* with int *n*; -1/0/1 (numeric.c L1399-L1461).

    Builds a 20-byte big-endian magnitude image of both operands (sign and pad
    nibbles masked off) and compares byte-by-byte; the cached image of *n* is
    reused across calls exactly as the C static does.
    """
    global _cmp_packed_lastval
    sign = cob_packed_get_sign(f)
    if sign >= 0 and n < 0:
        return 1
    if sign < 0 and n >= 0:
        return -1

    val1 = bytearray(20)
    inc = 0
    pad = 20 - f.size
    for size in range(20):
        if size < pad:
            val1[size] = 0
        else:
            val1[size] = f.data[inc]
            inc += 1
    val1[19] &= 0xf0
    if (common.COB_FIELD_DIGITS(f) % 2) == 0:
        val1[pad] &= 0x0f

    if n != _cmp_packed_lastval:
        _cmp_packed_lastval = n
        magnitude = -n if n < 0 else n
        for k in range(14, 20):
            _cmp_packed_value[k] = 0
        if magnitude:
            p = 19
            _cmp_packed_value[p] = (magnitude % 10) << 4
            p -= 1
            magnitude //= 10
            while magnitude:
                two = magnitude % 100
                _cmp_packed_value[p] = (two % 10) | ((two // 10) << 4)
                magnitude //= 100
                p -= 1

    for size in range(20):
        if val1[size] != _cmp_packed_value[size]:
            if sign < 0:
                return _cmp_packed_value[size] - val1[size]
            return val1[size] - _cmp_packed_value[size]
    return 0


# ===========================================================================
# codegen.h inline mirrors - integer fast paths the emitter calls directly
# (codegen.h L188-L271).  These take raw operands (a digit ``bytes`` buffer or a
# ``cob_field``) and return Python ints, matching the C signatures.
# ===========================================================================

def cob_get_numdisp(data, size):
    """Parse *size* zoned-DISPLAY digit bytes into an int (codegen.h L188-L202).

    Reproduces the C quirk that a byte greater than ``'9'`` (a trailing
    overpunch) contributes 10 to that digit position.
    """
    retval = 0
    for n in range(size):
        retval *= 10
        byte = data[n]
        if byte > ord("9"):
            retval += 10
        else:
            retval += (byte - ord("0"))
    return retval


def cob_cmp_packed_int(f, n):
    """Compare packed field *f* with int *n*; -1/0/1 (codegen.h L205-L223).

    Fast nibble walk: every nibble before the final low nibble is a digit; the
    final low nibble (0x0D) marks a negative value.
    """
    data = f.data
    val = 0
    p = 0
    for _ in range(f.size - 1):
        val = val * 10 + (data[p] >> 4)
        val = val * 10 + (data[p] & 0x0f)
        p += 1
    val = val * 10 + (data[p] >> 4)
    if (data[p] & 0x0f) == 0x0d:
        val = -val
    return -1 if val < n else (1 if val > n else 0)


def cob_get_packed_int(f):
    """Return packed field *f* as an int (codegen.h L227-L247)."""
    data = f.data
    val = 0
    p = 0
    for _ in range(f.size - 1):
        val = val * 10 + (data[p] >> 4)
        val = val * 10 + (data[p] & 0x0f)
        p += 1
    val = val * 10 + (data[p] >> 4)
    if (data[p] & 0x0f) == 0x0d:
        val = -val
    return val


def cob_add_packed_int(f, val):
    """Add int *val* to packed field *f* in place (codegen.h L249-L271).

    Handles the common same-sign case with a BCD ripple add; an opposite-sign
    add (which may flip the field sign) is delegated to :func:`cob_add_int`,
    exactly as the C inline does.
    """
    if val == 0:
        return 0
    data = f.data
    p = f.size - 1
    if (data[p] & 0x0f) == 0x0d:
        if val > 0:
            return cob_add_int(f, val)
        n = -val
    else:
        if val < 0:
            return cob_add_int(f, val)
        n = val
    inc = (data[p] >> 4) + (n % 10)
    n //= 10
    carry = inc // 10
    data[p] = ((inc % 10) << 4) | (data[p] & 0x0f)
    p -= 1
    for _ in range(f.size - 1):
        if not carry and not n:
            break
        inc = ((data[p] >> 4) * 10) + (data[p] & 0x0f) + carry + (n % 100)
        carry = inc // 100
        n //= 100
        inc %= 100
        data[p] = ((inc // 10) << 4) | (inc % 10)
        p -= 1
    return 0


# ===========================================================================
# Numeric DISPLAY compares (numeric.c L1484-L1777).
# ===========================================================================

def cob_cmp_numdisp(data, size, n):
    """Compare *size* unsigned zoned digits at *data* with int *n* (numeric.c L1484)."""
    val = 0
    for i in range(size):
        val = val * 10 + (data[i] - ord("0"))
    return -1 if val < n else (1 if val > n else 0)


def cob_cmp_long_numdisp(data, size, n):
    """Wide (long long) variant of :func:`cob_cmp_numdisp` (numeric.c L1498).

    Python ``int`` is unbounded, so the logic is identical to the narrow form.
    """
    val = 0
    for i in range(size):
        val = val * 10 + (data[i] - ord("0"))
    return -1 if val < n else (1 if val > n else 0)


# EBCDIC overpunch sign decode table - positive zone '{','A'..'I' (+0..+9),
# negative zone '}','J'..'R' (-0..-9).  Mirrors cob_get_ebcdic_sign
# (numeric.c L1586-L1650): returns ``(digit_added, is_negative)``.
_EBCDIC_SIGN_DECODE = {ord("{"): (0, False), ord("}"): (0, True)}
for _idx, _ch in enumerate("ABCDEFGHI", start=1):
    _EBCDIC_SIGN_DECODE[ord(_ch)] = (_idx, False)
for _idx, _ch in enumerate("JKLMNOPQR", start=1):
    _EBCDIC_SIGN_DECODE[ord(_ch)] = (_idx, True)
del _idx, _ch

# ASCII overpunch 'p'..'y' -> 0..9 (the COB_EBCDIC_MACHINE branch of
# cob_get_ascii_sign, numeric.c L1513-L1547).
_ASCII_SIGN_DECODE = {ord("p") + _k: _k for _k in range(10)}


def cob_get_ebcdic_sign(byte):
    """Decode an EBCDIC overpunch *byte* -> ``(digit_added, is_negative)``.

    Port of the static ``cob_get_ebcdic_sign`` (numeric.c L1586-L1650); unknown
    bytes decode to ``(0, False)`` like the C ``default`` arm.
    """
    return _EBCDIC_SIGN_DECODE.get(byte, (0, False))


def cob_get_long_ebcdic_sign(byte):
    """Wide variant of :func:`cob_get_ebcdic_sign` (numeric.c L1652-L1716)."""
    return _EBCDIC_SIGN_DECODE.get(byte, (0, False))


def cob_get_ascii_sign(byte):
    """Decode an ASCII overpunch *byte* ('p'..'y') -> digit 0..9 (numeric.c L1513).

    Only used on an EBCDIC host; unknown bytes yield 0 like the C switch's
    fall-through.
    """
    return _ASCII_SIGN_DECODE.get(byte, 0)


def cob_get_long_ascii_sign(byte):
    """Wide variant of :func:`cob_get_ascii_sign` (numeric.c L1549-L1584)."""
    return _ASCII_SIGN_DECODE.get(byte, 0)


def _cmp_sign_numdisp(data, size, n):
    """Shared body of cob_cmp_sign_numdisp / cob_cmp_long_sign_numdisp.

    The trailing digit carries the operational sign: a plain ``'0'..'9'`` byte
    is positive; otherwise the sign is read from the EBCDIC zone (when the
    active module uses EBCDIC display signs) or as an ASCII negative overpunch.
    """
    val = 0
    for i in range(size - 1):
        val = val * 10 + (data[i] - ord("0"))
    val *= 10
    last = data[size - 1]
    if ord("0") <= last <= ord("9"):
        val += (last - ord("0"))
    else:
        module = common.cob_current_module
        if module is not None and module.display_sign:
            delta, negative = cob_get_ebcdic_sign(last)
            val += delta
            if negative:
                val = -val
        else:
            # ASCII negative overpunch: 'p'..'y' -> 0..9, value is negative.
            val += (last - ord("p"))
            val = -val
    return -1 if val < n else (1 if val > n else 0)


def cob_cmp_sign_numdisp(data, size, n):
    """Compare *size* signed zoned digits at *data* with int *n* (numeric.c L1718)."""
    return _cmp_sign_numdisp(data, size, n)


def cob_cmp_long_sign_numdisp(data, size, n):
    """Wide variant of :func:`cob_cmp_sign_numdisp` (numeric.c L1749)."""
    return _cmp_sign_numdisp(data, size, n)


# ===========================================================================
# Binary integer fast paths (codegen.h L32-L102, L505-L2675).
#
# The C header generates a dense family of width/sign specialised inline
# helpers.  They all reduce to: read ``NN/8`` bytes as a native
# (``cob_cmp_*`` / ``cob_add_*`` / ``cob_sub_*``) or byte-swapped/big-endian
# (``cob_cmpswp_*`` / ``cob_addswp_*`` / ``cob_subswp_*`` / ``cob_setswp_*``)
# integer of the given signedness, then compare / add / subtract / store.
# We synthesise the EXACT ``cob_*_{u,s}NN_binary`` names the emitter can produce
# (the authoritative set is the 128 ``cob_*_binary`` declarations in
# ``libcob/codegen.h``) so every call-site resolves unchanged, with the per-width
# parameters bound as defaults to avoid closure late-binding.  Aligned variants
# (``cob_*_align_*``, including ``cob_cmpswp_align_*``) are semantic aliases of
# their unaligned namesakes (memory alignment is a C-only concern).  An
# emitted-symbol audit test (tests/libcob_py/test_numeric.py) asserts this family
# is exhaustive vs codegen.h so a missing helper can never silently recur.
# ===========================================================================

_NATIVE_ORDER = sys.byteorder
_BIN_WIDTHS = (8, 16, 24, 32, 40, 48, 56, 64)
_ALIGN_WIDTHS = (16, 32, 64)


def _cmp_bin(data, n, nbytes, signed, order):
    """Compare ``nbytes`` of *data* (in *order*, *signed*) against int *n*."""
    if not signed and n < 0:
        return 1
    val = int.from_bytes(bytes(data[:nbytes]), order, signed=signed)
    return -1 if val < n else (1 if val > n else 0)


def _add_bin(data, val, nbytes, signed, order):
    """Add int *val* into the ``nbytes`` integer at *data* (wraps mod 2**bits)."""
    cur = int.from_bytes(bytes(data[:nbytes]), order, signed=signed)
    cur += val
    mask = (1 << (nbytes * 8)) - 1
    data[:nbytes] = bytearray((cur & mask).to_bytes(nbytes, order))


def _setswp_bin(data, val, nbytes):
    """Store int *val* as a big-endian ``nbytes`` integer at *data*."""
    mask = (1 << (nbytes * 8)) - 1
    data[:nbytes] = bytearray((val & mask).to_bytes(nbytes, "big"))


def _register_binary_family():
    """Synthesise the ``cob_*_{u,s}NN_binary`` helper family into the module."""
    g = globals()
    for bits in _BIN_WIDTHS:
        nbytes = bits // 8
        for prefix, is_signed in (("u", False), ("s", True)):
            def _cmp(data, n, _nb=nbytes, _sg=is_signed):
                return _cmp_bin(data, n, _nb, _sg, _NATIVE_ORDER)

            def _add(data, val, _nb=nbytes, _sg=is_signed):
                _add_bin(data, val, _nb, _sg, _NATIVE_ORDER)

            def _sub(data, val, _nb=nbytes, _sg=is_signed):
                _add_bin(data, -val, _nb, _sg, _NATIVE_ORDER)

            g["cob_cmp_%s%d_binary" % (prefix, bits)] = _cmp
            g["cob_add_%s%d_binary" % (prefix, bits)] = _add
            g["cob_sub_%s%d_binary" % (prefix, bits)] = _sub

            if bits >= 16:
                def _cmpswp(data, n, _nb=nbytes, _sg=is_signed):
                    return _cmp_bin(data, n, _nb, _sg, "big")

                def _setswp(data, val, _nb=nbytes):
                    _setswp_bin(data, val, _nb)

                # MIGRATION (C->Python) / REVIEW FIX: byte-swapped read-modify-write
                # add/sub helpers (codegen.h ``cob_addswp_*`` L1940 / ``cob_subswp_*``
                # L2237).  C reads the operand big-endian, applies +/- val, then
                # writes it back big-endian - i.e. ``*p = COB_BSWAP(COB_BSWAP(*p)
                # +/- val)`` with low-byte truncation on overflow.  ``_add_bin`` with
                # order="big" reproduces this exactly (negation gives subtract).
                # These 28 symbols (u/s x widths 16..64) were previously NOT
                # synthesised, so a COMP/COMP-4 (BINARY-SWAP) ADD/SUBTRACT routed
                # here by typeck.c/codegen.c raised AttributeError at run time.
                def _addswp(data, val, _nb=nbytes, _sg=is_signed):
                    _add_bin(data, val, _nb, _sg, "big")

                def _subswp(data, val, _nb=nbytes, _sg=is_signed):
                    _add_bin(data, -val, _nb, _sg, "big")

                g["cob_cmpswp_%s%d_binary" % (prefix, bits)] = _cmpswp
                g["cob_setswp_%s%d_binary" % (prefix, bits)] = _setswp
                g["cob_addswp_%s%d_binary" % (prefix, bits)] = _addswp
                g["cob_subswp_%s%d_binary" % (prefix, bits)] = _subswp

    # Aligned variants: identical semantics in Python (alignment is a C memory
    # concern), so alias them to the plain helpers.  This covers the native
    # aligned family (``cob_*_align_*``) AND the byte-swapped aligned compare
    # (``cob_cmpswp_align_*``, codegen.h L472) - in C the only difference between
    # ``cob_cmpswp_align_sNN`` and ``cob_cmpswp_sNN`` is the (here irrelevant)
    # alignment of the big-endian load; the value semantics are identical, so the
    # alias is byte-for-byte correct.  REVIEW FIX: the 6 ``cob_cmpswp_align_*``
    # symbols were previously absent.
    for bits in _ALIGN_WIDTHS:
        for prefix in ("u", "s"):
            g["cob_cmp_align_%s%d_binary" % (prefix, bits)] = (
                g["cob_cmp_%s%d_binary" % (prefix, bits)])
            g["cob_add_align_%s%d_binary" % (prefix, bits)] = (
                g["cob_add_%s%d_binary" % (prefix, bits)])
            g["cob_sub_align_%s%d_binary" % (prefix, bits)] = (
                g["cob_sub_%s%d_binary" % (prefix, bits)])
            g["cob_cmpswp_align_%s%d_binary" % (prefix, bits)] = (
                g["cob_cmpswp_%s%d_binary" % (prefix, bits)])


_register_binary_family()
