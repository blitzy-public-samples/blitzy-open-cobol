"""Unit tests for :mod:`libcob_py.common` - the runtime *base* module.

``libcob_py.common`` is the pure-Python re-implementation of the C runtime
``libcob/common.c`` (with its companion enumeration ``libcob/exception.def`` and
the field/type model declared in ``libcob/common.h``).  It is the foundation of
the GNU Cobol C->Python backend refactor: every other ``libcob_py`` sub-module
imports it, and the rewritten ``cobc/codegen.c`` emits calls against the
``cob_*`` / ``COB_*`` surface it exposes.  These tests verify that surface
against the *authoritative C semantics*, asserting the values that were
confirmed by direct inspection of the source tree (146 exception conditions
across 23 categories - NOT the AAP narrative's looser "95/22" aggregate).

The suite is organised to mirror the six behavioural areas called out for this
file plus a broad "runtime helpers" group that exercises the remaining public
surface so the module clears the >=80% line-coverage gate (AAP section 0.7.1):

    Phase 1  exception table & dispatch (the headline 146/23 assertions)
    Phase 2  field model constants & cob_field construction
    Phase 3  sign overpunch round-trips (ASCII + EBCDIC, char- and field-level)
    Phase 4  class tests (NUMERIC/ALPHABETIC*) & comparison
    Phase 5  allocation & lifecycle (malloc / allocate-free / module enter-leave)
    Phase 6  cob_init orchestration ORDER (numeric->...->call; screenio excluded)
    Extras   field<->string, mem primitives, integer truncation, pointers,
             switches, run-time bounds checks, EXTERNAL items, table SORT,
             DATE/TIME ACCEPT, the environment API, source-location/TRACE, the
             C$ system helpers, CHAINING, COMMAND-LINE and run-time diagnostics.

HARD CONSTRAINTS (AAP sections 0.5 / 0.7.1)
-------------------------------------------
* **Standard library only.**  The sole non-stdlib import is ``pytest`` (a
  development-only test framework, never a runtime dependency).  ``os`` and
  ``struct`` from the standard library are used for environment and pointer-width
  assertions.  NO third-party package is imported.
* The module under test is reached through :func:`pytest.importorskip` so the
  suite *skips cleanly* (rather than erroring at collection) when the
  parallel-built ``libcob_py`` runtime is not yet importable.  ``conftest.py``
  has already placed the repository root on ``sys.path``.
"""

import os
import struct

import pytest

# Import the module under test and the package facade lazily: a clean SKIP (not
# a hard error) results when the parallel-built runtime is unavailable.
common = pytest.importorskip("libcob_py.common")
libcob_py = pytest.importorskip("libcob_py")


# ===========================================================================
# Shared helpers
# ===========================================================================
def mk(data, type=None, digits=0, scale=0, flags=0, size=None, pic=None):
    """Build a :class:`common.cob_field` from already-encoded *data* bytes.

    Byte layout per ``USAGE`` lives in ``libcob_py.numeric`` / ``libcob_py.move``;
    callers therefore pass the raw bytes plus the attribute metadata.  ``size``
    defaults to ``len(data)`` (the common elementary-item case) but may be given
    explicitly to model a SEPARATE-sign field whose value bytes are shorter than
    its storage.
    """
    field_type = common.COB_TYPE_UNKNOWN if type is None else type
    payload = bytearray(data)
    attr = common.cob_field_attr(type=field_type, digits=digits, scale=scale,
                                 flags=flags, pic=pic)
    return common.cob_field(size=len(payload) if size is None else size,
                            data=payload, attr=attr)


def _entry_field(entry, index):
    """Return ``entry[index]`` whether *entry* is a namedtuple or a plain tuple.

    The exception-table rows are ``namedtuple(code, id, name, critical)`` in the
    reference implementation, but this accessor keeps the assertions robust if a
    future port stores them as bare tuples or a small object - the column order
    (code, id, name, critical) is the stable contract.
    """
    return entry[index]


# The mutable module-level globals that individual tests legitimately mutate.
# The autouse fixture below snapshots and restores them so no test can leak
# runtime state into another (exception latches, the active-module chain, the
# init guard, switches, the EXTERNAL store, the allocation cache, ...).
_SCALAR_GLOBALS = (
    "cob_exception_code", "cob_got_exception", "cob_initialized",
    "cob_current_module", "cob_call_params", "cob_save_call_params",
    "cob_initial_external", "_cob_line_trace", "_commln", "_cob_local_env",
    "_current_arg", "_cob_argc", "_cob_argv",
    "cob_current_program_id", "cob_current_section", "cob_current_paragraph",
    "cob_source_file", "cob_source_statement", "cob_source_line",
    "cob_orig_program_id", "cob_orig_section", "cob_orig_paragraph",
    "cob_orig_statement", "cob_orig_line",
)


@pytest.fixture(autouse=True)
def _isolate_common_globals():
    """Snapshot and restore ``common``'s mutable globals around every test.

    Reassignable scalars/None values are restored by ``setattr``; the container
    globals (``_cob_switch`` list, the ``_cob_alloc_base`` allocation cache and
    the ``_externals`` EXTERNAL store) are mutated *in place* by the runtime, so
    they are restored in place to preserve object identity.
    """
    saved = {name: getattr(common, name, None) for name in _SCALAR_GLOBALS}
    saved_switch = list(getattr(common, "_cob_switch", []))
    saved_alloc = list(getattr(common, "_cob_alloc_base", []))
    saved_externals = dict(getattr(common, "_externals", {}))
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(common, name, value)
        if hasattr(common, "_cob_switch"):
            common._cob_switch[:] = saved_switch
        if hasattr(common, "_cob_alloc_base"):
            common._cob_alloc_base[:] = saved_alloc
        if hasattr(common, "_externals"):
            common._externals.clear()
            common._externals.update(saved_externals)


# Expected exception-table facts (bash-confirmed against libcob/exception.def).
_EXPECTED_EXCEPTION_COUNT = 146
_EXPECTED_CATEGORIES = frozenset({
    "EC-ALL", "EC-ARGUMENT", "EC-BOUND", "EC-DATA", "EC-FLOW", "EC-FUNCTION",
    "EC-I-O", "EC-IMP", "EC-LOCALE", "EC-OO", "EC-ORDER", "EC-OVERFLOW",
    "EC-PROGRAM", "EC-RAISING", "EC-RANGE", "EC-REPORT", "EC-SCREEN", "EC-SIZE",
    "EC-SORT-MERGE", "EC-STORAGE", "EC-USER", "EC-VALIDATE", "EC-XML",
})

# Column indices into an exception-table row (code, enum id, EC-name, critical).
_COL_CODE, _COL_ID, _COL_NAME, _COL_CRIT = 0, 1, 2, 3


def _exception_table():
    """Return the runtime exception table, probing the documented name first.

    Prefers the documented :data:`EXCEPTION_TABLE`; falls back to the C-style
    ``cob_exception_table`` / ``EXCEPTIONS`` spellings so the assertions stay
    robust to a renamed-but-equivalent table.  Skips cleanly if none is present.
    """
    for attr in ("EXCEPTION_TABLE", "cob_exception_table", "EXCEPTIONS"):
        table = getattr(common, attr, None)
        if table is not None:
            return table
    pytest.skip("libcob_py.common exposes no exception table attribute")


# ===========================================================================
# Phase 1 - Exception table & dispatch
# ===========================================================================
def test_exception_table_has_146_entries():
    """The table enumerates exactly the 146 authoritative EC-* conditions."""
    table = _exception_table()
    assert len(table) == _EXPECTED_EXCEPTION_COUNT


def test_exception_categories_count_23():
    """The 146 conditions span exactly 23 distinct categories.

    A *category* is a top-level EC-<CAT> condition: every category has a "root"
    entry whose code low byte is ``0x00`` (e.g. ``EC-ARGUMENT`` = ``0x0100``),
    while ``EC-ALL`` (code ``0xFFFF``) is its own category.  Deriving the set
    this way (rather than by string-splitting names like ``EC-I-O`` /
    ``EC-SORT-MERGE`` that themselves contain hyphens) yields exactly the 23
    documented names.
    """
    table = _exception_table()
    categories = {
        _entry_field(entry, _COL_NAME)
        for entry in table
        if (_entry_field(entry, _COL_CODE) & 0xFF) == 0
    }
    categories.add("EC-ALL")
    assert categories == set(_EXPECTED_CATEGORIES)
    assert len(categories) == 23

    # When the module publishes a precomputed category count, it must agree.
    published = getattr(common, "EXCEPTION_CATEGORY_COUNT", None)
    if published is not None:
        assert published == 23


def test_exception_name_lookup():
    """``cob_get_exception_name`` maps a hex code to its EC- name exactly."""
    expected = {
        0xFFFF: "EC-ALL",
        0x0F03: "EC-SCREEN-ITEM-TRUNCATED",
        0x0A02: "EC-OVERFLOW-STRING",
        0x0A03: "EC-OVERFLOW-UNSTRING",
        0x1004: "EC-SIZE-OVERFLOW",
        0x1005: "EC-SIZE-TRUNCATION",
        0x1007: "EC-SIZE-ZERO-DIVIDE",
        0x1202: "EC-STORAGE-NOT-ALLOC",
    }
    for code, name in expected.items():
        assert common.cob_get_exception_name(code) == name, (
            "code 0x%04X should resolve to %s" % (code, name)
        )
    # An unknown / "no exception" code resolves to None (the C sentinel arm).
    assert common.cob_get_exception_name(0x0000) is None


def _id_for_code(code):
    """Return the enum id whose table row carries *code* (or skip)."""
    for entry in _exception_table():
        if _entry_field(entry, _COL_CODE) == code:
            return _entry_field(entry, _COL_ID)
    pytest.skip("no exception-table entry with code 0x%04X" % code)


def test_set_exception_sets_globals():
    """``cob_set_exception(id)`` latches the code and the got-exception flag.

    The argument is the *enum id* (e.g. ``COB_EC_SIZE_OVERFLOW``); the resulting
    :data:`cob_exception_code` is the table *code* (``0x1004``) and
    :data:`cob_got_exception` is latched to ``1``.
    """
    ec_id = getattr(common, "COB_EC_SIZE_OVERFLOW", None)
    if ec_id is None:
        ec_id = _id_for_code(0x1004)

    common.cob_exception_code = 0
    common.cob_got_exception = 0
    common.cob_set_exception(ec_id)

    assert common.cob_exception_code == 0x1004
    assert common.cob_got_exception == 1


def test_critical_flags():
    """The SIZE-error conditions are critical; EC-ALL and SCREEN-TRUNCATED are not.

    The table stores the critical flag as the 4th column.  Guarded with a
    capability check so the test degrades to a skip rather than a false failure
    if a port omits the flag.
    """
    table = _exception_table()
    by_code = {}
    for entry in table:
        # A row must expose at least (code, id, name); the critical flag is the
        # optional 4th column.
        if len(entry) <= _COL_CRIT:
            pytest.skip("exception table does not record the critical flag")
        by_code[_entry_field(entry, _COL_CODE)] = _entry_field(entry, _COL_CRIT)

    # EC-SIZE-OVERFLOW / -TRUNCATION / -ZERO-DIVIDE are critical (1).
    for code in (0x1004, 0x1005, 0x1007):
        assert by_code[code] == 1, "0x%04X must be critical" % code
    # EC-ALL and EC-SCREEN-ITEM-TRUNCATED are non-critical (0).
    for code in (0xFFFF, 0x0F03):
        assert by_code[code] == 0, "0x%04X must be non-critical" % code


# ===========================================================================
# Phase 2 - Field model constants & cob_field
# ===========================================================================
def test_type_constants():
    """The ``COB_TYPE_*`` discriminators match the C ``common.h`` values."""
    assert common.COB_TYPE_UNKNOWN == 0x00
    assert common.COB_TYPE_GROUP == 0x01
    assert common.COB_TYPE_NUMERIC_DISPLAY == 0x10
    assert common.COB_TYPE_NUMERIC_BINARY == 0x11
    assert common.COB_TYPE_NUMERIC_PACKED == 0x12
    assert common.COB_TYPE_NUMERIC_FLOAT == 0x13
    assert common.COB_TYPE_NUMERIC_DOUBLE == 0x14
    assert common.COB_TYPE_NUMERIC_EDITED == 0x24
    assert common.COB_TYPE_ALPHANUMERIC == 0x21
    assert common.COB_TYPE_ALPHANUMERIC_EDITED == 0x23
    # The numeric class bit (0x10) is shared by every numeric USAGE.
    assert common.COB_TYPE_NUMERIC_DISPLAY & common.COB_TYPE_NUMERIC == 0x10


def test_flag_constants():
    """The ``COB_FLAG_*`` and sign-display constants match ``common.h``."""
    assert common.COB_FLAG_HAVE_SIGN == 0x01
    assert common.COB_FLAG_SIGN_SEPARATE == 0x02
    assert common.COB_FLAG_SIGN_LEADING == 0x04
    assert common.COB_FLAG_BLANK_ZERO == 0x08
    assert common.COB_FLAG_JUSTIFIED == 0x10
    assert common.COB_FLAG_BINARY_SWAP == 0x20
    assert common.COB_DISPLAY_SIGN_ASCII == 0
    assert common.COB_DISPLAY_SIGN_EBCDIC == 1


def test_cob_field_construction():
    """A PIC 9(5) DISPLAY field stores mutable bytes and the documented attrs."""
    attr = common.cob_field_attr(
        type=common.COB_TYPE_NUMERIC_DISPLAY, digits=5, scale=0, flags=0,
        pic="9(5)")
    field = common.cob_field(size=5, data=bytearray(b"12345"), attr=attr)

    # ``.data`` must be a mutable bytearray so the runtime can edit it in place
    # (sign overpunch, MOVE, numeric editing) - the storage-aliasing contract.
    assert isinstance(field.data, bytearray)
    assert field.size == 5
    assert bytes(field.data) == b"12345"
    assert field.attr.type == common.COB_TYPE_NUMERIC_DISPLAY
    assert field.attr.digits == 5
    assert field.attr.scale == 0
    assert field.attr.pic == "9(5)"

    # An immutable ``bytes`` literal is copied into a private mutable bytearray
    # so in-place edits never mutate a shared read-only object.
    literal = common.cob_field(size=3, data=b"abc")
    assert isinstance(literal.data, bytearray)
    literal.data[0] = ord("Z")
    assert bytes(literal.data) == b"Zbc"


def test_field_accessor_macros():
    """The ``COB_FIELD_*`` accessors read the attribute fields/flags."""
    flags = (common.COB_FLAG_HAVE_SIGN | common.COB_FLAG_JUSTIFIED)
    field = mk(b"12345", type=common.COB_TYPE_NUMERIC_DISPLAY, digits=5,
               scale=2, flags=flags, pic="9(3)V9(2)")

    assert common.COB_FIELD_TYPE(field) == common.COB_TYPE_NUMERIC_DISPLAY
    assert common.COB_FIELD_DIGITS(field) == 5
    assert common.COB_FIELD_SCALE(field) == 2
    assert common.COB_FIELD_PIC(field) == "9(3)V9(2)"
    assert common.COB_FIELD_HAVE_SIGN(field)
    assert common.COB_FIELD_JUSTIFIED(field)
    assert not common.COB_FIELD_BLANK_ZERO(field)
    # The numeric class bit is set for a DISPLAY numeric.
    assert common.COB_FIELD_IS_NUMERIC(field)


def test_cob_d2i_i2d_roundtrip():
    """``cob_d2i`` / ``cob_i2d`` convert between a digit byte and its int value."""
    for digit in range(10):
        byte = common.cob_i2d(digit)
        assert byte == ord("0") + digit
        assert common.cob_d2i(byte) == digit


def test_field_data_separate_leading_sign_offset():
    """COB_FIELD_DATA/SIZE account for a SEPARATE LEADING sign byte."""
    flags = (common.COB_FLAG_HAVE_SIGN | common.COB_FLAG_SIGN_SEPARATE
             | common.COB_FLAG_SIGN_LEADING)
    # Storage is the sign byte '+' followed by the 3 value digits.
    field = mk(b"+123", type=common.COB_TYPE_NUMERIC_DISPLAY, digits=3,
               flags=flags, size=4)
    # The value bytes begin after the leading sign byte ...
    assert bytes(common.COB_FIELD_DATA(field)) == b"123"
    # ... and the value-byte count excludes the SEPARATE sign byte.
    assert common.COB_FIELD_SIZE(field) == 3


# ===========================================================================
# Phase 3 - Sign overpunch round-trip (ASCII + EBCDIC)
# ===========================================================================
def test_sign_ascii_roundtrip():
    """ASCII overpunch round-trips at both the char and the field level.

    Char level: ``cob_put_sign_ascii`` encodes a digit byte as its negative
    overpunch ('0'..'9' -> 'p'..'y', i.e. ``+0x40``) and ``cob_get_sign_ascii``
    decodes it back.  Field level: ``cob_real_put_sign`` overpunches the last
    digit of a signed zoned-DISPLAY item for a negative value and
    ``cob_real_get_sign`` reads it back, normalising the data byte to a plain
    digit (this ASCII round-trip is the one that MUST run).
    """
    # --- char-level round-trip over every digit -------------------------
    for digit in range(10):
        byte = ord("0") + digit
        overpunch = common.cob_put_sign_ascii(byte)
        assert common.cob_get_sign_ascii(overpunch) == byte
    # '5' (0x35) overpunches to 'u' (0x75); the negative overpunch adds 0x40.
    assert common.cob_put_sign_ascii(ord("5")) == ord("u")

    # --- field-level round-trip on a signed zoned-DISPLAY item ----------
    common.cob_current_module = None  # force the ASCII display-sign path
    field = mk(b"12345", type=common.COB_TYPE_NUMERIC_DISPLAY, digits=5,
               flags=common.COB_FLAG_HAVE_SIGN)

    # Positive: the digit bytes are unchanged and the sign reads back as +1.
    common.cob_real_put_sign(field, 1)
    assert common.cob_real_get_sign(field) == 1
    assert bytes(field.data) == b"12345"

    # Negative: the last digit is overpunched ('5' -> 'u') ...
    common.cob_real_put_sign(field, -1)
    assert field.data[-1] == ord("u")
    # ... and reading the sign back returns -1 and re-normalises the byte.
    assert common.cob_real_get_sign(field) == -1
    assert bytes(field.data) == b"12345"


def test_sign_ebcdic_roundtrip():
    """EBCDIC overpunch round-trips at the char level (positive & negative).

    ``cob_put_sign_ebcdic(digit, sign)`` returns the zoned byte (positive zone
    '0'->'{','1'->'A'..'9'->'I'; negative zone '0'->'}','1'->'J'..'9'->'R') and
    ``cob_get_sign_ebcdic(byte)`` returns the ``(digit_byte, sign)`` pair.  Each
    helper is probed via :func:`getattr` so the test skips gracefully if a port
    renames it.
    """
    put_ebcdic = getattr(common, "cob_put_sign_ebcdic", None)
    get_ebcdic = getattr(common, "cob_get_sign_ebcdic", None)
    if put_ebcdic is None or get_ebcdic is None:
        pytest.skip("EBCDIC sign helpers not exposed under the expected names")

    for digit in range(10):
        byte = ord("0") + digit
        for sign in (1, -1):
            zoned = put_ebcdic(byte, sign)
            decoded_digit, decoded_sign = get_ebcdic(zoned)
            assert decoded_digit == byte
            assert decoded_sign == sign
    # Spot-check the documented zone bytes for digit '5'.
    assert put_ebcdic(ord("5"), 1) == ord("E")
    assert put_ebcdic(ord("5"), -1) == ord("N")


def test_real_sign_packed_decimal():
    """``cob_real_put_sign`` / ``cob_real_get_sign`` set the PACKED sign nibble."""
    # A 3-byte PACKED-DECIMAL holding 123 with a positive (0x0C) sign nibble.
    field = mk(b"\x12\x3c", type=common.COB_TYPE_NUMERIC_PACKED, digits=3,
               flags=common.COB_FLAG_HAVE_SIGN)
    common.cob_real_put_sign(field, -1)
    assert (field.data[-1] & 0x0F) == 0x0D  # negative nibble
    assert common.cob_real_get_sign(field) == -1
    common.cob_real_put_sign(field, 1)
    assert (field.data[-1] & 0x0F) == 0x0C  # positive nibble
    assert common.cob_real_get_sign(field) == 1


def test_cob_get_put_sign_guarded_by_have_sign():
    """``cob_get_sign``/``cob_put_sign`` are no-ops for an unsigned field."""
    unsigned = mk(b"12345", type=common.COB_TYPE_NUMERIC_DISPLAY, digits=5)
    assert common.cob_get_sign(unsigned) == 0
    # Storing a sign into an unsigned item must not corrupt the digits.
    common.cob_put_sign(unsigned, -1)
    assert bytes(unsigned.data) == b"12345"


# ===========================================================================
# Phase 4 - Class tests & comparison
# ===========================================================================
def test_class_numeric_alpha():
    """NUMERIC and ALPHABETIC[-UPPER/-LOWER] class tests follow COBOL rules."""
    # NUMERIC: every value byte of a zoned DISPLAY item must be a digit.
    assert common.cob_is_numeric(
        mk(b"12345", type=common.COB_TYPE_NUMERIC_DISPLAY, digits=5))
    assert not common.cob_is_numeric(
        mk(b"12A45", type=common.COB_TYPE_NUMERIC_DISPLAY, digits=5))

    # ALPHABETIC: letters and spaces only.
    assert common.cob_is_alpha(mk(b"ABC def"))
    assert not common.cob_is_alpha(mk(b"AB12"))

    # ALPHABETIC-UPPER / -LOWER.
    assert common.cob_is_upper(mk(b"ABC XYZ"))
    assert not common.cob_is_upper(mk(b"ABc"))
    assert common.cob_is_lower(mk(b"abc xyz"))
    assert not common.cob_is_lower(mk(b"abC"))


def test_cob_cmp():
    """``cob_cmp`` returns equal->0, greater->positive, less->negative.

    Covers both numeric/numeric comparison (deferred to the numeric subsystem)
    and alphanumeric comparison including the COBOL rule that the shorter
    operand is space-padded.
    """
    # --- numeric comparison ---------------------------------------------
    n_lo = mk(b"00123", type=common.COB_TYPE_NUMERIC_DISPLAY, digits=5)
    n_eq = mk(b"00123", type=common.COB_TYPE_NUMERIC_DISPLAY, digits=5)
    n_hi = mk(b"00456", type=common.COB_TYPE_NUMERIC_DISPLAY, digits=5)
    assert common.cob_cmp(n_lo, n_eq) == 0
    assert common.cob_cmp(n_hi, n_lo) > 0
    assert common.cob_cmp(n_lo, n_hi) < 0

    # --- alphanumeric comparison (equal length) -------------------------
    assert common.cob_cmp(mk(b"ABC"), mk(b"ABC")) == 0
    assert common.cob_cmp(mk(b"ABD"), mk(b"ABC")) > 0
    assert common.cob_cmp(mk(b"ABC"), mk(b"ABD")) < 0

    # --- alphanumeric comparison (unequal length -> space padding) ------
    # "ABC" vs "AB": the shorter "AB" is padded to "AB ", and 'C' > ' '.
    assert common.cob_cmp(mk(b"ABC"), mk(b"AB")) > 0
    assert common.cob_cmp(mk(b"AB"), mk(b"ABC")) < 0
    # Trailing spaces are not significant: "AB" == "AB ".
    assert common.cob_cmp(mk(b"AB"), mk(b"AB ")) == 0


def test_cob_cmp_char_and_all():
    """``cob_cmp_char`` / ``cob_cmp_all`` compare against repeated byte values."""
    field = mk(b"AAAA")
    # Every byte equals 'A' -> 0; less than 'B'; greater than '@'.
    assert common.cob_cmp_char(field, ord("A")) == 0
    assert common.cob_cmp_char(mk(b"AAAA"), ord("B")) < 0
    assert common.cob_cmp_char(mk(b"AAAA"), ord("@")) > 0

    # cob_cmp_all compares a field against a (shorter) repeated literal.
    all_a = mk(b"A")
    assert common.cob_cmp_all(mk(b"AAAA"), all_a) == 0
    assert common.cob_cmp_all(mk(b"AAAB"), all_a) > 0


def test_cob_is_omitted():
    """An OMITTED argument is modelled by ``data is None``."""
    omitted = common.cob_field(size=0, data=None)
    assert common.cob_is_omitted(omitted)
    assert not common.cob_is_omitted(mk(b"X"))


# ===========================================================================
# Phase 5 - Allocation & lifecycle
# ===========================================================================
def test_cob_malloc_zeroed():
    """``cob_malloc(16)`` returns a 16-byte zero-initialised mutable buffer."""
    buf = common.cob_malloc(16)
    assert len(buf) == 16
    assert all(byte == 0 for byte in buf)
    # calloc semantics -> a writable bytearray the runtime can fill in place.
    assert isinstance(buf, bytearray)


def test_allocate_free():
    """ALLOCATE/FREE round-trips; FREE of un-allocated storage sets EC-STORAGE-NOT-ALLOC."""
    allocate = getattr(common, "cob_allocate", None)
    free_alloc = getattr(common, "cob_free_alloc", None)
    if allocate is None or free_alloc is None:
        pytest.skip("cob_allocate / cob_free_alloc not implemented")

    # --- allocate via a numeric size field (16 bytes) -------------------
    size_field = mk(b"16", type=common.COB_TYPE_NUMERIC_DISPLAY, digits=2)
    holder = [None]
    result = allocate(holder, None, size_field)
    assert isinstance(result, bytearray)
    assert len(result) == 16
    assert holder[0] is result

    # --- free the storage we just allocated -----------------------------
    common.cob_exception_code = 0
    free_alloc(holder, None)
    assert holder[0] is None
    assert common.cob_exception_code == 0

    # --- FREE of storage that was never allocated -> EC-STORAGE-NOT-ALLOC
    common.cob_exception_code = 0
    bogus = [bytearray(8)]  # a buffer the allocator never handed out
    free_alloc(bogus, None)
    assert common.cob_exception_code == 0x1202


def test_module_enter_leave():
    """``cob_module_enter`` / ``cob_module_leave`` maintain the active chain."""
    # Pretend the runtime is initialised so enter() does not emit the lazy-init
    # warning path (which would also call the real subsystem initializers).
    common.cob_initialized = 1
    before = common.cob_current_module

    module = common.cob_module()
    common.cob_module_enter(module)
    assert common.cob_current_module is module
    assert module.next is before

    common.cob_module_leave(module)
    assert common.cob_current_module is before


# ===========================================================================
# Phase 6 - cob_init orchestration ORDER
# ===========================================================================
# The canonical subsystem start-up order the C runtime performs (common.c):
# numeric -> strings -> move -> intrinsic -> fileio -> termio -> call.
# screenio is initialised lazily on first ACCEPT/DISPLAY and is therefore NOT
# part of this chain.
_EXPECTED_INIT_ORDER = [
    "numeric", "strings", "move", "intrinsic", "fileio", "termio", "call",
]


def test_cob_init_order(monkeypatch):
    """``cob_init`` runs the subsystem initializers in the exact canonical order.

    Each ``cob_init_<name>`` is monkeypatched to record its name; the package
    facade ``cob_init`` is then invoked and the recorded sequence is asserted to
    be exactly ``numeric->strings->move->intrinsic->fileio->termio->call`` with
    ``screenio`` never initialised.  ``monkeypatch`` restores the originals.
    """
    import importlib

    # The orchestration lives on the facade (preferred) and delegates to common.
    init_fn = getattr(libcob_py, "cob_init", None) or getattr(
        common, "cob_init", None)
    if init_fn is None:
        pytest.skip(
            "cob_init is not present on the package facade or common - it "
            "SHOULD exist on the facade")

    recorded = []

    def _recorder(name):
        return lambda *args, **kwargs: recorded.append(name)

    # Replace each subsystem initializer with a recorder.  raising=False binds
    # the attribute even if a module happens not to define it yet.
    for name in _EXPECTED_INIT_ORDER:
        module = importlib.import_module("libcob_py." + name)
        monkeypatch.setattr(module, "cob_init_" + name, _recorder(name),
                            raising=False)

    # Tripwire: screenio must never be initialised by the cob_init chain.
    try:
        screenio = importlib.import_module("libcob_py.screenio")
        monkeypatch.setattr(screenio, "cob_init_screenio",
                            _recorder("screenio"), raising=False)
    except ImportError:
        pass

    # Reset the idempotency guard so the initializers actually run this call.
    monkeypatch.setattr(common, "cob_initialized", 0, raising=False)

    init_fn([])

    assert recorded == _EXPECTED_INIT_ORDER
    assert "screenio" not in recorded


def test_cob_init_order_matches_published_constant():
    """The module's published ``COB_INIT_ORDER`` equals the canonical sequence."""
    order = getattr(common, "COB_INIT_ORDER", None)
    if order is None:
        pytest.skip("COB_INIT_ORDER not published by the module")
    assert list(order) == _EXPECTED_INIT_ORDER


def test_cob_init_is_idempotent(monkeypatch):
    """A second ``cob_init`` call is a no-op while the runtime stays initialised."""
    import importlib

    calls = []
    for name in _EXPECTED_INIT_ORDER:
        module = importlib.import_module("libcob_py." + name)
        monkeypatch.setattr(module, "cob_init_" + name,
                            lambda *a, **k: calls.append(1), raising=False)

    monkeypatch.setattr(common, "cob_initialized", 0, raising=False)
    common.cob_init([])
    first = len(calls)
    assert first == len(_EXPECTED_INIT_ORDER)
    assert common.cob_initialized == 1

    # Second call: guarded by cob_initialized -> initializers do not run again.
    common.cob_init([])
    assert len(calls) == first


def test_subsystem_initializer_optional_skips_missing(monkeypatch):
    """With ``COB_PY_INIT_OPTIONAL=1`` a genuinely-absent module is skipped."""
    runner = getattr(common, "_run_subsystem_initializers", None)
    if runner is None:
        pytest.skip("_run_subsystem_initializers is not exposed")
    import importlib.util

    env_name = getattr(common, "_INIT_OPTIONAL_ENV", "COB_PY_INIT_OPTIONAL")
    monkeypatch.setenv(env_name, "1")
    # Make every subsystem look absent (find_spec returns None).
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: None)
    # Must complete without raising (the test-mode escape hatch).
    runner()


def test_subsystem_initializer_required_missing_raises(monkeypatch):
    """Without the opt-out, a missing required subsystem is a hard RuntimeError."""
    runner = getattr(common, "_run_subsystem_initializers", None)
    if runner is None:
        pytest.skip("_run_subsystem_initializers is not exposed")
    import importlib.util

    env_name = getattr(common, "_INIT_OPTIONAL_ENV", "COB_PY_INIT_OPTIONAL")
    monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: None)
    with pytest.raises(RuntimeError):
        runner()


# ===========================================================================
# Extras - field<->string and the C buffer primitives
# ===========================================================================
def test_field_to_string_trims_trailing_space_and_nul():
    """``cob_field_to_string`` strips trailing spaces/NULs, keeps embedded ones."""
    assert common.cob_field_to_string(mk(b"HELLO   ")) == "HELLO"
    assert common.cob_field_to_string(mk(b"A B  ")) == "A B"
    assert common.cob_field_to_string(mk(b"WORD\x00\x00")) == "WORD"

    # The optional out-parameter buffer is filled with the NUL-terminated text.
    out = bytearray()
    text = common.cob_field_to_string(mk(b"NAME  "), out)
    assert text == "NAME"
    assert bytes(out) == b"NAME\x00"


def test_cob_memcpy_into_field():
    """``cob_memcpy`` moves alphanumeric bytes into a receiving field."""
    dst = mk(b"........", type=common.COB_TYPE_ALPHANUMERIC)
    common.cob_memcpy(dst, b"ABCD", 4)
    # The copied bytes land at the start of the receiving field.
    assert bytes(dst.data[:4]) == b"ABCD"


def test_mem_primitives():
    """``memcpy`` / ``memmove`` / ``memset`` / ``memcmp`` match the C library."""
    buf = bytearray(b"........")
    assert common.memcpy(memoryview(buf), b"ABCD", 4) is not None
    assert bytes(buf) == b"ABCD...."

    common.memset(memoryview(buf), ord("X"), 2)
    assert bytes(buf) == b"XXCD...."

    common.memmove(memoryview(buf), b"YZ", 2)
    assert bytes(buf[:2]) == b"YZ"

    assert common.memcmp(b"ABC", b"ABC", 3) == 0
    assert common.memcmp(b"ABC", b"ABD", 3) < 0
    assert common.memcmp(b"ABD", b"ABC", 3) > 0
    # The comparison is unsigned (0xFF > 0x01) and zero length is always equal.
    assert common.memcmp(b"\xff", b"\x01", 1) > 0
    assert common.memcmp(b"X", b"Y", 0) == 0


def test_cob_trunc_div():
    """``cob_trunc_div`` truncates toward zero (C ``/``), not toward -inf."""
    assert common.cob_trunc_div(7, 2) == 3
    assert common.cob_trunc_div(-7, 2) == -3
    assert common.cob_trunc_div(7, -2) == -3
    assert common.cob_trunc_div(-7, -2) == 3
    assert common.cob_trunc_div(10, 5) == 2
    # Arbitrary precision (no float) and undefined C div-by-zero -> exception.
    big = 10 ** 40 + 7
    assert common.cob_trunc_div(big, 10) == big // 10
    with pytest.raises(ZeroDivisionError):
        common.cob_trunc_div(1, 0)


# ===========================================================================
# Extras - POINTER items
# ===========================================================================
def test_pointer_get_set_roundtrip():
    """``cob_set_pointer``/``cob_get_pointer`` round-trip an address; NULL is 0."""
    psize = struct.calcsize("P")
    ptr = common.cob_field(size=psize, data=bytearray(psize))

    assert common.cob_set_pointer(ptr, 0x1234) is ptr
    assert common.cob_get_pointer(ptr) == 0x1234
    common.cob_set_pointer(ptr, 0)
    assert common.cob_get_pointer(ptr) == 0

    # PROGRAM-POINTER storage is identical.
    common.cob_set_prog_pointer(ptr, 0xABCD)
    assert common.cob_get_prog_pointer(ptr) == 0xABCD


def test_pointer_set_addr_and_addr_of():
    """``cob_set_addr`` writes an address into a buffer; ``cob_addr_of`` returns a slot."""
    psize = struct.calcsize("P")
    data = bytearray(psize)
    common.cob_set_addr(data, 0x2222)
    assert common.cob_get_pointer(data) == 0x2222

    slot = common.cob_addr_of(bytearray(4))
    assert isinstance(slot, bytearray)
    assert len(slot) == psize
    # A NULL operand yields a zero slot.
    assert common.cob_get_pointer(common.cob_addr_of(None)) == 0


def test_cob_field_set_data_aliases_and_returns_field():
    """``cob_field_set_data`` re-points a field at new storage (aliased) and returns it."""
    field = common.cob_field(size=4, data=bytearray(4))
    new_storage = bytearray(b"WXYZ")
    returned = common.cob_field_set_data(field, new_storage)
    assert returned is field
    assert field.data is new_storage


def test_cob_pointer_manip_up_and_down():
    """``cob_pointer_manip`` adds (addsub=0) / subtracts (addsub=1) an offset."""
    psize = struct.calcsize("P")
    ptr = common.cob_field(size=psize, data=bytearray(psize))
    common.cob_set_pointer(ptr, 1000)
    offset = mk(b"100", type=common.COB_TYPE_NUMERIC_DISPLAY, digits=3)

    common.cob_pointer_manip(ptr, offset, 0)   # SET ... UP BY
    assert common.cob_get_pointer(ptr) == 1100
    common.cob_pointer_manip(ptr, offset, 1)   # SET ... DOWN BY
    assert common.cob_get_pointer(ptr) == 1000


# ===========================================================================
# Extras - implementor switches
# ===========================================================================
def test_switch_get_set():
    """``cob_set_switch`` toggles a switch; out-of-domain flag values are ignored."""
    common.cob_set_switch(0, 1)
    assert common.cob_get_switch(0) == 1
    common.cob_set_switch(0, 0)
    assert common.cob_get_switch(0) == 0
    common.cob_set_switch(2, 1)
    common.cob_set_switch(2, 5)  # neither 0 nor 1 -> ignored
    assert common.cob_get_switch(2) == 1


# ===========================================================================
# Extras - run-time bounds / validity checks (all stop the run on violation)
# ===========================================================================
def test_cob_check_ref_mod_valid_and_invalid(capsys):
    """Valid reference modifications pass; out-of-bounds offset/length stop the run."""
    common.cob_check_ref_mod(1, 3, 5, "ITEM")  # within bounds -> no raise
    with pytest.raises(SystemExit):
        common.cob_check_ref_mod(0, 3, 5, "ITEM")   # offset < 1
    with pytest.raises(SystemExit):
        common.cob_check_ref_mod(1, 10, 5, "ITEM")  # length runs past the end


def test_cob_check_subscript_out_of_range(capsys):
    """An out-of-range subscript raises EC-BOUND-SUBSCRIPT and stops the run."""
    common.cob_check_subscript(3, 1, 5, "TBL")  # in range -> no raise
    with pytest.raises(SystemExit):
        common.cob_check_subscript(9, 1, 5, "TBL")


def test_cob_check_odo_violation(capsys):
    """An OCCURS DEPENDING ON value outside [min, max] stops the run."""
    common.cob_check_odo(3, 1, 5, "ODO")  # in range -> no raise
    with pytest.raises(SystemExit):
        common.cob_check_odo(0, 1, 5, "ODO")


def test_cob_check_numeric_invalid(capsys):
    """A non-numeric value in a numeric item stops the run."""
    bad = mk(b"12X45", type=common.COB_TYPE_NUMERIC_DISPLAY, digits=5)
    with pytest.raises(SystemExit):
        common.cob_check_numeric(bad, "NUM")


def test_cob_check_based_null(capsys):
    """A NULL BASED/LINKAGE address stops the run; a real address passes."""
    common.cob_check_based(bytearray(4), "BSD")  # non-NULL -> no raise
    with pytest.raises(SystemExit):
        common.cob_check_based(None, "BSD")


# ===========================================================================
# Extras - EXTERNAL items
# ===========================================================================
def test_cob_external_addr_shared_storage():
    """The first reference allocates shared storage; later refs return the same."""
    first = common.cob_external_addr("SHARED-ITEM", 8)
    assert isinstance(first, bytearray)
    assert len(first) == 8
    assert common.cob_initial_external == 1  # latched on first allocation

    second = common.cob_external_addr("SHARED-ITEM", 8)
    assert second is first                   # the SAME shared buffer
    assert common.cob_initial_external == 0  # not the initial reference now


# ===========================================================================
# Extras - table SORT
# ===========================================================================
def test_cob_table_sort_ascending():
    """An ascending table SORT orders fixed-size records in place."""
    table = common.cob_field(size=3, data=bytearray(b"CCCAAABBB"))
    key = mk(b"\x00\x00\x00", type=common.COB_TYPE_ALPHANUMERIC)
    common.cob_table_sort_init(1, None)
    common.cob_table_sort_init_key(common.COB_ASCENDING, key, 0)
    common.cob_table_sort(table, 3)
    assert bytes(table.data) == b"AAABBBCCC"


def test_cob_table_sort_descending():
    """A descending table SORT reverses the ordering."""
    table = common.cob_field(size=3, data=bytearray(b"AAACCCBBB"))
    key = mk(b"\x00\x00\x00", type=common.COB_TYPE_ALPHANUMERIC)
    common.cob_table_sort_init(1, None)
    common.cob_table_sort_init_key(common.COB_DESCENDING, key, 0)
    common.cob_table_sort(table, 3)
    assert bytes(table.data) == b"CCCBBBAAA"


# ===========================================================================
# Extras - DATE/TIME ACCEPT
# ===========================================================================
def test_accept_date_and_time_shapes():
    """The DATE/TIME ACCEPT helpers fill digit strings of the documented widths."""
    cases = (
        (common.cob_accept_date, 6),
        (common.cob_accept_date_yyyymmdd, 8),
        (common.cob_accept_day, 5),
        (common.cob_accept_day_yyyyddd, 7),
        (common.cob_accept_time, 8),
    )
    for fn, width in cases:
        field = common.cob_field(size=width, data=bytearray(width))
        fn(field)
        assert len(field.data) == width
        assert field.data.isdigit(), "%s should yield digits" % fn.__name__

    dow = common.cob_field(size=1, data=bytearray(1))
    common.cob_accept_day_of_week(dow)
    assert chr(dow.data[0]) in "1234567"  # 1=Mon .. 7=Sun


# ===========================================================================
# Extras - environment API
# ===========================================================================
def test_environment_api():
    """``cobputenv``/``cobgetenv`` and the field-based SET/ACCEPT ENVIRONMENT."""
    test_keys = ("COB_PY_TEST_ENVVAR", "COB_PY_TEST_FIELDVAR")
    try:
        assert common.cobputenv("COB_PY_TEST_ENVVAR=hello") == 0
        assert common.cobgetenv("COB_PY_TEST_ENVVAR") == "hello"
        # A malformed (no '=') string fails; an unset variable is None.
        assert common.cobputenv("NOEQUALS") == -1
        assert common.cobgetenv("COB_PY_DEFINITELY_NOT_SET_123") is None

        # SET ENVIRONMENT name TO value (field form).
        common.cob_set_environment(
            mk(b"COB_PY_TEST_FIELDVAR", type=common.COB_TYPE_ALPHANUMERIC),
            mk(b"world", type=common.COB_TYPE_ALPHANUMERIC))
        assert os.environ.get("COB_PY_TEST_FIELDVAR") == "world"

        # ACCEPT FROM ENVIRONMENT name (field form) reads it back.
        out = common.cob_field(size=16, data=bytearray(16))
        common.cob_get_environment(
            mk(b"COB_PY_TEST_FIELDVAR", type=common.COB_TYPE_ALPHANUMERIC), out)
        assert bytes(out.data).rstrip(b" \x00") == b"world"
    finally:
        for key in test_keys:
            os.environ.pop(key, None)


# ===========================================================================
# Extras - source location & TRACE
# ===========================================================================
def test_cob_set_location_records_position():
    """``cob_set_location`` records the current program/section/paragraph/line."""
    common.cob_set_location("PROG1", "src.cob", 42, "SECT", "PARA", "MOVE")
    assert common.cob_current_program_id == "PROG1"
    assert common.cob_source_file == "src.cob"
    assert common.cob_source_line == 42
    assert common.cob_current_section == "SECT"
    assert common.cob_current_paragraph == "PARA"
    assert common.cob_source_statement == "MOVE"


def test_ready_reset_trace_toggle():
    """READY/RESET TRACE flip the line-trace flag."""
    common.cob_ready_trace()
    assert common._cob_line_trace == 1
    common.cob_reset_trace()
    assert common._cob_line_trace == 0


# ===========================================================================
# Extras - C$ / ACUCOBOL system helpers physically located in common.c
# ===========================================================================
def test_cob_acuw_getpid():
    """C$GETPID returns this process's id."""
    pid = common.cob_acuw_getpid()
    assert isinstance(pid, int)
    assert pid == os.getpid()


def test_cob_acuw_justify_right_left_centre():
    """C$JUSTIFY right/left/centre-justifies in place (direction gated by NARG>1)."""
    common.cob_call_params = 2  # the direction byte is consulted only when >1

    right = bytearray(b"  ab  ")
    common.cob_acuw_justify(right, b"R")   # any non-L/C byte -> right-justify
    assert bytes(right) == b"    ab"

    left = bytearray(b"  ab  ")
    common.cob_acuw_justify(left, b"L")
    assert bytes(left) == b"ab    "

    centre = bytearray(b"  ab  ")
    common.cob_acuw_justify(centre, b"C")
    assert bytes(centre) == b"  ab  "


def test_cob_return_args_stores_saved_param_count():
    """C$NARG stores the saved CALL parameter count into the first parameter."""
    count_field = mk(b"00", type=common.COB_TYPE_NUMERIC_DISPLAY, digits=2)
    module = common.cob_module(cob_procedure_parameters=[count_field])
    common.cob_current_module = module
    common.cob_save_call_params = 3

    assert common.cob_return_args(None) == 0
    # 3 stored zoned-DISPLAY into the 2-digit field -> b"03".
    assert bytes(count_field.data) == b"03"


def test_cob_parameter_size_without_module_is_zero():
    """C$PARAMSIZE returns 0 when there is no active module/parameters."""
    common.cob_current_module = None
    assert common.cob_parameter_size(None) == 0


# ===========================================================================
# Extras - CHAINING and COMMAND-LINE
# ===========================================================================
def test_cob_chain_setup_pads_argument():
    """CHAINING copies the n-th argument space-padded and updates the param count."""
    common._cob_argv = ["prog", "FIRST", "SECOND"]
    common._cob_argc = 3
    buf = bytearray(10)
    common.cob_chain_setup(buf, 1, 10)
    assert bytes(buf).rstrip(b" ") == b"FIRST"
    assert common.cob_call_params == 2


def test_cob_display_then_accept_command_line():
    """DISPLAY UPON COMMAND-LINE is echoed back by ACCEPT FROM COMMAND-LINE."""
    common._commln = None
    common.cob_display_command_line(mk(b"ARG1 ARG2"))
    dst = common.cob_field(size=9, data=bytearray(9))
    common.cob_accept_command_line(dst)
    assert bytes(dst.data).rstrip(b" ") == b"ARG1 ARG2"


# ===========================================================================
# Extras - run-time diagnostics & shutdown
# ===========================================================================
def test_cob_runtime_error_writes_stderr(capsys):
    """``cob_runtime_error`` formats its message to stderr."""
    common.cob_runtime_error("boom %d", 42)
    captured = capsys.readouterr()
    assert "boom 42" in captured.err


def test_cob_check_version_mismatch_stops_run(capsys):
    """A library version mismatch stops the run."""
    with pytest.raises(SystemExit):
        common.cob_check_version("prog", "0.0.0", 0)


def test_cobtidy_runs_without_exit():
    """``cobtidy`` runs exit handlers and teardown WITHOUT calling ``sys.exit``."""
    assert common.cobtidy() == 0


def test_cobinit_returns_zero(monkeypatch):
    """``cobinit`` initialises the runtime and returns 0."""
    import importlib
    for name in _EXPECTED_INIT_ORDER:
        module = importlib.import_module("libcob_py." + name)
        monkeypatch.setattr(module, "cob_init_" + name,
                            lambda *a, **k: None, raising=False)
    monkeypatch.setattr(common, "cob_initialized", 0, raising=False)
    assert common.cobinit() == 0
