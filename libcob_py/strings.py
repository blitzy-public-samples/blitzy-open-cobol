"""libcob_py.strings - INSPECT / STRING / UNSTRING runtime for the Python COBOL backend.

Pure-Python port of the GNU Cobol (OpenCOBOL) C runtime module
``libcob/strings.c`` (613 lines).  It provides the runtime entry points that the
rewritten ``cobc`` code emitter calls for the COBOL ``INSPECT``, ``STRING`` and
``UNSTRING`` statements.  Every ``cob_*`` public name is preserved verbatim so
that the emitter's ``cob_<fn>`` call-sites map 1:1 onto
``libcob_py.strings.<fn>`` (AAP section 0.6.5, exhaustive emitter->runtime map).

The three statements are each implemented as the *same* bracketed call sequence
the C runtime uses - an ``init`` call, a series of accumulating sub-calls, and a
``finish`` call - operating on shared module-scope state.  This mirrors the C
file-scope statics exactly so the TALLYING/REPLACING marker semantics, the
BEFORE/AFTER region boundaries, and the DELIMITED-BY/POINTER/COUNT behaviour all
match the original runtime byte-for-byte (AAP section 0.7.2 minimal-deviation
rule).

Authoritative source (ported semantically - the C file is NOT modified):

* ``libcob/strings.c`` - the INSPECT marker engine (``inspect_common`` and the
  ALL/LEADING/FIRST/TRAILING/CHARACTERS/CONVERTING family), the STRING appender,
  and the UNSTRING splitter.

Implementation constraints (AAP sections 0.5 / 0.7.1):

* **STANDARD LIBRARY ONLY** - this module introduces ZERO third-party
  dependencies.  It operates directly on the ``bytearray`` storage of
  :class:`libcob_py.common.cob_field` objects; it uses NO regular-expression or
  third-party text package.  The C ``memcpy``/``memcmp`` byte primitives become
  plain ``bytearray`` slicing and equality, and C ``malloc``/``free`` become
  ordinary Python allocation reclaimed by the garbage collector.

* **Load-time dependency = ``libcob_py.common`` only** (the field/flag model, the
  exception subsystem, the figurative constants, the sign helpers and
  ``cob_memcpy``).  ``libcob/strings.c`` additionally calls ``cob_add_int``
  (data-arithmetic, provided by :mod:`libcob_py.numeric`) and
  ``cob_get_int`` / ``cob_set_int`` / ``cob_move`` (data-movement, provided by
  :mod:`libcob_py.move`).  To avoid an import cycle - and exactly mirroring the
  pattern documented in ``libcob_py.common`` (its ``_lazy_*`` helpers) and used
  by ``libcob_py.numeric`` - those siblings are imported *lazily inside the
  function bodies that need them* (see the ``_cob_*`` thin wrappers below).
  Consequently ``import libcob_py.strings`` succeeds with only ``common``
  present; ``numeric`` / ``move`` are required solely when the corresponding
  runtime paths (TALLYING counters, WITH POINTER, COUNT IN, DELIMITER IN) are
  actually executed.
"""

from __future__ import annotations

# The sole load-time dependency: the runtime base module (field model, flags,
# exception subsystem, figurative constants, sign handling and ``cob_memcpy``).
from libcob_py import common


# ---------------------------------------------------------------------------
# INSPECT operation selectors (strings.c L38-L41) - reproduced verbatim.
# ---------------------------------------------------------------------------
INSPECT_ALL = 0
INSPECT_LEADING = 1
INSPECT_FIRST = 2
INSPECT_TRAILING = 3

# Default delimiter-table hint (strings.c L43).  Retained as a named constant for
# fidelity; Python grows the delimiter list dynamically so it is advisory only.
DLM_DEFAULT_NUM = 8


# ===========================================================================
# Module-scope working state.
#
# These mirror the file-scope ``static`` variables of strings.c.  Because a
# COBOL program is single-threaded with respect to a given INSPECT/STRING/
# UNSTRING statement (each statement runs to completion before the next), shared
# module state is a faithful and sufficient model of the C statics.  Functions
# that assign these declare ``global``.
# ===========================================================================

# --- INSPECT state (strings.c L50-L59) ---
_inspect_var = None          # snapshot of the inspected field (shares its data)
_inspect_data = None         # the inspected field's backing bytearray
_inspect_base = 0            # offset to the first value byte (skips a leading
#                              SEPARATE sign byte), mirroring COB_FIELD_DATA
_inspect_size = 0            # value-byte count (COB_FIELD_SIZE)
_inspect_start = 0           # value-relative start offset of the active region
_inspect_end = 0             # value-relative end offset (exclusive)
_inspect_mark = []           # per-byte marker array (-1 == untouched)
_inspect_replacing = 0       # non-zero for INSPECT ... REPLACING
_inspect_sign = 0            # operational sign extracted at init, restored at finish

# --- STRING state (strings.c L61-L67) ---
_string_dst = None           # destination field snapshot (shares its data)
_string_ptr = None           # WITH POINTER field snapshot, or None
_string_dlm = None           # current DELIMITED BY field snapshot, or None
_string_offset = 0           # 0-based write cursor into the destination

# --- UNSTRING state (strings.c L69-L76) ---
_dlm_list = []               # list of (delimiter-field-snapshot, all_flag) tuples
_unstring_src = None         # source field snapshot (shares its data)
_unstring_ptr = None         # WITH POINTER field snapshot, or None
_unstring_offset = 0         # 0-based read cursor into the source
_unstring_count = 0          # number of receiving items filled (for TALLYING)
_unstring_ndlms = 0          # number of registered delimiters


# ===========================================================================
# Lazy cross-module helpers (cycle-breaking deferred imports).
#
# ``numeric`` and ``move`` both import ``common`` (and ``common`` is imported by
# this module), so importing them at module top level could create an import
# cycle and would also force them to exist before ``import libcob_py.strings``
# can succeed.  Following the exact pattern of ``common``'s ``_lazy_*`` helpers,
# the import is performed on first use, inside the function body.
# ===========================================================================

def _cob_add_int(f, n):
    """``cob_add_int`` via a deferred import of :mod:`libcob_py.numeric`."""
    from libcob_py import numeric  # deferred: breaks the import cycle
    return numeric.cob_add_int(f, n)


def _cob_get_int(f):
    """``cob_get_int`` via a deferred import of :mod:`libcob_py.move`."""
    from libcob_py import move  # deferred
    return move.cob_get_int(f)


def _cob_set_int(f, n):
    """``cob_set_int`` via a deferred import of :mod:`libcob_py.move`."""
    from libcob_py import move  # deferred
    return move.cob_set_int(f, n)


def _cob_move(src, dst):
    """``cob_move`` via a deferred import of :mod:`libcob_py.move`."""
    from libcob_py import move  # deferred
    return move.cob_move(src, dst)


# ===========================================================================
# Internal helpers
# ===========================================================================

def _copy_field(f):
    """Return a shallow snapshot of *f* that SHARES its data buffer.

    The C runtime brackets each statement with a struct copy such as
    ``inspect_var_copy = *var`` (strings.c L214): the ``cob_field`` *value*
    (size / data-pointer / attr-pointer) is snapshotted while the underlying
    data bytes remain shared, so in-place writes still reach the caller's field.
    ``cob_field``'s constructor keeps an existing ``bytearray`` by reference
    (it does not copy it), so passing ``f.data`` through reproduces that exact
    shared-data semantic.  ``None`` (an OMITTED argument) passes through
    unchanged.
    """
    if f is None:
        return None
    return common.cob_field(f.size, f.data, f.attr)


def cob_min_int(x, y):
    """Return the smaller of two ints (strings.c L81-L88, ``cob_min_int``)."""
    if x < y:
        return x
    return y


def alloc_figurative(f1, f2):
    """Expand figurative replacement *f1* to *f2*'s length (strings.c L90-L120).

    When ``INSPECT ... REPLACING`` names a figurative constant (e.g. ``SPACE``,
    an ``ALPHANUMERIC_ALL`` field of size 1) as the *replacement* but the matched
    value *f2* is wider, the replacement must be stretched to the same width by
    repeating its bytes cyclically.  Returns a fresh ALPHANUMERIC
    :class:`~libcob_py.common.cob_field` of size ``f2.size`` (the C code reuses a
    growable static buffer; Python allocates per call and lets the GC reclaim it,
    which is behaviourally identical).
    """
    size2 = f2.size
    figptr = bytearray(size2)
    size1 = 0
    for n in range(size2):
        figptr[n] = f1.data[size1]
        size1 += 1
        if size1 >= f1.size:
            size1 = 0
    attr = common.cob_field_attr(common.COB_TYPE_ALPHANUMERIC, 0, 0, 0, None)
    return common.cob_field(size2, figptr, attr)


def inspect_common(f1, f2, type_):
    """Core INSPECT match/mark engine (strings.c L122-L202).

    Shared by ``cob_inspect_all`` / ``cob_inspect_leading`` /
    ``cob_inspect_first`` / ``cob_inspect_trailing``.  Scans the active region
    ``[_inspect_start, _inspect_end)`` for occurrences of the matched value
    *f2*, honouring the per-byte marker array so that no byte is counted or
    replaced twice.  ``type_`` selects the scan strategy:

    * ``INSPECT_ALL``      - every non-overlapping occurrence,
    * ``INSPECT_LEADING``  - only the run anchored at the region start,
    * ``INSPECT_FIRST``    - only the first occurrence,
    * ``INSPECT_TRAILING`` - the run anchored at the region end (scanned
      right-to-left).

    When ``_inspect_replacing`` is set the matched bytes are marked with the
    replacement bytes (*f1*); otherwise they are marked as "counted" and the
    occurrence count is added to the TALLYING counter *f1* via ``cob_add_int``.
    """
    # A NULL field models a figurative LOW-VALUE operand (strings.c L131-L136).
    if f1 is None:
        f1 = common.cob_low
    if f2 is None:
        f2 = common.cob_low

    # REPLACING requires matched/replacement widths to agree; a figurative
    # ALPHANUMERIC_ALL replacement is stretched, otherwise it is a size error
    # (strings.c L138-L146).
    if _inspect_replacing and f1.size != f2.size:
        if common.COB_FIELD_TYPE(f1) == common.COB_TYPE_ALPHANUMERIC_ALL:
            f1 = alloc_figurative(f1, f2)
        else:
            common.cob_set_exception(common.COB_EC_RANGE_INSPECT_SIZE)
            return

    data = _inspect_data
    base = _inspect_base
    mark = _inspect_mark
    # ``mark`` is indexed from the region start: C ``mark = &inspect_mark[start]``
    # (strings.c L148).  ``mark_base`` is that starting index.
    mark_base = _inspect_start
    length = _inspect_end - _inspect_start
    f2data = f2.data
    f2size = f2.size
    n = 0

    if type_ == INSPECT_TRAILING:
        # Right-to-left scan, stopping at the first non-match (strings.c L150-L171).
        i = length - f2size
        while i >= 0:
            if data[base + _inspect_start + i:base + _inspect_start + i + f2size] \
                    == f2data[:f2size]:
                # Skip the occurrence if any constituent byte is already marked.
                blocked = False
                for j in range(f2size):
                    if mark[mark_base + i + j] != -1:
                        blocked = True
                        break
                if not blocked:
                    for j in range(f2size):
                        mark[mark_base + i + j] = (
                            f1.data[j] if _inspect_replacing else 1)
                    i -= f2size - 1
                    n += 1
                i -= 1
            else:
                break
    else:
        # Left-to-right scan (strings.c L172-L196).
        i = 0
        while i < length - f2size + 1:
            if data[base + _inspect_start + i:base + _inspect_start + i + f2size] \
                    == f2data[:f2size]:
                blocked = False
                for j in range(f2size):
                    if mark[mark_base + i + j] != -1:
                        blocked = True
                        break
                if not blocked:
                    for j in range(f2size):
                        mark[mark_base + i + j] = (
                            f1.data[j] if _inspect_replacing else 1)
                    i += f2size - 1
                    n += 1
                    if type_ == INSPECT_FIRST:
                        break
                i += 1
            elif type_ == INSPECT_LEADING:
                # LEADING stops at the first byte that does not match.
                break
            else:
                i += 1

    # TALLYING: accumulate the occurrence count into the counter (strings.c L199-L201).
    if n > 0 and not _inspect_replacing:
        _cob_add_int(f1, n)


# ===========================================================================
# INSPECT (strings.c L204-L358)
# ===========================================================================

def cob_inspect_init(var, replacing):
    """Begin an INSPECT on *var* (strings.c L208-L232).

    Snapshots the inspected field, extracts (and normalises) its operational
    sign, sizes a fresh per-byte marker array to the value width and clears the
    exception code.  ``replacing`` is truthy for ``INSPECT ... REPLACING`` (whose
    marks carry replacement bytes written back in :func:`cob_inspect_finish`) and
    falsy for ``INSPECT ... TALLYING`` / ``CONVERTING``.
    """
    global _inspect_var, _inspect_data, _inspect_base, _inspect_size
    global _inspect_start, _inspect_end, _inspect_mark
    global _inspect_replacing, _inspect_sign

    _inspect_var = _copy_field(var)
    _inspect_replacing = replacing
    # cob_get_sign normalises an embedded DISPLAY sign back to a plain digit in
    # place, so the marker scan sees clean digits; the sign is restored at
    # finish (strings.c L217, L357).
    _inspect_sign = common.cob_get_sign(var)
    _inspect_size = common.COB_FIELD_SIZE(var)
    # ``base`` is the offset of the first value byte within ``var.data``.  It is
    # 1 only for a SEPARATE *and* LEADING sign (mirroring COB_FIELD_DATA), so
    # in-place writes through ``var.data`` always reach the caller's storage.
    if common.COB_FIELD_SIGN_SEPARATE(var) and common.COB_FIELD_SIGN_LEADING(var):
        _inspect_base = 1
    else:
        _inspect_base = 0
    _inspect_data = var.data
    _inspect_start = 0
    _inspect_end = 0
    _inspect_mark = [-1] * _inspect_size
    common.cob_exception_code = 0


def cob_inspect_start():
    """Open the active region to the whole value (strings.c L234-L239)."""
    global _inspect_start, _inspect_end
    _inspect_start = 0
    _inspect_end = _inspect_size


def cob_inspect_before(str_):
    """Restrict the active region to before the first *str_* (strings.c L241-L252).

    Models ``INSPECT ... BEFORE INITIAL str``: scans forward from the current
    region start for *str_* and, on the first occurrence, clamps the region end
    to that position.  If *str_* does not occur the region is unchanged.
    """
    global _inspect_end
    data = _inspect_data
    base = _inspect_base
    s = str_.data
    ssize = str_.size
    p = _inspect_start
    while p < _inspect_end - ssize + 1:
        if data[base + p:base + p + ssize] == s[:ssize]:
            _inspect_end = p
            return
        p += 1


def cob_inspect_after(str_):
    """Restrict the active region to after the first *str_* (strings.c L254-L266).

    Models ``INSPECT ... AFTER INITIAL str``: scans forward for *str_* and, on
    the first occurrence, moves the region start to just past it.  If *str_* does
    not occur the region collapses to empty (start == end).
    """
    global _inspect_start
    data = _inspect_data
    base = _inspect_base
    s = str_.data
    ssize = str_.size
    p = _inspect_start
    while p < _inspect_end - ssize + 1:
        if data[base + p:base + p + ssize] == s[:ssize]:
            _inspect_start = p + ssize
            return
        p += 1
    _inspect_start = _inspect_end


def cob_inspect_characters(f1):
    """INSPECT ... CHARACTERS (strings.c L268-L298).

    For ``REPLACING CHARACTERS BY x`` every still-unmarked byte of the active
    region is marked with the replacement byte ``x`` (written back at finish).
    For ``TALLYING f1 CHARACTERS`` every still-unmarked byte is counted and the
    total added to the counter *f1*.
    """
    mark = _inspect_mark
    mark_base = _inspect_start
    length = _inspect_end - _inspect_start
    if _inspect_replacing:
        for i in range(length):
            if mark[mark_base + i] == -1:
                mark[mark_base + i] = f1.data[0]
    else:
        n = 0
        for i in range(length):
            if mark[mark_base + i] == -1:
                mark[mark_base + i] = 1
                n += 1
        if n > 0:
            _cob_add_int(f1, n)


def cob_inspect_all(f1, f2):
    """INSPECT ... ALL *f2* (strings.c L300-L304) - every occurrence."""
    inspect_common(f1, f2, INSPECT_ALL)


def cob_inspect_leading(f1, f2):
    """INSPECT ... LEADING *f2* (strings.c L306-L310) - the leading run only."""
    inspect_common(f1, f2, INSPECT_LEADING)


def cob_inspect_first(f1, f2):
    """INSPECT ... FIRST *f2* (strings.c L312-L316) - the first occurrence only."""
    inspect_common(f1, f2, INSPECT_FIRST)


def cob_inspect_trailing(f1, f2):
    """INSPECT ... TRAILING *f2* (strings.c L318-L322) - the trailing run only."""
    inspect_common(f1, f2, INSPECT_TRAILING)


def cob_inspect_converting(f1, f2):
    """INSPECT ... CONVERTING *f1* TO *f2* (strings.c L324-L342).

    A character-translate operation: each byte of the active region equal to
    some ``f1[j]`` is replaced (in place) by the corresponding ``f2[j]`` and
    marked so it is not converted again.  When *f1* is wider than *f2* the
    surplus source characters all map to the final byte of *f2* (the C
    ``ix = f2->size - 1`` clamp).

    This faithfully reproduces the C indexing exactly as written: the data byte
    is read at ``inspect_start + i`` while the marker is consulted at the
    region-relative index ``i`` (strings.c uses ``inspect_mark[i]`` here, not
    the ``inspect_start``-offset form used elsewhere).  CONVERTING is emitted
    with ``replacing == 0`` so the direct in-place writes below are authoritative
    and :func:`cob_inspect_finish` performs no mark write-back.
    """
    data = _inspect_data
    base = _inspect_base
    mark = _inspect_mark
    length = _inspect_end - _inspect_start
    f1data = f1.data
    f2data = f2.data
    f2size = f2.size
    for j in range(f1.size):
        for i in range(length):
            if mark[i] == -1 and data[base + _inspect_start + i] == f1data[j]:
                ix = j
                if ix >= f2size:
                    ix = f2size - 1
                data[base + _inspect_start + i] = f2data[ix]
                mark[i] = 1


def cob_inspect_finish():
    """Complete an INSPECT (strings.c L344-L358).

    For ``REPLACING`` every marked byte is written back into the inspected value
    from the marker array (the marks carry the replacement bytes).  The
    operational sign extracted in :func:`cob_inspect_init` is then restored.
    """
    if _inspect_replacing:
        data = _inspect_data
        base = _inspect_base
        mark = _inspect_mark
        for i in range(_inspect_size):
            if mark[i] != -1:
                # The C stores ``inspect_mark[i]`` into an unsigned char; the
                # mask keeps the write within a single byte.
                data[base + i] = mark[i] & 0xFF

    common.cob_put_sign(_inspect_var, _inspect_sign)


# ===========================================================================
# STRING (strings.c L360-L434)
# ===========================================================================

def cob_string_init(dst, ptr):
    """Begin a STRING into *dst* (strings.c L364-L383).

    Snapshots the destination and the optional ``WITH POINTER`` field, resets the
    write cursor and clears the exception code.  When a POINTER is supplied its
    (1-based) value seeds the cursor; a value outside ``1 .. dst.size`` raises
    ``EC-OVERFLOW-STRING`` immediately, which suppresses every subsequent
    :func:`cob_string_append` (matching the C guard).
    """
    global _string_dst, _string_ptr, _string_offset

    _string_dst = _copy_field(dst)
    _string_ptr = None
    # QA FIX (CRITICAL #4, emitter<->runtime contract): mirror the C guard
    # ``if (ptr)`` (strings.c L370), NOT ``ptr is not None``.  The immutable
    # front-end (typeck.c cb_emit_string) supplies ``cb_int0`` for an omitted
    # ``WITH POINTER`` phrase, which the emitter renders as the literal integer
    # ``0`` (== C NULL) - e.g. ``strings.cob_string_init(f_12, 0)``.  A truthiness
    # test treats that ``0`` as "no pointer" exactly as C does, while a genuine
    # ``cob_field`` (which defines no ``__bool__``/``__len__``) is always truthy.
    # The previous ``is not None`` accepted the ``0`` and crashed in _copy_field
    # with ``AttributeError: 'int' object has no attribute 'size'``.
    if ptr:
        _string_ptr = _copy_field(ptr)
    _string_offset = 0
    common.cob_exception_code = 0

    if _string_ptr is not None:
        _string_offset = _cob_get_int(_string_ptr) - 1
        if _string_offset < 0 or _string_offset >= _string_dst.size:
            common.cob_set_exception(common.COB_EC_OVERFLOW_STRING)


def cob_string_delimited(dlm):
    """Set the DELIMITED BY value for the next append(s) (strings.c L385-L393).

    A ``None`` *dlm* models ``DELIMITED BY SIZE`` (no delimiter - the whole
    source is appended).
    """
    global _string_dlm
    _string_dlm = None
    # QA FIX (CRITICAL #4): mirror the C guard ``if (dlm)`` (strings.c L389).
    # typeck.c emits ``cb_int0`` (-> integer ``0``) for ``DELIMITED BY SIZE``
    # (no delimiter), so a truthiness test is required: ``0`` means "no
    # delimiter" while a real ``cob_field`` delimiter is truthy.  ``is not None``
    # would pass ``0`` into _copy_field and raise AttributeError.
    if dlm:
        _string_dlm = _copy_field(dlm)


def cob_string_append(src):
    """Append *src* to the destination (strings.c L395-L426).

    Honours the active DELIMITED BY value by truncating *src* at its first
    occurrence.  As many bytes as fit are copied at the write cursor; if the
    source does not fit, the destination is filled to the end and
    ``EC-OVERFLOW-STRING`` is raised.  Once any exception is pending the call is
    a no-op (the C early-return), so a prior overflow halts the statement.
    """
    global _string_offset

    if common.cob_exception_code:
        return

    src_size = src.size
    if _string_dlm is not None:
        dlm_data = _string_dlm.data
        dlm_size = _string_dlm.size
        size = src_size - dlm_size + 1
        for i in range(size):
            if src.data[i:i + dlm_size] == dlm_data[:dlm_size]:
                src_size = i
                break

    dst = _string_dst
    if src_size <= dst.size - _string_offset:
        # Raw byte move (C ``memcpy``): no receiving-field editing applies.
        dst.data[_string_offset:_string_offset + src_size] = src.data[:src_size]
        _string_offset += src_size
    else:
        size = dst.size - _string_offset
        dst.data[_string_offset:_string_offset + size] = src.data[:size]
        _string_offset += size
        common.cob_set_exception(common.COB_EC_OVERFLOW_STRING)


def cob_string_finish():
    """Complete a STRING (strings.c L428-L434).

    Writes the final (1-based) cursor back through the ``WITH POINTER`` field, if
    one was supplied.
    """
    if _string_ptr is not None:
        _cob_set_int(_string_ptr, _string_offset + 1)


# ===========================================================================
# UNSTRING (strings.c L436-L596)
# ===========================================================================

def cob_unstring_init(src, ptr, num_dlm):
    """Begin an UNSTRING of *src* (strings.c L440-L479).

    Snapshots the source and the optional ``WITH POINTER`` field, resets the read
    cursor / filled-item count / delimiter list and clears the exception code.
    *num_dlm* is the number of delimiters the statement will register; the C
    runtime uses it to size a reusable buffer, whereas Python grows the list
    dynamically so the argument is advisory (accepted for call-site
    compatibility).  When a POINTER is supplied its (1-based) value seeds the
    cursor; a value outside ``1 .. src.size`` raises ``EC-OVERFLOW-UNSTRING``.
    """
    global _unstring_src, _unstring_ptr, _unstring_offset
    global _unstring_count, _unstring_ndlms, _dlm_list

    _unstring_src = _copy_field(src)
    _unstring_ptr = None
    # QA FIX (CRITICAL #4): mirror the C guard ``if (ptr)`` (strings.c L448).
    # typeck.c cb_emit_unstring passes ``cb_int0`` (-> integer ``0``) for an
    # omitted ``WITH POINTER`` phrase; a truthiness test treats it as NULL,
    # whereas ``is not None`` crashed in _copy_field on the integer ``0``.
    if ptr:
        _unstring_ptr = _copy_field(ptr)

    _unstring_offset = 0
    _unstring_count = 0
    _unstring_ndlms = 0
    # ``num_dlm`` is intentionally unused beyond documentation: the delimiter
    # store is a Python list grown by cob_unstring_delimited.  Touching it keeps
    # linters from flagging an unused parameter while preserving the signature.
    _ = num_dlm
    _dlm_list = []
    common.cob_exception_code = 0

    if _unstring_ptr is not None:
        _unstring_offset = _cob_get_int(_unstring_ptr) - 1
        if _unstring_offset < 0 or _unstring_offset >= _unstring_src.size:
            common.cob_set_exception(common.COB_EC_OVERFLOW_UNSTRING)


def cob_unstring_delimited(dlm, all_):
    """Register a DELIMITED BY value (strings.c L481-L487).

    *all_* is truthy for ``DELIMITED BY ALL dlm`` (consecutive delimiter runs are
    treated as a single separator).  Delimiters are matched in registration
    order, mirroring the C delimiter table.
    """
    global _unstring_ndlms
    _dlm_list.append((_copy_field(dlm), all_))
    _unstring_ndlms += 1


def cob_unstring_into(dst, dlm, cnt):
    """Extract the next field into *dst* (strings.c L489-L578).

    With no delimiters registered, copies up to ``dst``'s capacity from the read
    cursor.  Otherwise scans for the first registered delimiter from the cursor:
    the bytes before it are moved into *dst*; ``DELIMITED BY ALL`` then skips any
    immediately-following copies of that same delimiter.  The optional
    ``DELIMITER IN`` field *dlm* receives the matched delimiter (or ZERO/SPACE
    when the field ended without a delimiter), and the optional ``COUNT IN``
    field *cnt* receives the number of source characters moved.  Pending
    exceptions short-circuit the call (the C early-return).
    """
    global _unstring_offset, _unstring_count

    if common.cob_exception_code:
        return

    src = _unstring_src
    src_data = src.data
    src_size = src.size

    if _unstring_offset >= src_size:
        return

    start = _unstring_offset            # absolute offset of the field start
    dlm_data = None                     # matched delimiter bytes (None == no match)
    dlm_size = 0
    match_size = 0

    if _unstring_ndlms == 0:
        # DELIMITED BY SIZE: copy up to the receiver's capacity (strings.c L514-L518).
        match_size = cob_min_int(common.COB_FIELD_SIZE(dst),
                                 src_size - _unstring_offset)
        common.cob_memcpy(dst, src_data[start:start + match_size], match_size)
        _unstring_offset += match_size
    else:
        brkpt = False
        p = start
        while p < src_size:
            matched = False
            for i in range(_unstring_ndlms):
                d_field = _dlm_list[i][0]
                d_all = _dlm_list[i][1]
                dlsize = d_field.size
                dp = d_field.data
                if p + dlsize > src_size:
                    continue
                if src_data[p:p + dlsize] == dp[:dlsize]:
                    # Delimiter found: move the preceding bytes into the receiver.
                    match_size = p - start
                    common.cob_memcpy(dst, src_data[start:start + match_size],
                                      match_size)
                    _unstring_offset += match_size + dlsize
                    dlm_data = dp
                    dlm_size = dlsize
                    if d_all:
                        # DELIMITED BY ALL: swallow consecutive delimiters.
                        p += dlsize
                        while p < src_size:
                            if p + dlsize > src_size:
                                break
                            if src_data[p:p + dlsize] != dp[:dlsize]:
                                break
                            _unstring_offset += dlsize
                            p += dlsize
                    brkpt = True
                    matched = True
                    break
            if brkpt:
                break
            if not matched:
                p += 1
        if not brkpt:
            # No delimiter to the end: copy the remainder (strings.c L555-L561).
            match_size = src_size - _unstring_offset
            common.cob_memcpy(dst, src_data[start:start + match_size], match_size)
            _unstring_offset = src_size
            dlm_data = None

    _unstring_count += 1

    # DELIMITER IN: deliver the matched delimiter, or a figurative fill when the
    # field ended without one (strings.c L565-L573).
    # QA FIX (CRITICAL #4): mirror the C guard ``if (dlm)`` (strings.c L565).
    # cb_build_unstring_into defaults an omitted ``DELIMITER IN`` to ``cb_int0``
    # (-> integer ``0``); a truthiness test treats ``0`` as "no receiver", while
    # ``is not None`` would route ``0`` into cob_memcpy/_cob_move and fail.
    if dlm:
        if dlm_data is not None:
            common.cob_memcpy(dlm, dlm_data[:dlm_size], dlm_size)
        elif common.COB_FIELD_IS_NUMERIC(dlm):
            _cob_move(common.cob_zero, dlm)
        else:
            _cob_move(common.cob_space, dlm)

    # COUNT IN: deliver the number of source characters moved (strings.c L575-L577).
    # QA FIX (CRITICAL #4): mirror the C guard ``if (cnt)`` (strings.c L577);
    # an omitted ``COUNT IN`` is ``cb_int0`` (-> integer ``0``), so use truthiness.
    if cnt:
        _cob_set_int(cnt, match_size)


def cob_unstring_tallying(f):
    """UNSTRING ... TALLYING IN *f* (strings.c L580-L584).

    Adds the number of receiving items filled by this statement to *f*.
    """
    _cob_add_int(f, _unstring_count)


def cob_unstring_finish():
    """Complete an UNSTRING (strings.c L586-L596).

    Raises ``EC-OVERFLOW-UNSTRING`` if source characters remain unexamined (the
    ``ON OVERFLOW`` condition) and writes the final (1-based) cursor back through
    the ``WITH POINTER`` field, if one was supplied.
    """
    if _unstring_offset < _unstring_src.size:
        common.cob_set_exception(common.COB_EC_OVERFLOW_UNSTRING)

    if _unstring_ptr is not None:
        _cob_set_int(_unstring_ptr, _unstring_offset + 1)


# ===========================================================================
# Initialization (strings.c L598-L613)
# ===========================================================================

def cob_init_strings():
    """Initialise the strings subsystem (strings.c L600-L613).

    The C runtime pre-allocates the marker buffer (``COB_MEDIUM_BUFF`` bytes) and
    the scratch figurative field here.  In Python those buffers are allocated
    lazily and sized exactly per operation (the marker array in
    :func:`cob_inspect_init`, figurative fields in :func:`alloc_figurative`), so
    this routine simply resets the module's working state to a clean baseline.
    It participates in the runtime start-up sequence after ``cob_init_numeric``
    and before ``cob_init_move`` (AAP section 0.3.2).
    """
    global _inspect_var, _inspect_data, _inspect_base, _inspect_size
    global _inspect_start, _inspect_end, _inspect_mark
    global _inspect_replacing, _inspect_sign
    global _string_dst, _string_ptr, _string_dlm, _string_offset
    global _dlm_list, _unstring_src, _unstring_ptr, _unstring_offset
    global _unstring_count, _unstring_ndlms

    # Reference COB_MEDIUM_BUFF for parity with the C buffer pre-sizing; Python's
    # per-operation allocation makes an eager buffer unnecessary.
    _ = common.COB_MEDIUM_BUFF

    _inspect_var = None
    _inspect_data = None
    _inspect_base = 0
    _inspect_size = 0
    _inspect_start = 0
    _inspect_end = 0
    _inspect_mark = []
    _inspect_replacing = 0
    _inspect_sign = 0

    _string_dst = None
    _string_ptr = None
    _string_dlm = None
    _string_offset = 0

    _dlm_list = []
    _unstring_src = None
    _unstring_ptr = None
    _unstring_offset = 0
    _unstring_count = 0
    _unstring_ndlms = 0


# ===========================================================================
# Public API surface.
#
# Every ``cob_*`` name the emitter may target for INSPECT / STRING / UNSTRING is
# exported.  ``inspect_common``, ``alloc_figurative`` and ``cob_min_int`` are the
# internal helpers retained from strings.c (exported for completeness / testing,
# matching the C translation unit's symbol set).
# ===========================================================================
__all__ = [
    # INSPECT
    "cob_inspect_init",
    "cob_inspect_start",
    "cob_inspect_before",
    "cob_inspect_after",
    "cob_inspect_characters",
    "cob_inspect_all",
    "cob_inspect_leading",
    "cob_inspect_first",
    "cob_inspect_trailing",
    "cob_inspect_converting",
    "cob_inspect_finish",
    # STRING
    "cob_string_init",
    "cob_string_delimited",
    "cob_string_append",
    "cob_string_finish",
    # UNSTRING
    "cob_unstring_init",
    "cob_unstring_delimited",
    "cob_unstring_into",
    "cob_unstring_tallying",
    "cob_unstring_finish",
    # Initialisation
    "cob_init_strings",
    # Internal helpers (retained from the C translation unit)
    "inspect_common",
    "alloc_figurative",
    "cob_min_int",
    # INSPECT operation selectors
    "INSPECT_ALL",
    "INSPECT_LEADING",
    "INSPECT_FIRST",
    "INSPECT_TRAILING",
]
