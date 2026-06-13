"""libcob_py.move - data-movement runtime (pure-Python port of ``libcob/move.c``).

This is the dedicated MOVE / data-conversion subsystem identified as the GAP
module in AAP section 0.6.5: ``libcob/move.c`` is a compiled, first-class C
runtime module (built into ``libcob.la`` and initialised between ``strings`` and
``intrinsic`` in the ``cob_init_*`` sequence) yet it was absent from the
prompt's nine-module ``libcob_py`` table.  It is reproduced here so that:

* the rewritten emitter (``cobc/codegen.c``) can route every ``MOVE`` statement
  and every ``cob_move`` / ``cob_set_int`` / ``cob_get_int`` call-site to
  ``libcob_py.move.<fn>`` (codegen ``codegen_pymod`` rule 9), and
* the sibling runtime modules (``common``, ``numeric``, ``strings``, ``fileio``,
  ``termio``) that reach data-movement through ``common._lazy_move`` /
  ``cob_get_int`` / ``cob_set_int`` have a concrete provider rather than a
  deferred import that fails at runtime.

The port mirrors ``libcob/move.c`` function-for-function so the byte-for-byte
numeric / editing results are preserved (AAP section 0.6.2 numeric-parity gate).

Standard library only - this module introduces ZERO third-party dependencies
(AAP sections 0.5 / 0.7.1).  Its only sibling imports are ``common`` (the base
module - field structures, sign handling, field-attribute accessors) and
``numeric`` (the binary load/store and packed-decimal primitives, reused so the
binary byte-order / sign-extension logic lives in exactly one place).
"""

import struct

from libcob_py import common
from libcob_py import numeric


# ===========================================================================
# Powers of ten (move.c L40-L72).  Python ints are unbounded, so a single table
# serves both the former ``cob_exp10`` (int) and ``cob_exp10LL`` (long long)
# scaling uses; index i yields 10**i.
# ===========================================================================
_COB_EXP10 = tuple(10 ** i for i in range(19))


def _exp10(n):
    """Return ``10**n`` for a non-negative scale (move.c cob_exp10 lookup)."""
    if 0 <= n < len(_COB_EXP10):
        return _COB_EXP10[n]
    return 10 ** n


# ===========================================================================
# cob_current_module accessors with safe fallbacks.
#
# The C code reads ``cob_current_module->decimal_point`` / ``currency_symbol``
# / ``numeric_separator`` / ``flag_binary_truncate`` directly.  During unit
# testing (and before cob_init runs) there may be no current module, so these
# helpers fall back to the canonical ASCII ``.`` / ``$`` / ``,`` and the
# truncate-on default - exactly the cob_module constructor defaults.
# ===========================================================================
def _decimal_point():
    m = common.cob_current_module
    return m.decimal_point if m is not None else ord(".")


def _currency_symbol():
    m = common.cob_current_module
    return m.currency_symbol if m is not None else ord("$")


def _numeric_separator():
    m = common.cob_current_module
    return m.numeric_separator if m is not None else ord(",")


def _flag_binary_truncate():
    m = common.cob_current_module
    return m.flag_binary_truncate if m is not None else 1


# ===========================================================================
# Destination digit region.
#
# ``COB_FIELD_DATA`` returns a *slice copy* of ``f.data`` for a SEPARATE-LEADING
# sign (digits start at offset 1), so writing through it would be lost.  Every
# in-place store therefore resolves an explicit ``(offset, size)`` against the
# backing ``bytearray`` - the same pattern numeric.py uses (``_display_region``).
# ===========================================================================
def _data_off(f):
    """Offset of the first value/digit byte within ``f.data``."""
    if common.COB_FIELD_SIGN_SEPARATE(f) and common.COB_FIELD_SIGN_LEADING(f):
        return 1
    return 0


def store_common_region(f, data, size, scale):
    """Align/store the digits in *data* into DISPLAY field *f* (move.c L101-L132).

    *data* is a ``bytes``/``bytearray`` of zoned digit bytes; *size* is its
    length and *scale* its decimal scale.  The destination digits are filled
    with ``'0'`` then the overlapping (by decimal position) source digits are
    copied, mapping a space to ``'0'`` exactly as the C runtime does.
    """
    off = _data_off(f)
    fsize = common.COB_FIELD_SIZE(f)
    lf1 = -scale
    lf2 = -common.COB_FIELD_SCALE(f)
    hf1 = int(size) + lf1
    hf2 = int(fsize) + lf2
    lcf = max(lf1, lf2)
    gcf = min(hf1, hf2)
    # memset (COB_FIELD_DATA (f), '0', COB_FIELD_SIZE (f));
    zero = ord("0")
    for i in range(fsize):
        f.data[off + i] = zero
    if gcf > lcf:
        csize = gcf - lcf
        p = hf1 - gcf            # source index
        q = hf2 - gcf            # destination digit index
        for _ in range(csize):
            c = data[p]
            f.data[off + q] = zero if c == ord(" ") else c
            p += 1
            q += 1


# ===========================================================================
# Binary load/store (move.c cob_binary_mget_int64 / cob_binary_mset_int64).
#
# Reused from numeric.py so the byte-order / sign-extension logic lives in one
# place; numeric.cob_binary_get_int64 / cob_binary_set_int64 are the exact
# functional equivalents (signed two's-complement, COB_FLAG_BINARY_SWAP-aware).
# ===========================================================================
def cob_binary_mget_int64(f):
    """Load a (possibly swapped, signed) binary field as a Python int."""
    if common.COB_FIELD_HAVE_SIGN(f):
        return numeric.cob_binary_get_int64(f)
    return numeric.cob_binary_get_uint64(f)


def cob_binary_mset_int64(f, n):
    """Store integer *n* into the low ``f.size`` bytes of binary field *f*."""
    numeric.cob_binary_set_int64(f, n)


# ===========================================================================
# Display moves (move.c L210-L368)
# ===========================================================================
def cob_move_alphanum_to_display(f1, f2):
    """ALPHANUMERIC -> NUMERIC-DISPLAY (move.c L210-L284).

    Parses the (possibly signed, decimal-pointed) alphanumeric text in *f1* and
    right-justifies the integer digits into the DISPLAY receiver *f2*; a bad
    character zeroes the receiver (the C ``error`` path).
    """
    s1 = common.COB_FIELD_DATA(f1)
    size1 = common.COB_FIELD_SIZE(f1)
    e1 = size1
    sign = 0
    dp = _decimal_point()
    nsep = _numeric_separator()
    off2 = _data_off(f2)
    fsize2 = common.COB_FIELD_SIZE(f2)

    # Zero the receiver up front (so an early return / error leaves zeros).
    for i in range(fsize2):
        f2.data[off2 + i] = ord("0")

    # Leading sign / spaces (C scans for a leading + or -).
    i = 0
    while i < e1 and s1[i] == ord(" "):
        i += 1
    if i < e1 and (s1[i] == ord("+") or s1[i] == ord("-")):
        sign = -1 if s1[i] == ord("-") else 1
        i += 1

    # Count digits before the decimal point.
    count = 0
    p = i
    while p < e1 and s1[p] != dp:
        if ord("0") <= s1[p] <= ord("9"):
            count += 1
        p += 1

    # Find the start position in the receiver (right-justified integer part).
    size = fsize2 - common.COB_FIELD_SCALE(f2)
    dst = off2
    src = i
    if count < size:
        dst += size - count
    else:
        # Skip the surplus leading source digits.
        skip = count - size
        while skip > 0:
            while src < e1 and not (ord("0") <= s1[src] <= ord("9")):
                src += 1
            if src < e1:
                src += 1
            skip -= 1

    # Move digits until the receiver integer+fraction region is full.
    pcount = 0
    end_dst = off2 + fsize2
    bad = False
    while src < e1 and dst < end_dst:
        c = s1[src]
        if ord("0") <= c <= ord("9"):
            f2.data[dst] = c
            dst += 1
        elif c == dp:
            pcount += 1
            if pcount > 1:
                bad = True
                break
        elif not (c == ord(" ") or c == nsep):
            bad = True
            break
        src += 1

    if bad:
        for i in range(f2.size):
            f2.data[i] = ord("0")
        common.cob_put_sign(f2, 0)
        return
    common.cob_put_sign(f2, sign)


def cob_move_display_to_display(f1, f2):
    """NUMERIC-DISPLAY -> NUMERIC-DISPLAY (move.c L286-L297)."""
    sign = common.cob_get_sign(f1)
    store_common_region(f2, common.COB_FIELD_DATA(f1),
                        common.COB_FIELD_SIZE(f1), common.COB_FIELD_SCALE(f1))
    common.cob_put_sign(f1, sign)
    common.cob_put_sign(f2, sign)


def cob_move_display_to_alphanum(f1, f2):
    """NUMERIC-DISPLAY -> ALPHANUMERIC (move.c L299-L334)."""
    data1 = common.COB_FIELD_DATA(f1)
    size1 = common.COB_FIELD_SIZE(f1)
    sign = common.cob_get_sign(f1)
    size2 = f2.size
    if size1 >= size2:
        f2.data[:size2] = bytes(data1[:size2])
    else:
        diff = size2 - size1
        zero_size = 0
        f2.data[:size1] = bytes(data1[:size1])
        # implied 0 ('P's)
        if common.COB_FIELD_SCALE(f1) < 0:
            zero_size = min(-common.COB_FIELD_SCALE(f1), diff)
            for i in range(zero_size):
                f2.data[size1 + i] = ord("0")
        # padding
        if diff - zero_size > 0:
            for i in range(diff - zero_size):
                f2.data[size1 + zero_size + i] = ord(" ")
    common.cob_put_sign(f1, sign)


def cob_move_alphanum_to_alphanum(f1, f2):
    """ALPHANUMERIC -> ALPHANUMERIC (move.c L336-L368), honouring JUSTIFIED."""
    data1 = f1.data
    size1 = f1.size
    size2 = f2.size
    if size1 >= size2:
        if common.COB_FIELD_JUSTIFIED(f2):
            f2.data[:size2] = bytes(data1[size1 - size2:size1])
        else:
            f2.data[:size2] = bytes(data1[:size2])
    else:
        if common.COB_FIELD_JUSTIFIED(f2):
            for i in range(size2 - size1):
                f2.data[i] = ord(" ")
            f2.data[size2 - size1:size2] = bytes(data1[:size1])
        else:
            f2.data[:size1] = bytes(data1[:size1])
            for i in range(size1, size2):
                f2.data[i] = ord(" ")


# ===========================================================================
# Packed decimal moves (move.c L371-L451)
# ===========================================================================
def cob_move_display_to_packed(f1, f2):
    """NUMERIC-DISPLAY -> NUMERIC-PACKED / COMP-3 (move.c L371-L420)."""
    sign = common.cob_get_sign(f1)
    data1 = common.COB_FIELD_DATA(f1)
    digits1 = common.COB_FIELD_DIGITS(f1)
    scale1 = common.COB_FIELD_SCALE(f1)
    digits2 = common.COB_FIELD_DIGITS(f2)
    scale2 = common.COB_FIELD_SCALE(f2)

    # pack string
    for i in range(f2.size):
        f2.data[i] = 0
    offset = 1 - (digits2 % 2)
    # p walks the (virtual) source digit positions; indices outside [0,digits1)
    # contribute 0 (the C ``data1 <= p && p < data1 + digits1`` guard).
    base = (digits1 - scale1) - (digits2 - scale2)
    for i in range(offset, digits2 + offset):
        pidx = base + (i - offset)
        if 0 <= pidx < digits1:
            c = data1[pidx]
            n = 0 if c == ord(" ") else common.cob_d2i(c)
        else:
            n = 0
        if i % 2 == 0:
            f2.data[i // 2] = (n << 4) & 0xFF
        else:
            f2.data[i // 2] |= n
    common.cob_put_sign(f1, sign)

    last = f2.size - 1
    if not common.COB_FIELD_HAVE_SIGN(f2):
        f2.data[last] = (f2.data[last] & 0xF0) | 0x0F
    elif sign < 0:
        f2.data[last] = (f2.data[last] & 0xF0) | 0x0D
    else:
        f2.data[last] = (f2.data[last] & 0xF0) | 0x0C


def cob_move_packed_to_display(f1, f2):
    """NUMERIC-PACKED / COMP-3 -> NUMERIC-DISPLAY (move.c L422-L451)."""
    data = f1.data
    sign = common.cob_get_sign(f1)
    digits1 = common.COB_FIELD_DIGITS(f1)
    offset = 1 - (digits1 % 2)
    buff = bytearray(digits1)
    for i in range(offset, digits1 + offset):
        if i % 2 == 0:
            buff[i - offset] = common.cob_i2d(data[i // 2] >> 4)
        else:
            buff[i - offset] = common.cob_i2d(data[i // 2] & 0x0F)
    store_common_region(f2, buff, digits1, common.COB_FIELD_SCALE(f1))
    common.cob_put_sign(f2, sign)


# ===========================================================================
# Floating-point moves (move.c L453-L538) - COMP-1 / COMP-2
# ===========================================================================
def cob_move_display_to_fp(f1, f2):
    """NUMERIC-DISPLAY -> COMP-1 / COMP-2 (move.c L453-L485)."""
    sign = common.cob_get_sign(f1)
    size1 = common.COB_FIELD_SIZE(f1)
    data1 = common.COB_FIELD_DATA(f1)
    scale = common.COB_FIELD_SCALE(f1)
    size = size1 - scale
    intpart = bytes(data1[:size]) if size > 0 else b""
    if scale <= 0:
        text = "%s.0" % intpart.decode("latin-1")
    else:
        text = "%s.%s" % (intpart.decode("latin-1"),
                          bytes(data1[size:size + scale]).decode("latin-1"))
    try:
        val = float(text)
    except ValueError:
        val = 0.0
    if sign < 0:
        val = -val
    common.cob_put_sign(f1, sign)
    if common.COB_FIELD_TYPE(f2) == common.COB_TYPE_NUMERIC_FLOAT:
        f2.data[:4] = struct.pack("=f", val)
    else:
        f2.data[:8] = struct.pack("=d", val)


def cob_move_fp_to_display(f1, f2):
    """COMP-1 / COMP-2 -> NUMERIC-DISPLAY (move.c L487-L538)."""
    if common.COB_FIELD_TYPE(f1) == common.COB_TYPE_NUMERIC_FLOAT:
        val = struct.unpack("=f", bytes(f1.data[:4]))[0]
    else:
        val = struct.unpack("=d", bytes(f1.data[:8]))[0]
    sign = 1
    if val < 0:
        sign = -1
        val = -val
    intgr = int(val)
    decs = 0
    res = intgr
    while res:
        decs += 1
        res //= 10
    # snprintf (buff2, 63, "%-18.*lf", 18 - decs, val): fixed-point, 18-decs
    # fractional digits, left-justified in an 18-wide field.
    prec = 18 - decs
    if prec < 0:
        prec = 0
    formatted = "%-18.*f" % (prec, val)
    # Strip '.' and ' ' (the C compaction loop).
    buff = "".join(ch for ch in formatted if ch != "." and ch != " ")
    store_common_region(f2, buff.encode("latin-1"), len(buff), 18 - decs)
    common.cob_put_sign(f2, sign)


# ===========================================================================
# Binary integer moves (move.c L541-L614)
# ===========================================================================
def cob_move_display_to_binary(f1, f2):
    """NUMERIC-DISPLAY -> NUMERIC-BINARY / COMP (move.c L541-L574)."""
    size1 = common.COB_FIELD_SIZE(f1)
    data1 = common.COB_FIELD_DATA(f1)
    sign = common.cob_get_sign(f1)
    val = 0
    size = size1 - common.COB_FIELD_SCALE(f1) + common.COB_FIELD_SCALE(f2)
    for i in range(size):
        if i < size1:
            val = val * 10 + common.cob_d2i(data1[i])
        else:
            val = val * 10
    if sign < 0 and common.COB_FIELD_HAVE_SIGN(f2):
        val = -val
    if _flag_binary_truncate() and not common.COB_FIELD_REAL_BINARY(f2):
        digits2 = common.COB_FIELD_DIGITS(f2)
        modulus = _exp10(digits2)
        # C ``%`` is truncation toward zero on a signed long long.
        val = int(math_fmod_ll(val, modulus))
    cob_binary_mset_int64(f2, val)
    common.cob_put_sign(f1, sign)


def math_fmod_ll(val, modulus):
    """Truncating modulo matching C ``long long %`` (sign follows dividend)."""
    if modulus == 0:
        return val
    r = abs(val) % modulus
    return -r if val < 0 else r


def cob_move_binary_to_display(f1, f2):
    """NUMERIC-BINARY / COMP -> NUMERIC-DISPLAY (move.c L576-L614)."""
    sign = 1
    if common.COB_FIELD_HAVE_SIGN(f1):
        val2 = numeric.cob_binary_get_int64(f1)
        if val2 < 0:
            sign = -1
            val = -val2
        else:
            val = val2
    else:
        val = numeric.cob_binary_get_uint64(f1)
    # Convert to string (right-justified into a 20-wide buffer like the C code).
    buff = bytearray(b" " * 20)
    i = 20
    while val > 0:
        i -= 1
        buff[i] = common.cob_i2d(val % 10)
        val //= 10
    store_common_region(f2, buff[i:20], 20 - i, common.COB_FIELD_SCALE(f1))
    common.cob_put_sign(f2, sign)



# ===========================================================================
# Edited moves (move.c L616-L963)
#
# The PICTURE string is a sequence of 5-byte groups emitted by the front-end
# and rendered by codegen.c as a Python ``bytes`` literal: 1 symbol byte
# followed by a 4-byte NATIVE int32 repeat count (the C
# ``memcpy(&n, p+1, sizeof(int))``).  ``_iter_pic`` decodes that wire format.
# ===========================================================================
def _iter_pic(pic):
    """Yield ``(symbol_byte, repeat_count)`` over a 5-byte-group PIC ``bytes``."""
    if not pic:
        return
    n = len(pic)
    i = 0
    while i + 5 <= n:
        sym = pic[i]
        if sym == 0:
            break
        count = struct.unpack_from("=i", pic, i + 1)[0]
        yield sym, count
        i += 5


def cob_move_display_to_edited(f1, f2):
    """NUMERIC-DISPLAY -> NUMERIC-EDITED (move.c L616-L848)."""
    decimal_point = -1
    sign = common.cob_get_sign(f1)
    neg = 1 if sign < 0 else 0
    dp = _decimal_point()
    cur = _currency_symbol()

    count = 0
    count_sign = 1
    count_curr = 1
    trailing_sign = 0
    trailing_curr = 0
    is_zero = 1
    suppress_zero = 1
    sign_first = 0
    p_is_left = 0
    pad = ord(" ")
    sign_symbol = 0
    curr_symbol = 0

    pic = common.COB_FIELD_PIC(f2)

    # Count the number of digit places before the decimal point.
    for c, repeat in _iter_pic(pic):
        if c in (ord("9"), ord("Z"), ord("*")):
            count += repeat
            count_sign = 0
            count_curr = 0
        elif count_curr and c == cur:
            count += repeat
        elif count_sign and (c == ord("+") or c == ord("-")):
            count += repeat
        elif c == ord("P"):
            if count == 0:
                p_is_left = 1
                break
            else:
                count += repeat
                count_sign = 0
                count_curr = 0
        elif c == ord("V") or c == dp:
            break

    src_data = common.COB_FIELD_DATA(f1)
    size1 = common.COB_FIELD_SIZE(f1)
    min_i = 0
    max_i = size1
    src = max_i - common.COB_FIELD_SCALE(f1) - count
    dst = 0
    end = f2.size

    def _read_src(src):
        if min_i <= src < max_i:
            return src_data[src], src + 1
        return ord("0"), src + 1

    for c, n in _iter_pic(pic):
        while n > 0:
            n -= 1
            if c == ord("0") or c == ord("/"):
                f2.data[dst] = c
                dst += 1
            elif c == ord("B"):
                f2.data[dst] = pad if suppress_zero else ord("B")
                dst += 1
            elif c == ord("P"):
                if p_is_left:
                    src += 1
                    dst -= 1
                dst += 1
            elif c == ord("9"):
                x, src = _read_src(src)
                f2.data[dst] = x
                if x != ord("0"):
                    is_zero = suppress_zero = 0
                suppress_zero = 0
                trailing_sign = 1
                trailing_curr = 1
                dst += 1
            elif c == ord("V"):
                dst -= 1
                decimal_point = dst
                dst += 1
            elif c == ord(".") or c == ord(","):
                if c == dp:
                    f2.data[dst] = dp
                    decimal_point = dst
                else:
                    f2.data[dst] = pad if suppress_zero else c
                dst += 1
            elif c == ord("C") or c == ord("D"):
                end = dst
                pair = (b"CR" if c == ord("C") else b"DB") if neg else b"  "
                f2.data[dst] = pair[0]
                f2.data[dst + 1] = pair[1]
                dst += 2
            elif c == ord("Z") or c == ord("*"):
                x, src = _read_src(src)
                if x != ord("0"):
                    is_zero = suppress_zero = 0
                pad = ord("*") if c == ord("*") else ord(" ")
                f2.data[dst] = pad if suppress_zero else x
                trailing_sign = 1
                trailing_curr = 1
                dst += 1
            elif c == ord("+") or c == ord("-"):
                x, src = _read_src(src)
                if x != ord("0"):
                    is_zero = suppress_zero = 0
                if trailing_sign:
                    f2.data[dst] = ord("-") if neg else (ord("+") if c == ord("+") else ord(" "))
                    end -= 1
                elif dst == 0 or suppress_zero:
                    f2.data[dst] = pad
                    sign_symbol = ord("-") if neg else (ord("+") if c == ord("+") else ord(" "))
                    if not curr_symbol:
                        sign_first += 1
                else:
                    f2.data[dst] = x
                dst += 1
            else:
                if c == cur:
                    x, src = _read_src(src)
                    if x != ord("0"):
                        is_zero = suppress_zero = 0
                    if trailing_curr:
                        f2.data[dst] = cur
                        end -= 1
                    elif dst == 0 or suppress_zero:
                        f2.data[dst] = pad
                        curr_symbol = cur
                    else:
                        f2.data[dst] = x
                    dst += 1
                else:
                    f2.data[dst] = ord("?")    # invalid PIC
                    dst += 1

    if suppress_zero or (is_zero and common.COB_FIELD_BLANK_ZERO(f2)):
        # all digits are zeros
        if pad == ord(" ") or common.COB_FIELD_BLANK_ZERO(f2):
            for i in range(f2.size):
                f2.data[i] = ord(" ")
        else:
            for i in range(f2.size):
                if f2.data[i] != dp:
                    f2.data[i] = pad
    else:
        # put zero after the decimal point if necessary
        if decimal_point >= 0:
            extra = set(b",+-/B")
            for d in range(decimal_point + 1, end):
                ch = f2.data[d]
                if not (ord("0") <= ch <= ord("9")) and ch not in extra:
                    f2.data[d] = ord("0")
        # put sign or currency symbol at the beginning
        if sign_symbol or curr_symbol:
            d = end - 1
            while d > 0:
                if f2.data[d] == ord(" "):
                    break
                d -= 1
            if sign_symbol and curr_symbol:
                if sign_first:
                    f2.data[d] = curr_symbol
                    d -= 1
                    if d >= 0:
                        f2.data[d] = sign_symbol
                else:
                    f2.data[d] = sign_symbol
                    d -= 1
                    if d >= 0:
                        f2.data[d] = curr_symbol
            elif sign_symbol:
                f2.data[d] = sign_symbol
            else:
                f2.data[d] = curr_symbol
        # replace all 'B's by pad
        cnt = 0
        for d in range(end):
            if f2.data[d] == ord("B"):
                f2.data[d] = pad if cnt == 0 else ord(" ")
            else:
                cnt += 1
    common.cob_put_sign(f1, sign)


def cob_move_edited_to_display(f1, f2):
    """NUMERIC-EDITED -> NUMERIC-DISPLAY (move.c L850-L925)."""
    buff = bytearray()
    sign = 0
    scale = 0
    count = 0
    have_point = 0
    dp = _decimal_point()
    for i in range(f1.size):
        cp = f1.data[i]
        if ord("0") <= cp <= ord("9"):
            buff.append(cp)
            if have_point:
                scale += 1
        elif cp == ord(".") or cp == ord(","):
            if cp == dp:
                have_point = 1
        elif cp == ord("-") or cp == ord("C"):
            sign = -1
    # Count digit places after the decimal point for 'V'/'P'.
    if scale == 0:
        for c, n in _iter_pic(common.COB_FIELD_PIC(f1)):
            if c in (ord("9"), ord("0"), ord("Z"), ord("*")):
                if have_point:
                    scale += n
                else:
                    count += n
            elif c == ord("P"):
                if count == 0:
                    have_point = 1
                    scale += n
                else:
                    scale -= n
            elif c == ord("V"):
                have_point = 1
    store_common_region(f2, buff, len(buff), scale)
    common.cob_put_sign(f2, sign)


def cob_move_alphanum_to_edited(f1, f2):
    """ALPHANUMERIC -> ALPHANUMERIC-EDITED (move.c L927-L967)."""
    sign = common.cob_get_sign(f1)
    src_data = common.COB_FIELD_DATA(f1)
    size1 = common.COB_FIELD_SIZE(f1)
    src = 0
    dst = 0
    for c, n in _iter_pic(common.COB_FIELD_PIC(f2)):
        while n > 0:
            n -= 1
            if c in (ord("A"), ord("X"), ord("9")):
                if src < size1:
                    f2.data[dst] = src_data[src]
                    src += 1
                else:
                    f2.data[dst] = ord(" ")
                dst += 1
            elif c == ord("0") or c == ord("/"):
                f2.data[dst] = c
                dst += 1
            elif c == ord("B"):
                f2.data[dst] = ord(" ")
                dst += 1
            else:
                f2.data[dst] = ord("?")    # invalid PIC
                dst += 1
    common.cob_put_sign(f1, sign)



# ===========================================================================
# MOVE dispatcher (move.c L969-L1163)
# ===========================================================================
def _new_display_temp(size, scale):
    """Build the scratch NUMERIC-DISPLAY field used by indirect_move."""
    attr = common.cob_field_attr(
        type=common.COB_TYPE_NUMERIC_DISPLAY, digits=size, scale=scale,
        flags=common.COB_FLAG_HAVE_SIGN, pic=None)
    return common.cob_field(size=size, data=bytearray(size), attr=attr)


def indirect_move(func, src, dst, size, scale):
    """Convert *src* via *func* into a DISPLAY temp, then MOVE it to *dst*.

    Port of move.c L969-L984: the bridge used whenever a direct converter for
    the (src-type, dst-type) pair does not exist.
    """
    temp = _new_display_temp(size, scale)
    func(src, temp)
    cob_move(temp, dst)


def cob_move_all(src, dst):
    """Figurative ALPHANUMERIC-ALL fill -> *dst* (move.c L986-L1026)."""
    if common.COB_FIELD_IS_NUMERIC(dst):
        digcount = 18
        attr = common.cob_field_attr(type=common.COB_TYPE_NUMERIC_DISPLAY,
                                     digits=18, scale=0, flags=0, pic=None)
    else:
        digcount = dst.size
        attr = common.cob_field_attr(type=common.COB_TYPE_ALPHANUMERIC,
                                     digits=0, scale=0, flags=0, pic=None)
    data = bytearray(digcount)
    if src.size == 1:
        fill = src.data[0]
        for i in range(digcount):
            data[i] = fill
    else:
        for i in range(digcount):
            data[i] = src.data[i % src.size]
    temp = common.cob_field(size=digcount, data=data, attr=attr)
    cob_move(temp, dst)


def cob_move(src, dst):
    """The MOVE statement runtime entry point (move.c L1028-L1163).

    Dispatches on the (source USAGE, destination USAGE) pair to the matching
    converter, reproducing the C switch byte-for-byte.
    """
    if common.COB_FIELD_TYPE(src) == common.COB_TYPE_ALPHANUMERIC_ALL:
        cob_move_all(src, dst)
        return
    if dst.size == 0:
        return
    if src.size == 0:
        src = common.cob_space

    stype = common.COB_FIELD_TYPE(src)
    dtype = common.COB_FIELD_TYPE(dst)

    # non-elementary move
    if stype == common.COB_TYPE_GROUP or dtype == common.COB_TYPE_GROUP:
        cob_move_alphanum_to_alphanum(src, dst)
        return

    T = common
    if stype == T.COB_TYPE_NUMERIC_DISPLAY:
        if dtype in (T.COB_TYPE_NUMERIC_FLOAT, T.COB_TYPE_NUMERIC_DOUBLE):
            cob_move_display_to_fp(src, dst)
        elif dtype == T.COB_TYPE_NUMERIC_DISPLAY:
            cob_move_display_to_display(src, dst)
        elif dtype == T.COB_TYPE_NUMERIC_PACKED:
            cob_move_display_to_packed(src, dst)
        elif dtype == T.COB_TYPE_NUMERIC_BINARY:
            cob_move_display_to_binary(src, dst)
        elif dtype == T.COB_TYPE_NUMERIC_EDITED:
            cob_move_display_to_edited(src, dst)
        elif dtype == T.COB_TYPE_ALPHANUMERIC_EDITED:
            sc = T.COB_FIELD_SCALE(src)
            if sc < 0 or sc > T.COB_FIELD_DIGITS(src):
                indirect_move(cob_move_display_to_display, src, dst,
                              max(T.COB_FIELD_DIGITS(src), T.COB_FIELD_SCALE(src)),
                              max(0, T.COB_FIELD_SCALE(src)))
            else:
                cob_move_alphanum_to_edited(src, dst)
        else:
            cob_move_display_to_alphanum(src, dst)
        return

    if stype == T.COB_TYPE_NUMERIC_PACKED:
        if dtype == T.COB_TYPE_NUMERIC_DISPLAY:
            cob_move_packed_to_display(src, dst)
        else:
            indirect_move(cob_move_packed_to_display, src, dst,
                          T.COB_FIELD_DIGITS(src), T.COB_FIELD_SCALE(src))
        return

    if stype == T.COB_TYPE_NUMERIC_BINARY:
        if dtype == T.COB_TYPE_NUMERIC_DISPLAY:
            cob_move_binary_to_display(src, dst)
        elif dtype in (T.COB_TYPE_NUMERIC_BINARY, T.COB_TYPE_NUMERIC_PACKED,
                       T.COB_TYPE_NUMERIC_EDITED, T.COB_TYPE_NUMERIC_FLOAT,
                       T.COB_TYPE_NUMERIC_DOUBLE):
            indirect_move(cob_move_binary_to_display, src, dst, 20,
                          T.COB_FIELD_SCALE(src))
        else:
            indirect_move(cob_move_binary_to_display, src, dst,
                          T.COB_FIELD_DIGITS(src), T.COB_FIELD_SCALE(src))
        return

    if stype == T.COB_TYPE_NUMERIC_EDITED:
        if dtype == T.COB_TYPE_NUMERIC_DISPLAY:
            cob_move_edited_to_display(src, dst)
        elif dtype in (T.COB_TYPE_NUMERIC_PACKED, T.COB_TYPE_NUMERIC_BINARY,
                       T.COB_TYPE_NUMERIC_EDITED, T.COB_TYPE_NUMERIC_FLOAT,
                       T.COB_TYPE_NUMERIC_DOUBLE):
            indirect_move(cob_move_edited_to_display, src, dst, 36, 18)
        elif dtype == T.COB_TYPE_ALPHANUMERIC_EDITED:
            cob_move_alphanum_to_edited(src, dst)
        else:
            cob_move_alphanum_to_alphanum(src, dst)
        return

    if stype in (T.COB_TYPE_NUMERIC_FLOAT, T.COB_TYPE_NUMERIC_DOUBLE):
        indirect_move(cob_move_fp_to_display, src, dst, 40, 20)
        return

    # default: source is alphanumeric / group-as-alphanumeric
    if dtype == T.COB_TYPE_NUMERIC_DISPLAY:
        cob_move_alphanum_to_display(src, dst)
    elif dtype in (T.COB_TYPE_NUMERIC_PACKED, T.COB_TYPE_NUMERIC_BINARY,
                   T.COB_TYPE_NUMERIC_EDITED, T.COB_TYPE_NUMERIC_FLOAT,
                   T.COB_TYPE_NUMERIC_DOUBLE):
        indirect_move(cob_move_alphanum_to_display, src, dst, 36, 18)
    elif dtype == T.COB_TYPE_ALPHANUMERIC_EDITED:
        cob_move_alphanum_to_edited(src, dst)
    else:
        cob_move_alphanum_to_alphanum(src, dst)


# ===========================================================================
# Convenience integer accessors (move.c L1166-L1357)
# ===========================================================================
def _to_c_int(n):
    """Wrap *n* to a signed 32-bit value (C ``(int)`` cast semantics)."""
    n &= 0xFFFFFFFF
    return n - 0x100000000 if n >= 0x80000000 else n


def _to_c_longlong(n):
    """Wrap *n* to a signed 64-bit value (C ``(long long)`` cast semantics)."""
    n &= 0xFFFFFFFFFFFFFFFF
    return n - 0x10000000000000000 if n >= 0x8000000000000000 else n


def cob_packed_get_int(f1):
    """COMP-3 -> int (move.c L1166-L1190)."""
    data = f1.data
    sign = common.cob_get_sign(f1)
    digits = common.COB_FIELD_DIGITS(f1)
    scale = common.COB_FIELD_SCALE(f1)
    offset = 1 - (digits % 2)
    val = 0
    for i in range(offset, digits - scale + offset):
        val *= 10
        if i % 2 == 0:
            val += data[i // 2] >> 4
        else:
            val += data[i // 2] & 0x0F
    if sign < 0:
        val = -val
    return val


def cob_packed_get_long_long(f1):
    """COMP-3 -> long long (move.c L1192-L1216); identical maths in Python."""
    return cob_packed_get_int(f1)


def cob_display_get_int(f):
    """NUMERIC-DISPLAY -> int (move.c L1218-L1255)."""
    size = common.COB_FIELD_SIZE(f)
    data = common.COB_FIELD_DATA(f)
    sign = common.cob_get_sign(f)
    scale = common.COB_FIELD_SCALE(f)
    # skip preceding zeros (advance i to the first non-zero digit)
    i = 0
    while i < size and common.cob_d2i(data[i]) == 0:
        i += 1
    val = 0
    if scale < 0:
        while i < size:
            val = val * 10 + common.cob_d2i(data[i])
            i += 1
        val *= _exp10(-scale)
    else:
        upper = size - scale
        while i < upper:
            val = val * 10 + common.cob_d2i(data[i])
            i += 1
    if sign < 0:
        val = -val
    common.cob_put_sign(f, sign)
    return val


def cob_display_get_long_long(f):
    """NUMERIC-DISPLAY -> long long (move.c L1257-L1294); identical in Python."""
    return cob_display_get_int(f)


def cob_set_int(f, n):
    """Store the C ``int`` *n* into field *f* (move.c L1296-L1307).

    Builds a native 4-byte signed binary temporary (no byte-swap, HAVE_SIGN)
    holding *n* and MOVEs it into *f*, exactly as the C runtime does.
    """
    n = _to_c_int(n)
    attr = common.cob_field_attr(type=common.COB_TYPE_NUMERIC_BINARY,
                                 digits=9, scale=0,
                                 flags=common.COB_FLAG_HAVE_SIGN, pic=None)
    temp = common.cob_field(size=4, data=bytearray(struct.pack("=i", n)),
                            attr=attr)
    cob_move(temp, f)


def cob_set_pointer(f, val):
    """Store pointer *val* into POINTER item *f* (the CALL RETURNING-pointer path).

    The emitter (codegen.c output_call) lowers ``CALL ... RETURNING ptr`` to
    ``move.cob_set_pointer(ptr, _unifunc(...))``, choosing the ``move`` namespace
    for symmetry with the ``move.cob_set_int`` return-code path.  Pointer storage
    is owned by :mod:`libcob_py.common` (the single raw-address-bytes model
    shared with ``cob_get_pointer`` / ``cob_pointer_manip``), so this delegates
    there to keep one authoritative implementation.  Returns *f*.
    """
    return common.cob_set_pointer(f, val)


def cob_get_int(f):
    """Return field *f* as a C ``int`` (move.c L1309-L1332)."""
    ftype = common.COB_FIELD_TYPE(f)
    if ftype == common.COB_TYPE_NUMERIC_DISPLAY:
        return cob_display_get_int(f)
    if ftype == common.COB_TYPE_NUMERIC_BINARY:
        return _to_c_int(cob_binary_mget_int64(f))
    if ftype == common.COB_TYPE_NUMERIC_PACKED:
        return cob_packed_get_int(f)
    attr = common.cob_field_attr(type=common.COB_TYPE_NUMERIC_BINARY,
                                 digits=9, scale=0,
                                 flags=common.COB_FLAG_HAVE_SIGN, pic=None)
    temp = common.cob_field(size=4, data=bytearray(4), attr=attr)
    cob_move(f, temp)
    return struct.unpack("=i", bytes(temp.data[:4]))[0]


def cob_get_long_long(f):
    """Return field *f* as a C ``long long`` (move.c L1334-L1357)."""
    ftype = common.COB_FIELD_TYPE(f)
    if ftype == common.COB_TYPE_NUMERIC_DISPLAY:
        return cob_display_get_long_long(f)
    if ftype == common.COB_TYPE_NUMERIC_BINARY:
        return _to_c_longlong(cob_binary_mget_int64(f))
    if ftype == common.COB_TYPE_NUMERIC_PACKED:
        return cob_packed_get_long_long(f)
    attr = common.cob_field_attr(type=common.COB_TYPE_NUMERIC_BINARY,
                                 digits=18, scale=0,
                                 flags=common.COB_FLAG_HAVE_SIGN, pic=None)
    temp = common.cob_field(size=8, data=bytearray(8), attr=attr)
    cob_move(f, temp)
    return struct.unpack("=q", bytes(temp.data[:8]))[0]


# ===========================================================================
# Runtime initialisation (move.c L1359-L1363)
# ===========================================================================
def cob_init_move():
    """Initialise the MOVE subsystem (move.c L1359-L1363).

    The C version pre-allocates the ``lastdata`` scratch buffer used by
    ``cob_move_all``; in Python that buffer is allocated on demand, so this is
    the explicit, no-side-effect initialiser kept for the cob_init_* ordering
    contract reproduced by ``common._run_subsystem_initializers``.
    """
    return None

