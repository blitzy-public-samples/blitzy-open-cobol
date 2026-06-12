"""Unit tests for :mod:`libcob_py.common` - the runtime base module.

Covers the field structures and accessor macros, the digit helpers, the
exception-dispatch table (all 146 authoritative ``EC-*`` codes), the
allocation lifecycle, the runtime init/exit sequence (including the *hardened*
subsystem initializer that resolves the review finding on silent ``ImportError``
swallowing), sign handling, comparisons, class tests, switches, runtime checks,
table SORT, date/time ACCEPT, the environment API and the ``C$`` helpers.

The module under test is reached through ``pytest.importorskip`` so the suite
degrades gracefully if the runtime package is unavailable.
"""

import os
import struct
import sys

import pytest

common = pytest.importorskip("libcob_py.common")


# ---------------------------------------------------------------------------
# Field construction helpers (shared convention with test_move.py).
# ---------------------------------------------------------------------------
def mk(data, type=common.COB_TYPE_UNKNOWN, digits=0, scale=0, flags=0,
       size=None, pic=None):
    """Build a cob_field from pre-encoded bytes."""
    payload = bytearray(data)
    attr = common.cob_field_attr(type=type, digits=digits, scale=scale,
                                 flags=flags, pic=pic)
    return common.cob_field(size=len(payload) if size is None else size,
                            data=payload, attr=attr)


def pic(*pairs):
    """Encode a PICTURE as the 5-byte-group wire format (symbol + int32 count)."""
    out = bytearray()
    for sym, count in pairs:
        out.append(ord(sym))
        out += struct.pack("=i", count)
    return bytes(out)


ALNUM = common.COB_TYPE_ALPHANUMERIC
DISP = common.COB_TYPE_NUMERIC_DISPLAY
HAVE_SIGN = common.COB_FLAG_HAVE_SIGN


#: Module-level globals that individual tests below mutate (CALL-parameter
#: count, captured argv, init flag, source-location tracking).  They are part of
#: the shared runtime state, so a leak here would corrupt *other* test files
#: (e.g. fileio's ``_chk_parms`` reads ``common.cob_call_params``).  This autouse
#: fixture snapshots and restores them around every test so the suite stays
#: order-independent.
_GUARDED_GLOBALS = (
    "cob_call_params", "_cob_argc", "_cob_argv", "cob_initialized",
    "cob_current_program_id", "cob_source_file", "cob_source_line",
    "cob_current_section", "cob_current_paragraph", "cob_source_statement",
    "_cob_line_trace", "cob_exception_code", "cob_got_exception",
)


@pytest.fixture(autouse=True)
def _restore_common_globals():
    saved = {name: getattr(common, name, None) for name in _GUARDED_GLOBALS}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(common, name, value)


# ===========================================================================
# 1. Field structures and accessor macros
# ===========================================================================
def test_cob_field_normalises_data_to_bytearray():
    f = common.cob_field(3, b"abc", common.cob_field_attr(ALNUM))
    assert isinstance(f.data, bytearray)
    f2 = common.cob_field(3, "abc", common.cob_field_attr(ALNUM))
    assert f2.data == bytearray(b"abc")
    f3 = common.cob_field(0, None, None)
    assert f3.data is None
    assert isinstance(f3.attr, common.cob_field_attr)


def test_field_accessor_macros():
    f = mk(b"12345", DISP, digits=5, scale=2, flags=HAVE_SIGN, pic=pic(("9", 5)))
    assert common.COB_FIELD_TYPE(f) == DISP
    assert common.COB_FIELD_DIGITS(f) == 5
    assert common.COB_FIELD_SCALE(f) == 2
    assert common.COB_FIELD_PIC(f) == pic(("9", 5))
    assert common.COB_FIELD_HAVE_SIGN(f)
    assert common.COB_FIELD_IS_NUMERIC(f)
    assert common.COB_FIELD_SIZE(f) == 5
    assert bytes(common.COB_FIELD_DATA(f)) == b"12345"


def test_field_data_separate_leading_sign_offset():
    # SEPARATE + LEADING sign: the value bytes start at offset 1.
    flags = HAVE_SIGN | common.COB_FLAG_SIGN_SEPARATE | common.COB_FLAG_SIGN_LEADING
    f = mk(b"+123", DISP, digits=3, flags=flags)
    assert bytes(common.COB_FIELD_DATA(f)) == b"123"
    assert common.COB_FIELD_SIZE(f) == 3  # size 4 minus the separate sign byte


def test_field_flag_predicates():
    f = mk(b"x", ALNUM, flags=common.COB_FLAG_JUSTIFIED | common.COB_FLAG_BLANK_ZERO)
    assert common.COB_FIELD_JUSTIFIED(f)
    assert common.COB_FIELD_BLANK_ZERO(f)
    assert not common.COB_FIELD_IS_POINTER(f)


def test_cob_d2i_i2d_roundtrip():
    for d in range(10):
        assert common.cob_d2i(common.cob_i2d(d)) == d
    assert common.cob_d2i(ord("0")) == 0
    assert common.cob_i2d(9) == ord("9")


# ===========================================================================
# 2. Exception dispatch - all 146 authoritative EC-* codes
# ===========================================================================
def test_exception_table_size_matches_authority():
    # exception.def enumerates 146 COB_EXCEPTION entries; the table is padded
    # with an unused index 0 and a COB_EC_MAX sentinel.
    assert common.COB_EC_MAX == 147
    assert len(common._TAB_CODE) == 148
    assert len(common._TAB_NAME) == 148


def test_screen_item_truncated_code():
    code = common._TAB_CODE[common.COB_EC_SCREEN_ITEM_TRUNCATED]
    assert code == 0x0F03
    assert common.cob_get_exception_name(0x0F03) == "EC-SCREEN-ITEM-TRUNCATED"


def test_every_category_name_is_resolvable():
    # Each of the 22 categories plus the leaf codes must round-trip name<->code
    # for non-zero codes.
    seen = 0
    for idx in range(1, common.COB_EC_MAX):
        code = common._TAB_CODE[idx]
        if code:
            assert common.cob_get_exception_name(code) == common._TAB_NAME[idx]
            seen += 1
    assert seen >= 22  # at least the 22 category codes resolve


def test_cob_set_exception_latches_state():
    common.cob_got_exception = 0
    common.cob_exception_code = 0
    common.cob_set_exception(common.COB_EC_BOUND_SUBSCRIPT)
    assert common.cob_exception_code == \
        common._TAB_CODE[common.COB_EC_BOUND_SUBSCRIPT]
    assert common.cob_got_exception == 1


def test_cob_get_exception_name_unknown_returns_none():
    assert common.cob_get_exception_name(0xDEAD) is None


# ===========================================================================
# 3. Allocation lifecycle
# ===========================================================================
def test_cob_malloc_zero_filled():
    buf = common.cob_malloc(16)
    assert isinstance(buf, bytearray)
    assert len(buf) == 16
    assert buf == bytearray(16)


def test_cob_allocate_and_free():
    dataptr = [None]
    retfld = mk(bytearray(4), common.COB_TYPE_NUMERIC_BINARY, digits=9,
                flags=HAVE_SIGN)
    sizefld = mk(bytearray(4), common.COB_TYPE_NUMERIC_BINARY, digits=9,
                 flags=HAVE_SIGN)
    from libcob_py import move
    move.cob_set_int(sizefld, 32)
    common.cob_allocate(dataptr, retfld, sizefld)
    assert dataptr[0] is not None
    assert len(dataptr[0]) == 32
    # Free the allocation (BASED storage release).
    common.cob_free_alloc(dataptr, None)


# ===========================================================================
# 4. Runtime error / version reporting
# ===========================================================================
def test_cob_runtime_error_writes_stderr(capsys):
    common.cob_runtime_error("boom %d/%s", 7, "x")
    err = capsys.readouterr().err
    assert "boom 7/x" in err


def test_cob_check_version_mismatch_stops_run(capsys):
    # A mismatched package version reports the diagnostic and stops the run
    # (cob_stop_run -> SystemExit), exactly as the C runtime did.
    with pytest.raises(SystemExit):
        common.cob_check_version("PROG", "0.0", 999)
    err = capsys.readouterr().err
    assert "Version mismatch" in err


# ===========================================================================
# 5. Runtime init / exit lifecycle + HARDENED subsystem initializer
#    (resolves common.py MAJOR L1161 - silent ImportError swallowing)
# ===========================================================================
def test_cob_init_full_package_succeeds():
    common.cob_initialized = False
    common.cob_init()
    assert common.cob_initialized == 1
    # Idempotent: a second call is a no-op.
    common.cob_init()
    assert common.cob_initialized == 1


def test_subsystem_initializer_raises_on_missing_required(monkeypatch):
    import importlib.util as iu
    orig = iu.find_spec

    def fake(name, *a, **k):
        if name == "libcob_py.fileio":
            return None  # simulate a genuinely-missing required module
        return orig(name, *a, **k)

    monkeypatch.setattr(iu, "find_spec", fake)
    monkeypatch.delenv(common._INIT_OPTIONAL_ENV, raising=False)
    with pytest.raises(RuntimeError) as exc:
        common._run_subsystem_initializers()
    assert "fileio" in str(exc.value)
    assert "incomplete" in str(exc.value)


def test_subsystem_initializer_test_mode_skips_missing(monkeypatch):
    import importlib.util as iu
    orig = iu.find_spec

    def fake(name, *a, **k):
        if name == "libcob_py.fileio":
            return None
        return orig(name, *a, **k)

    monkeypatch.setattr(iu, "find_spec", fake)
    monkeypatch.setenv(common._INIT_OPTIONAL_ENV, "1")
    # In test-isolation mode the missing module is skipped, no raise.
    common._run_subsystem_initializers()


def test_subsystem_initializer_propagates_inner_import_error(monkeypatch):
    # An ImportError raised *inside* a present module is a real defect and must
    # never be swallowed.
    import importlib
    import importlib.util as iu
    import importlib.machinery as im

    orig_import = importlib.import_module
    orig_find = iu.find_spec

    def fake_import(name, *a, **k):
        if name == "libcob_py.termio":
            raise ImportError("defect inside termio")
        return orig_import(name, *a, **k)

    def fake_find(name, *a, **k):
        if name == "libcob_py.termio":
            return im.ModuleSpec("libcob_py.termio", None)  # present
        return orig_find(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    monkeypatch.setattr(iu, "find_spec", fake_find)
    monkeypatch.delenv(common._INIT_OPTIONAL_ENV, raising=False)
    with pytest.raises(ImportError) as exc:
        common._run_subsystem_initializers()
    assert "defect inside termio" in str(exc.value)


def test_module_enter_leave_chain():
    # cob_module models per-program runtime context; it is pushed/popped by
    # enter/leave to form the active-module chain.
    m1 = common.cob_module()
    m2 = common.cob_module(decimal_point=ord(","), currency_symbol=ord("#"))
    common.cob_module_enter(m1)
    common.cob_module_enter(m2)
    assert common.cob_current_module is m2
    common.cob_module_leave(m2)
    common.cob_module_leave(m1)
    # No exception => the linked-module chain pushed/popped cleanly.


# ===========================================================================
# 6. Sign handling
# ===========================================================================
def test_ascii_sign_helpers():
    assert common.cob_get_sign_ascii(ord("0")) == ord("0")
    # Overpunched negative digit (ASCII 'p' == '0' overpunch) decodes back.
    neg = common.cob_put_sign_ascii(ord("0"))
    assert common.cob_get_sign_ascii(neg) == ord("0")


def test_ebcdic_sign_helpers_roundtrip():
    # cob_put_sign_ebcdic encodes a digit+sign into one overpunch byte;
    # cob_get_sign_ebcdic decodes it back to (digit_byte, sign).
    enc_neg = common.cob_put_sign_ebcdic(ord("5"), -1)
    assert common.cob_get_sign_ebcdic(enc_neg) == (ord("5"), -1)
    enc_pos = common.cob_put_sign_ebcdic(ord("5"), 1)
    assert common.cob_get_sign_ebcdic(enc_pos) == (ord("5"), 1)


def test_real_get_put_sign_display():
    f = mk(b"123", DISP, digits=3, flags=HAVE_SIGN)
    common.cob_real_put_sign(f, -1)
    assert common.cob_real_get_sign(f) == -1
    common.cob_real_put_sign(f, 1)
    assert common.cob_real_get_sign(f) == 1


# ===========================================================================
# 7. cob_field_to_string + cob_memcpy
# ===========================================================================
def test_field_to_string_trims_trailing_space_and_nul():
    f = mk(b"NAME\x00 ", ALNUM)
    assert common.cob_field_to_string(f) == "NAME"
    # With an out-buffer the NUL-terminated result is also written back.
    buf = bytearray()
    common.cob_field_to_string(f, buf)
    assert buf == bytearray(b"NAME\x00")


def test_field_to_string_preserves_embedded():
    f = mk(b"A B", ALNUM)
    assert common.cob_field_to_string(f) == "A B"


def test_cob_memcpy_truncates_and_pads():
    dst = mk(bytearray(5), ALNUM)
    common.cob_memcpy(dst, b"abcdefg", 7)
    assert bytes(dst.data) == b"abcde"  # copy bounded by the destination size


# ===========================================================================
# 8. Comparisons
# ===========================================================================
def test_cob_cmp_alnum_ordering():
    a = mk(b"ABC", ALNUM)
    b = mk(b"ABD", ALNUM)
    assert common.cob_cmp(a, b) < 0
    assert common.cob_cmp(b, a) > 0
    assert common.cob_cmp(a, mk(b"ABC", ALNUM)) == 0


def test_cob_cmp_alnum_unequal_length_space_padded():
    a = mk(b"AB", ALNUM)
    b = mk(b"AB ", ALNUM)
    # Trailing spaces are ignored by the alphanumeric comparison.
    assert common.cob_cmp_alnum(a, b) == 0


def test_cob_cmp_char_fill():
    f = mk(b"AAAA", ALNUM)
    assert common.cob_cmp_char(f, ord("A")) == 0
    assert common.cob_cmp_char(f, ord("B")) < 0


def test_cob_cmp_all_figurative():
    f = mk(b"AAA", ALNUM)
    allf = mk(b"A", ALNUM, flags=0)
    assert common.cob_cmp_all(f, allf) == 0


# ===========================================================================
# 9. Class condition tests
# ===========================================================================
def test_cob_is_numeric_true_false():
    num = mk(b"00123", DISP, digits=5)
    assert common.cob_is_numeric(num)
    bad = mk(b"0012X", DISP, digits=5)
    assert not common.cob_is_numeric(bad)


def test_cob_is_alpha_upper_lower():
    assert common.cob_is_alpha(mk(b"Abc", ALNUM))
    assert not common.cob_is_alpha(mk(b"Ab1", ALNUM))
    assert common.cob_is_upper(mk(b"ABC", ALNUM))
    assert not common.cob_is_upper(mk(b"ABc", ALNUM))
    assert common.cob_is_lower(mk(b"abc", ALNUM))
    assert not common.cob_is_lower(mk(b"abC", ALNUM))


def test_cob_is_omitted():
    assert common.cob_is_omitted(common.cob_field(0, None, None))
    assert not common.cob_is_omitted(mk(b"x", ALNUM))


# ===========================================================================
# 10. Implementor switches
# ===========================================================================
def test_switch_get_set():
    common.cob_set_switch(1, 1)
    assert common.cob_get_switch(1) == 1
    common.cob_set_switch(1, 0)
    assert common.cob_get_switch(1) == 0


# ===========================================================================
# 11. Runtime bounds / class checks
# ===========================================================================
def test_cob_check_ref_mod_valid_and_invalid():
    # Valid reference modification: offset 1, length 3 within size 5.
    common.cob_check_ref_mod(1, 3, 5, "X")
    # Out-of-range reference modification raises EC-BOUND-REF-MOD then stops
    # the run (cob_stop_run -> SystemExit).
    with pytest.raises(SystemExit):
        common.cob_check_ref_mod(4, 5, 5, "X")
    assert common.cob_exception_code == \
        common._TAB_CODE[common.COB_EC_BOUND_REF_MOD]


def test_cob_check_subscript_out_of_range():
    common.cob_check_subscript(2, 1, 5, "TBL")  # in range, no raise
    with pytest.raises(SystemExit):
        common.cob_check_subscript(9, 1, 5, "TBL")
    assert common.cob_exception_code == \
        common._TAB_CODE[common.COB_EC_BOUND_SUBSCRIPT]


def test_cob_check_odo_violation():
    common.cob_check_odo(3, 1, 5, "ODO")  # ok
    with pytest.raises(SystemExit):
        common.cob_check_odo(7, 1, 5, "ODO")


def test_cob_check_numeric_invalid():
    bad = mk(b"12X45", DISP, digits=5)
    with pytest.raises(SystemExit):
        common.cob_check_numeric(bad, "FLD")


# ===========================================================================
# 12. EXTERNAL data items
# ===========================================================================
def test_cob_external_addr_shared_storage():
    a = common.cob_external_addr("SHARED", 64)
    b = common.cob_external_addr("SHARED", 64)
    assert a is b
    assert len(a) == 64


# ===========================================================================
# 13. Table SORT
# ===========================================================================
def test_cob_table_sort_ascending():
    # Four 2-byte records held back-to-back; f.size is one *record's* size and
    # the key spans the whole 2-byte record at offset 0.
    data = bytearray(b"DDBBCCAA")
    f = common.cob_field(2, data, common.cob_field_attr(ALNUM))
    common.cob_table_sort_init(1, None)
    keyf = common.cob_field(2, bytearray(2), common.cob_field_attr(ALNUM))
    common.cob_table_sort_init_key(common.COB_ASCENDING, keyf, 0)
    common.cob_table_sort(f, 4)
    assert bytes(f.data) == b"AABBCCDD"


# ===========================================================================
# 14. Date / time ACCEPT
# ===========================================================================
def test_accept_date_and_time_shapes():
    d = mk(bytearray(6), DISP, digits=6)
    common.cob_accept_date(d)
    assert bytes(d.data).isdigit()
    dy = mk(bytearray(5), DISP, digits=5)
    common.cob_accept_day(dy)
    assert bytes(dy.data).isdigit()
    t = mk(bytearray(8), DISP, digits=8)
    common.cob_accept_time(t)
    assert bytes(t.data).isdigit()
    dow = mk(bytearray(1), DISP, digits=1)
    common.cob_accept_day_of_week(dow)
    assert bytes(dow.data) in [str(i).encode() for i in range(1, 8)]


def test_accept_date_yyyymmdd_and_day_yyyyddd():
    d = mk(bytearray(8), DISP, digits=8)
    common.cob_accept_date_yyyymmdd(d)
    assert bytes(d.data).isdigit() and len(d.data) == 8
    dy = mk(bytearray(7), DISP, digits=7)
    common.cob_accept_day_yyyyddd(dy)
    assert bytes(dy.data).isdigit() and len(dy.data) == 7


# ===========================================================================
# 15. Environment API
# ===========================================================================
def test_cobputenv_cobgetenv_roundtrip():
    common.cobputenv("BLITZY_TESTVAR=hello")
    assert common.cobgetenv("BLITZY_TESTVAR") == "hello"
    os.environ.pop("BLITZY_TESTVAR", None)


def test_cob_set_get_environment_fields():
    name = mk(b"BLITZY_ENV2", ALNUM)
    val = mk(b"world", ALNUM)
    common.cob_set_environment(name, val)
    out = mk(bytearray(16), ALNUM)
    common.cob_get_environment(name, out)
    assert bytes(out.data).rstrip(b" \x00").startswith(b"world")
    os.environ.pop("BLITZY_ENV2", None)


# ===========================================================================
# 16. C$ / CBL helper routines hosted in common
# ===========================================================================
def test_cob_acuw_getpid():
    assert common.cob_acuw_getpid() == os.getpid()


def test_cob_parameter_size_and_return_args():
    # With no active CALL frame these report zero-ish values without raising.
    pf = mk(bytearray(4), common.COB_TYPE_NUMERIC_BINARY, digits=9, flags=HAVE_SIGN)
    common.cob_return_args(pf)
    from libcob_py import move
    assert move.cob_get_int(pf) >= 0


def test_cob_acuw_justify_right_left_centre():
    # C$JUSTIFY operates in place on a bytearray; direction is gated by
    # cob_call_params > 1.
    common.cob_call_params = 2
    # Right (default direction byte 'R' is not L/C => right-justify).
    buf = bytearray(b"  ab  ")
    common.cob_acuw_justify(buf, b"R")
    assert buf == bytearray(b"    ab")
    # Left.
    buf = bytearray(b"  ab  ")
    common.cob_acuw_justify(buf, b"L")
    assert buf == bytearray(b"ab    ")
    # Centre.
    buf = bytearray(b"  ab  ")
    common.cob_acuw_justify(buf, b"C")
    assert buf == bytearray(b"  ab  ")


def test_cob_set_location_records_position():
    common.cob_set_location("PROG1", "src.cob", 42, "SEC", "PARA", "MOVE")
    assert common.cob_current_program_id == "PROG1"
    assert common.cob_source_line == 42
    assert common.cob_source_statement == "MOVE"


def test_cob_chain_setup_pads_argument():
    common._cob_argv = ["prog", "FIRST", "SECOND"]
    common._cob_argc = 3
    buf = bytearray(10)
    common.cob_chain_setup(buf, 1, 10)
    assert bytes(buf).rstrip() == b"FIRST"
    assert common.cob_call_params == 2


def test_cob_display_then_accept_command_line():
    src = mk(b"HELLO ARGS", ALNUM)
    common.cob_display_command_line(src)
    dst = mk(bytearray(10), ALNUM)
    common.cob_accept_command_line(dst)
    assert bytes(dst.data) == b"HELLO ARGS"


def test_cob_accept_arg_number_and_value():
    common._cob_argv = ["prog", "A1", "A2"]
    common._cob_argc = 3
    n = mk(bytearray(4), common.COB_TYPE_NUMERIC_BINARY, digits=9, flags=HAVE_SIGN)
    common.cob_accept_arg_number(n)
    from libcob_py import move
    assert move.cob_get_int(n) == 2


def test_ready_reset_trace_toggle():
    common.cob_ready_trace()
    assert common._cob_line_trace == 1
    common.cob_reset_trace()
    assert common._cob_line_trace == 0


def test_cob_is_numeric_packed_and_binary():
    # PACKED-DECIMAL +123 (BCD 0x12 0x3C) is numeric.
    pk = mk(b"\x12\x3c", common.COB_TYPE_NUMERIC_PACKED, digits=3, flags=HAVE_SIGN)
    assert common.cob_is_numeric(pk)
    # A binary item is always numeric.
    from libcob_py import move
    b = mk(bytearray(4), common.COB_TYPE_NUMERIC_BINARY, digits=9, flags=HAVE_SIGN)
    move.cob_set_int(b, 17)
    assert common.cob_is_numeric(b)


def test_cob_get_pointer_decodes_address_bytes():
    # cob_get_pointer mirrors the C ``memcpy(&tmptr, f->data, sizeof(void*))``:
    # it decodes the sizeof(void*) address bytes of the item as a native-endian
    # integer (the value output_integer needs), NOT the field object.
    addr = 0x1122334455667788 & ((1 << (struct.calcsize("P") * 8)) - 1)
    raw = addr.to_bytes(struct.calcsize("P"), sys.byteorder)
    fld = common.cob_field(size=len(raw), data=bytearray(raw),
                           attr=common.cob_field_attr(type=common.COB_TYPE_NUMERIC_BINARY))
    assert common.cob_get_pointer(fld) == addr
    assert common.cob_get_prog_pointer(fld) == addr
    # None / a raw buffer are both accepted.
    assert common.cob_get_pointer(None) == 0
    assert common.cob_get_pointer(memoryview(bytearray(raw))) == addr


def test_cobtidy_runs_without_exit():
    # cobtidy runs exit handlers + teardown but does NOT raise SystemExit.
    assert common.cobtidy() == 0


# ===========================================================================
# cob_pointer_manip - SET pointer UP/DOWN BY n
#
# Runtime home of the C backend's emitted static helper (codegen.c gen_ptrmanip):
#   memcpy(&tmptr, f1->data, sizeof(void*)); tmptr +=/-= cob_get_int(f2);
#   memcpy(f1->data, &tmptr, sizeof(void*));
# The front-end (typeck.c L2660/L2702) emits common.cob_pointer_manip(f1, f2,
# flag) with flag=0 (UP/add, cb_int0) or flag=1 (DOWN/subtract, cb_int1).
# ===========================================================================
_PTR_W = struct.calcsize("P")


def _ptr_field(addr):
    """A USAGE POINTER field holding *addr* in native byte order."""
    return common.cob_field(
        size=_PTR_W,
        data=bytearray(int(addr).to_bytes(_PTR_W, sys.byteorder)),
        attr=common.cob_field_attr(type=common.COB_TYPE_NUMERIC_BINARY),
    )


def _ptr_value(f):
    return int.from_bytes(bytes(f.data[:_PTR_W]), sys.byteorder)


def _amount_field(n):
    """A small numeric DISPLAY field carrying the magnitude *n*."""
    s = str(n).encode("latin-1")
    return common.cob_field(
        size=len(s), data=bytearray(s),
        attr=common.cob_field_attr(type=DISP, digits=len(s)))


class TestPointerManip:
    def test_add_increments_pointer(self):
        pf = _ptr_field(0x1000)
        common.cob_pointer_manip(pf, _amount_field(16), 0)   # addsub=0 -> UP
        assert _ptr_value(pf) == 0x1010

    def test_subtract_decrements_pointer(self):
        pf = _ptr_field(0x1010)
        common.cob_pointer_manip(pf, _amount_field(16), 1)   # addsub=1 -> DOWN
        assert _ptr_value(pf) == 0x1000

    def test_add_then_subtract_round_trips(self):
        pf = _ptr_field(0x2000)
        amt = _amount_field(255)
        common.cob_pointer_manip(pf, amt, 0)
        common.cob_pointer_manip(pf, amt, 1)
        assert _ptr_value(pf) == 0x2000

    def test_subtract_below_zero_wraps_pointer_width(self):
        # C unsigned char* wraps modulo 2**(8*sizeof(void*)).
        pf = _ptr_field(0)
        common.cob_pointer_manip(pf, _amount_field(1), 1)
        assert _ptr_value(pf) == (1 << (_PTR_W * 8)) - 1


# ===========================================================================
# C standard-library buffer primitives: memcpy / memmove / memset / memcmp.
#
# The front-end lowers small fixed-size MOVE/compare ops directly to these C
# names (typeck.c L2471/L2505/L4510/L4524/...); the Python emitter routes the
# bare names to common.* and passes memoryview slices (cast-address) and ints
# (cast-length).  These must be byte-exact re-implementations of the C library.
# ===========================================================================
class TestMemcpy:
    def test_copies_bytes_into_memoryview_slice(self):
        back = bytearray(b"\x00" * 10)
        dst = memoryview(back)[2:]            # mimics output_data: memoryview(b)[off:]
        assert common.memcpy(dst, b"ABCDE", 5) is dst
        assert bytes(back) == b"\x00\x00ABCDE\x00\x00\x00"

    def test_copies_from_memoryview_source(self):
        back = bytearray(b"......")
        src = bytearray(b"XYZ")
        common.memcpy(memoryview(back)[0:], memoryview(src)[0:], 3)
        assert bytes(back[:3]) == b"XYZ"

    def test_zero_length_is_noop(self):
        back = bytearray(b"abc")
        common.memcpy(memoryview(back)[0:], b"ZZZ", 0)
        assert bytes(back) == b"abc"

    def test_partial_length(self):
        back = bytearray(b"12345")
        common.memcpy(memoryview(back)[0:], b"AB", 2)
        assert bytes(back) == b"AB345"

    def test_into_bytearray_directly(self):
        back = bytearray(b"....")
        common.memcpy(back, b"WXYZ", 4)
        assert bytes(back) == b"WXYZ"


class TestMemmove:
    def test_overlap_safe_forward(self):
        # Overlapping copy: shift "ABCDE" right by one within the same buffer.
        back = bytearray(b"ABCDE\x00")
        common.memmove(memoryview(back)[1:], memoryview(back)[0:], 5)
        assert bytes(back) == b"AABCDE"

    def test_basic_copy(self):
        back = bytearray(b"\x00\x00\x00")
        common.memmove(memoryview(back)[0:], b"QRS", 3)
        assert bytes(back) == b"QRS"


class TestMemset:
    def test_fills_bytes(self):
        buf = bytearray(b"123456")
        assert common.memset(memoryview(buf)[1:], ord("0"), 4) is not None
        assert bytes(buf) == b"100006"

    def test_value_masked_modulo_256(self):
        buf = bytearray(b"......")
        common.memset(memoryview(buf)[0:], 0x100 + ord(" "), 6)   # 0x120 & 0xFF == space
        assert bytes(buf) == b"      "

    def test_zero_length_is_noop(self):
        buf = bytearray(b"abc")
        common.memset(memoryview(buf)[0:], ord("Z"), 0)
        assert bytes(buf) == b"abc"

    def test_fills_with_zero_byte(self):
        buf = bytearray(b"\xff\xff\xff")
        common.memset(buf, 0, 3)
        assert bytes(buf) == b"\x00\x00\x00"


class TestMemcmp:
    def test_equal(self):
        assert common.memcmp(b"ABC", b"ABC", 3) == 0

    def test_less_than(self):
        assert common.memcmp(b"ABC", b"ABD", 3) < 0

    def test_greater_than(self):
        assert common.memcmp(b"ABZ", b"ABD", 3) > 0

    def test_only_first_size_bytes_compared(self):
        # Differ only at index 2, but compare just the first 2 bytes -> equal.
        assert common.memcmp(memoryview(bytearray(b"AAA"))[0:], b"AAB", 2) == 0

    def test_zero_size_is_equal(self):
        assert common.memcmp(b"X", b"Y", 0) == 0

    def test_high_byte_unsigned_compare(self):
        # 0xFF must compare greater than 0x01 (unsigned char semantics).
        assert common.memcmp(b"\xff", b"\x01", 1) > 0
        assert common.memcmp(b"\x01", b"\xff", 1) < 0


# ===========================================================================
# cob_trunc_div - C truncate-toward-zero integer division
# (emitter routes the integer '/' operator here; codegen.c output_integer).
# ===========================================================================
class TestTruncDiv:
    def test_positive_exact(self):
        assert common.cob_trunc_div(6, 2) == 3

    def test_positive_truncates(self):
        assert common.cob_trunc_div(7, 2) == 3   # not 3.5

    def test_negative_dividend_truncates_toward_zero(self):
        # C: -7 / 2 == -3 (Python // would give -4).
        assert common.cob_trunc_div(-7, 2) == -3

    def test_negative_divisor_truncates_toward_zero(self):
        assert common.cob_trunc_div(7, -2) == -3

    def test_both_negative(self):
        assert common.cob_trunc_div(-7, -2) == 3

    def test_large_arbitrary_precision(self):
        a = 10 ** 40 + 7
        assert common.cob_trunc_div(a, 3) == a // 3   # both positive -> agree
        assert common.cob_trunc_div(-a, 3) == -(a // 3)

    def test_divide_by_zero_raises(self):
        with pytest.raises(ZeroDivisionError):
            common.cob_trunc_div(5, 0)


# ===========================================================================
# Pointer SET helpers and BASED/LINKAGE data re-pointing.
#   cob_set_pointer / cob_set_prog_pointer  - SET p TO ...
#   cob_set_addr                            - SET ADDRESS OF x TO ...
#   cob_addr_of                             - &data (pointer-to-pointer)
#   cob_field_set_data                      - (f->data = data, &f)
# ===========================================================================
class TestPointerSetters:
    def test_set_pointer_round_trips_int_address(self):
        fld = _ptr_field(0)
        ret = common.cob_set_pointer(fld, 0x4000)
        assert ret is fld                           # returns the field
        assert common.cob_get_pointer(fld) == 0x4000

    def test_set_pointer_null(self):
        fld = _ptr_field(0x1234)
        common.cob_set_pointer(fld, None)
        assert common.cob_get_pointer(fld) == 0

    def test_set_pointer_from_buffer_is_stable(self):
        # SET p TO ADDRESS OF x: a buffer value yields a stable synthetic address
        # (id-based) that round-trips through storage.
        fld = _ptr_field(0)
        buf = bytearray(b"data")
        common.cob_set_pointer(fld, buf)
        assert common.cob_get_pointer(fld) == (id(buf) & ((1 << (_PTR_W * 8)) - 1))

    def test_set_prog_pointer_round_trips(self):
        fld = _ptr_field(0)
        common.cob_set_prog_pointer(fld, 0x9999)
        assert common.cob_get_prog_pointer(fld) == 0x9999

    def test_set_addr_writes_address_into_buffer(self):
        # SET ADDRESS OF x TO val writes sizeof(void*) bytes into x's storage.
        back = bytearray(_PTR_W + 4)
        common.cob_set_addr(memoryview(back)[0:], 0xABCD)
        assert int.from_bytes(bytes(back[:_PTR_W]), sys.byteorder) == 0xABCD

    def test_addr_of_returns_pointer_width_buffer(self):
        buf = bytearray(b"hello")
        slot = common.cob_addr_of(buf)
        assert isinstance(slot, bytearray) and len(slot) == _PTR_W
        # holds the synthetic address of buf
        assert int.from_bytes(bytes(slot), sys.byteorder) == (
            id(buf) & ((1 << (_PTR_W * 8)) - 1))

    def test_addr_of_none_is_zero(self):
        slot = common.cob_addr_of(None)
        assert int.from_bytes(bytes(slot), sys.byteorder) == 0

    def test_field_set_data_aliases_and_returns_field(self):
        fld = common.cob_field(size=4, data=bytearray(b"\x00\x00\x00\x00"),
                               attr=common.cob_field_attr(type=ALNUM))
        backing = bytearray(b"WXYZ")
        view = memoryview(backing)[0:]
        ret = common.cob_field_set_data(fld, view)
        assert ret is fld                           # returns the field
        # aliasing: mutating through the field reaches the original backing store
        fld.data[0] = ord("A")
        assert backing[:1] == b"A"
