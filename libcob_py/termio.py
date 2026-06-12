"""libcob_py.termio - basic non-curses ACCEPT / DISPLAY fallback.

Pure-Python port of the C runtime module ``libcob/termio.c`` (the
GNU Cobol / OpenCOBOL terminal-I/O fallback).  It implements the *plain*
stream-based ``ACCEPT`` / ``DISPLAY`` path used whenever the full
SCREEN SECTION (curses) subsystem is not active.  In the rewritten
C->Python backend this module is also the **graceful-degradation target**
for :mod:`libcob_py.screenio`: when ``curses.initscr()`` fails, ``screenio``
delegates to the plain ``cob_display`` / ``cob_accept`` implemented here
(AAP sections 0.3.4 / 0.6.5).

Authority / source
------------------
Behaviour mirrors ``libcob/termio.c`` line-for-line:

* ``display_numeric``        -> :func:`_display_numeric`        (termio.c L48-L81)
* ``pretty_display_numeric`` -> :func:`_pretty_display_numeric` (termio.c L83-L148)
* ``display_alnum``          -> :func:`_display_alnum`          (termio.c L150-L158)
* ``display``                -> :func:`_format_field`           (termio.c L160-L206)
* ``cob_display``            -> :func:`cob_display`             (termio.c L208-L231)
* ``cob_accept``             -> :func:`cob_accept`              (termio.c L237-L280)
* ``cob_init_termio``        -> :func:`cob_init_termio`         (termio.c L282-L286)

Dependencies
------------
**Standard library only** - this module imports :mod:`sys` and :mod:`struct`
and nothing else from the platform; its only intra-package dependency is
:mod:`libcob_py.common` (AAP sections 0.5 / 0.7.1).  It introduces ZERO
third-party packages.  The data-movement runtime (``libcob_py.move``) is
reached indirectly through :func:`libcob_py.common._lazy_move`, so ``termio``
keeps ``common`` as its single sibling import and never imports ``move`` (or
``numeric`` / ``screenio``) directly.

The ``cob_display`` / ``cob_accept`` / ``cob_init_termio`` names and the
``cob_display(to_stderr, newline, varcnt, *fields)`` argument order are the
emitter->runtime contract: the rewritten ``cobc/codegen.c`` routes every
emitted ``cob_display`` / ``cob_accept`` call-site to this module
(AAP section 0.6.5 - exhaustive ``cob_* -> libcob_py.<module>.<fn>`` mapping).
"""

from __future__ import annotations

import struct
import sys

from libcob_py import common

# ---------------------------------------------------------------------------
# Module constants and file-scope state (termio.c L41-L42).
# ---------------------------------------------------------------------------

#: Number of decimal digits represented by a COBOL ``BINARY`` item of a given
#: byte size.  Indexed by the field's byte count, exactly as the C
#: ``bin_digits[]`` table (termio.c L42): a 1-byte BINARY shows 3 digits, a
#: 2-byte 5, a 4-byte 10, an 8-byte 20, ...
_BIN_DIGITS = (1, 3, 5, 8, 10, 13, 15, 17, 20)

#: Working buffer allocated by :func:`cob_init_termio`, mirroring the C
#: ``term_buff`` static (termio.c L41).  Python reads input lines dynamically
#: via :func:`sys.stdin.readline`, so this buffer is retained only for parity
#: with the C ``fgets`` bound (it sizes the ACCEPT read cap - see
#: :func:`cob_accept`).
_term_buff = None


# ---------------------------------------------------------------------------
# Module / formatting context helpers.
#
# The C runtime dereferences ``cob_current_module`` directly; here every
# access is guarded so the fallback path also works before a module has been
# entered (e.g. during isolated unit testing).  The guarded defaults match the
# ``cob_module`` constructor defaults in ``common`` ('.' decimal point, pretty
# display enabled).
# ---------------------------------------------------------------------------

def _decimal_point():
    """Return the active module's decimal-point byte (default ``ord('.')``)."""
    module = common.cob_current_module
    if module is not None:
        return module.decimal_point
    return ord(".")


def _pretty():
    """Return the active module's ``flag_pretty_display`` (default enabled)."""
    module = common.cob_current_module
    if module is not None:
        return module.flag_pretty_display
    return 1


def _screen_initialized():
    """True when the curses SCREEN subsystem is active.

    ``cob_screen_initialized`` is owned by :mod:`libcob_py.screenio`; it is
    referenced defensively here (``getattr`` with a ``False`` default) so that
    ``termio`` - the plain fallback - has no static import dependency on
    ``screenio`` and behaves as a pure stream path when the screen subsystem
    is absent (termio.c L216, L246).
    """
    return bool(getattr(common, "cob_screen_initialized", False))


# ---------------------------------------------------------------------------
# Low-level output emission.
#
# The C runtime writes raw bytes with ``putc`` / ``fprintf`` to a ``FILE *``.
# Here we write to the stream's binary ``buffer`` when one exists (the real
# ``sys.stdout`` / ``sys.stderr``) so the exact byte sequence reaches the
# terminal; for text-only sinks (e.g. an ``io.StringIO`` used in tests) we
# decode Latin-1 1:1 (COBOL data is single-byte) and write text.
# ---------------------------------------------------------------------------

def _emit(stream, data):
    """Write *data* (``bytes``) to *stream*, honouring binary vs text sinks."""
    sink = getattr(stream, "buffer", None)
    if sink is not None:
        sink.write(data)
    else:
        stream.write(data.decode("latin-1"))


def _flush(stream):
    """Flush *stream* (its binary ``buffer`` when present)."""
    sink = getattr(stream, "buffer", None)
    (sink if sink is not None else stream).flush()


# ---------------------------------------------------------------------------
# DISPLAY - per-field formatters (termio.c L48-L206).
#
# Each helper returns the field's display ``bytes`` (the C code wrote directly
# to the FILE*; returning bytes lets :func:`cob_display` assemble the whole
# record and emit it once, which produces an identical byte stream).
# ---------------------------------------------------------------------------

def _display_numeric(f):
    """Plain numeric display - zoned digits with a SEPARATE sign (termio.c L48-L81).

    Mirrors the C ``display_numeric``: build a ``NUMERIC_DISPLAY`` temporary
    sized ``digits (+1 for the sign)``, MOVE the source value into it, then
    emit every byte.  The temporary carries ``HAVE_SIGN | SIGN_SEPARATE`` for a
    signed source, additionally ``SIGN_LEADING`` when the source is itself
    sign-leading or a ``BINARY`` item.  No decimal point is inserted in the
    plain (non-pretty) path - exactly as the C runtime.
    """
    if f.size == 0:
        return b""

    digits = common.COB_FIELD_DIGITS(f)
    scale = common.COB_FIELD_SCALE(f)
    have_sign = bool(common.COB_FIELD_HAVE_SIGN(f))
    size = digits + (1 if have_sign else 0)

    flags = 0
    if have_sign:
        flags = common.COB_FLAG_HAVE_SIGN | common.COB_FLAG_SIGN_SEPARATE
        if (common.COB_FIELD_SIGN_LEADING(f)
                or common.COB_FIELD_TYPE(f) == common.COB_TYPE_NUMERIC_BINARY):
            flags |= common.COB_FLAG_SIGN_LEADING

    attr = common.cob_field_attr(
        type=common.COB_TYPE_NUMERIC_DISPLAY, digits=digits, scale=scale,
        flags=flags, pic=None)
    temp = common.cob_field(size=size, data=bytearray(size), attr=attr)

    # Value decode (USAGE -> zoned digits + separate sign) is delegated to the
    # data-movement runtime via the deferred ``common._lazy_move`` accessor,
    # keeping ``common`` as this module's only sibling import.
    common._lazy_move(f, temp)
    return bytes(temp.data[:size])


def _pretty_display_numeric(f):
    """Pretty numeric display - fixed sign + decimal point (termio.c L83-L148).

    The C ``pretty_display_numeric`` MOVEs the value into a ``NUMERIC_EDITED``
    temporary whose PICTURE is ``[+] 9..9 [decimal-point] 9..9``.  The editing
    PICTURE format is interpreted by the data-movement runtime; to avoid
    coupling to that (yet-unestablished) edited-PICTURE contract, this port
    instead MOVEs into a ``NUMERIC_DISPLAY`` temporary (the unambiguous, core
    MOVE conversion) and performs the trivial edited *layout* here: a fixed
    leading sign ('+' / '-') followed by the zero-filled integer digits, the
    module decimal-point, and the zero-filled fraction digits.  This reproduces
    the C edited output byte-for-byte (minimal-deviation rule, AAP section
    0.7.2) while keeping ``move`` reached only through ``common._lazy_move``.
    """
    if f.size == 0:
        return b""

    digits = common.COB_FIELD_DIGITS(f)
    scale = common.COB_FIELD_SCALE(f)
    have_sign = bool(common.COB_FIELD_HAVE_SIGN(f))

    # MOVE into a NUMERIC_DISPLAY temp with a SEPARATE LEADING sign so the
    # result is laid out as ``[sign][zoned-digits]`` regardless of the source's
    # own sign placement/USAGE.
    if have_sign:
        flags = (common.COB_FLAG_HAVE_SIGN
                 | common.COB_FLAG_SIGN_SEPARATE
                 | common.COB_FLAG_SIGN_LEADING)
        temp_size = digits + 1
    else:
        flags = 0
        temp_size = digits

    attr = common.cob_field_attr(
        type=common.COB_TYPE_NUMERIC_DISPLAY, digits=digits, scale=scale,
        flags=flags, pic=None)
    temp = common.cob_field(size=temp_size, data=bytearray(temp_size), attr=attr)
    common._lazy_move(f, temp)

    data = temp.data
    if have_sign:
        sign = data[0:1]          # '+' / '-' produced by the SIGN_SEPARATE MOVE
        zoned = data[1:1 + digits]
    else:
        sign = b""
        zoned = data[:digits]

    out = bytearray(sign)
    if scale > 0:
        intlen = digits - scale
        out += zoned[:intlen]
        out.append(_decimal_point())
        out += zoned[intlen:digits]
    else:
        # scale <= 0 (including P-scaling): no decimal point, like the C
        # ``else`` PICTURE branch of pretty_display_numeric.
        out += zoned[:digits]
    return bytes(out)


def _display_alnum(f):
    """Alphanumeric display - emit the field bytes verbatim (termio.c L150-L158)."""
    if f.data is None:
        return b""
    return bytes(f.data[:f.size])


def _display_double(f):
    """COMP-2 display - 18 fractional digits (termio.c L168-L172).

    Reads the native-endian IEEE-754 ``double`` from the field bytes and
    formats it with the C ``"%-.18lf"`` conversion.
    """
    value = struct.unpack("=d", bytes(f.data[:8]))[0]
    return ("%-.18f" % value).encode("latin-1")


def _display_float(f):
    """COMP-1 display - widened to double, 18 fractional digits (termio.c L173-L177)."""
    value = struct.unpack("=f", bytes(f.data[:4]))[0]
    return ("%-.18f" % value).encode("latin-1")


def _display_pointer(f):
    """POINTER display - ``0x`` followed by hex bytes, MSB first (termio.c L178-L188).

    The C code walks the pointer bytes most-significant first (forward on a
    big-endian host, backward on little-endian) emitting two lowercase hex
    nibbles per byte.  We reproduce that ordering from :data:`sys.byteorder`.
    """
    raw = bytes(f.data[:f.size])
    ordered = raw if sys.byteorder == "big" else raw[::-1]
    out = bytearray(b"0x")
    for b in ordered:
        out += ("%x%x" % (b >> 4, b & 0xF)).encode("latin-1")
    return bytes(out)


def _format_field(f):
    """Dispatch a single field to its formatter (port of the C ``display`` L160-L206).

    The type dispatch order is preserved exactly: ``COMP-2`` (double),
    ``COMP-1`` (float), POINTER, real/non-pretty BINARY, any other NUMERIC,
    then alphanumeric.  NUMERIC-EDITED items are *not* numeric under
    ``COB_FIELD_IS_NUMERIC`` (type ``0x24 & 0x10 == 0``) and therefore fall to
    the alphanumeric byte-dump, which is correct: an edited item already holds
    its formatted text.
    """
    if f is None or f.data is None:
        # Defensive: an OMITTED / null operand contributes nothing.
        return b""

    ftype = common.COB_FIELD_TYPE(f)

    if ftype == common.COB_TYPE_NUMERIC_DOUBLE:
        return _display_double(f)
    if ftype == common.COB_TYPE_NUMERIC_FLOAT:
        return _display_float(f)
    if common.COB_FIELD_IS_POINTER(f):
        return _display_pointer(f)

    pretty = _pretty()
    if (common.COB_FIELD_REAL_BINARY(f)
            or (ftype == common.COB_TYPE_NUMERIC_BINARY and not pretty)):
        # C overrides the displayed digit count to bin_digits[byte-size] and
        # falls through to the PLAIN numeric path (termio.c L189-L196).  We
        # build a shallow field copy that aliases the data but overrides
        # ``digits`` so _display_numeric decodes the binary value into the
        # correct number of zoned digits.
        idx = f.size if 0 <= f.size < len(_BIN_DIGITS) else (len(_BIN_DIGITS) - 1)
        override = common.cob_field_attr(
            type=f.attr.type, digits=_BIN_DIGITS[idx], scale=f.attr.scale,
            flags=f.attr.flags, pic=f.attr.pic)
        alias = common.cob_field(size=f.size, data=f.data, attr=override)
        return _display_numeric(alias)

    if common.COB_FIELD_IS_NUMERIC(f):
        if pretty:
            return _pretty_display_numeric(f)
        return _display_numeric(f)

    return _display_alnum(f)


# ---------------------------------------------------------------------------
# Public runtime entry points (the emitter -> runtime contract).
# ---------------------------------------------------------------------------

def cob_display(to_stderr, newline, varcnt, *fields):
    """DISPLAY one or more fields to a terminal stream (termio.c L208-L231).

    Arguments mirror the C ``cob_display(outorerr, newline, varcnt, ...)``:

    * *to_stderr* - when truthy, output is written to ``sys.stderr``; otherwise
      it goes to ``sys.stdout``.  As in the C runtime, output is also routed to
      ``stderr`` while the curses SCREEN subsystem is active, so plain DISPLAY
      does not corrupt the managed screen (``!outorerr && !cob_screen_initialized``).
    * *newline* - when truthy a trailing newline is emitted and the stream is
      flushed (``DISPLAY ...`` normal form); when falsey the newline is
      suppressed (``DISPLAY ... WITH NO ADVANCING``) and no flush is performed,
      exactly matching the C ``if (newline) { putc('\\n'); fflush(); }``.
    * *varcnt* - the number of trailing field operands to display.
    * *fields* - the ``cob_field`` operands (Python ``*args`` for the C varargs).

    Only the first *varcnt* operands are displayed, matching the C loop bound.
    """
    stream = sys.stderr if (to_stderr or _screen_initialized()) else sys.stdout

    count = len(fields) if varcnt is None else int(varcnt)
    if count < 0:
        count = 0

    record = bytearray()
    for field in fields[:count]:
        record += _format_field(field)

    _emit(stream, bytes(record))
    if newline:
        _emit(stream, b"\n")
        _flush(stream)


def cob_accept(f):
    """ACCEPT a line of input into field *f* (termio.c L237-L280).

    Reads one line from ``sys.stdin`` and stores it into the destination field,
    delegating the byte-level storage (justification, padding, numeric parsing
    per the receiving field's USAGE) to the data-movement runtime via
    ``common._lazy_move``.

    Faithful behaviours from the C runtime:

    * End-of-input (``readline`` returns ``""`` <-> C ``fgets`` returns NULL)
      yields a single space, matching ``term_buff[0] = ' '`` with size 1.
    * The trailing newline is stripped (C ``strlen(buff) - 1``).
    * For a ``NUMERIC_DISPLAY`` receiver the input is capped to the field size
      before the MOVE (termio.c L261-L265).

    Architectural adaptation (minimal-deviation, AAP 0.7.2): the C in-function
    ``cob_screen_initialized`` redirect to ``cob_field_accept`` is intentionally
    omitted.  In the C->Python design :mod:`libcob_py.screenio` owns the curses
    ACCEPT and *delegates* to this plain path when the screen is unavailable, so
    ``termio`` is always the bottom-of-stack stream reader and never calls back
    into ``screenio``.
    """
    line = sys.stdin.readline()
    if line == "":
        # EOF: behave like the C NULL-from-fgets branch (single blank).
        data = b" "
    else:
        if line.endswith("\n"):
            line = line[:-1]
        # ``fgets`` reads at most COB_MEDIUM_BUFF-1 bytes; honour the same bound
        # (sized from the buffer allocated in cob_init_termio when present).
        cap = (len(_term_buff) - 1) if _term_buff is not None \
            else common.COB_MEDIUM_MAX
        data = line.encode("latin-1")[:cap]

    if (common.COB_FIELD_TYPE(f) == common.COB_TYPE_NUMERIC_DISPLAY
            and len(data) > f.size):
        data = data[:f.size]

    attr = common.cob_field_attr(
        type=common.COB_TYPE_ALPHANUMERIC, digits=0, scale=0, flags=0, pic=None)
    temp = common.cob_field(size=len(data), data=bytearray(data), attr=attr)
    common._lazy_move(temp, f)


def cob_init_termio():
    """Initialise the terminal-I/O subsystem (termio.c L282-L286).

    Allocates the working line buffer (``term_buff = cob_malloc(COB_MEDIUM_BUFF)``
    in C) so the ACCEPT read cap matches the original ``fgets`` bound.  This
    runs as part of the runtime start-up sequence after ``cob_init_fileio`` and
    before ``cob_init_call`` (``common.COB_INIT_ORDER``; AAP section 0.5.3) and
    is discovered automatically by ``common._run_subsystem_initializers``.
    """
    global _term_buff
    _term_buff = common.cob_malloc(common.COB_MEDIUM_BUFF)
