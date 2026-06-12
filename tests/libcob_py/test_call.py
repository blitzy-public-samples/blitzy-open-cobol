"""Unit tests for :mod:`libcob_py.call`.

Exercises the pure-Python dynamic CALL loader that replaces the C runtime's
131-bucket hash plus ``dlopen``/``dlsym`` resolver (``libcob/call.c``) with
:func:`importlib.import_module`, ``sys.path`` manipulation, and module-cache
eviction for CANCEL (AAP 0.3.2 "Dynamic loader" / 0.4.1).  Coverage targets:

* ``cob_encode_program_id`` byte-for-byte program-name encoding,
* ``COB_LOAD_CASE`` folding (``_apply_case``),
* the search path (``cob_set_library_path``) and the call cache
  (``lookup`` / ``insert`` / ``cob_set_cancel``),
* dynamic resolution of a real generated-style module, the not-found path
  (``EC-PROGRAM-NOT-FOUND``) and the aborting ``*_1`` variants,
* field-addressed resolution (``cob_call_resolve`` / ``cob_field_cancel``),
* CANCEL eviction + fresh re-import (``cobcancel``),
* ``cob_init_call`` env wiring and system-routine registration.

Standard library only; ``pytest`` is a development-only framework (AAP 0.5).
"""
import importlib
import os
import sys

import pytest

libcob_py = pytest.importorskip("libcob_py")
common = pytest.importorskip("libcob_py.common")
call = pytest.importorskip("libcob_py.call")
system = pytest.importorskip("libcob_py.system")


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
def strfld(text, size=None):
    data = text.encode("latin-1")
    if size is not None:
        data = data.ljust(size, b" ")
    attr = common.cob_field_attr(
        type=common.COB_TYPE_ALPHANUMERIC, digits=0, scale=0, flags=0, pic=None)
    return common.cob_field(size=len(data), data=bytearray(data), attr=attr)


@pytest.fixture(autouse=True)
def clean_resolver():
    """Reset the resolver caches and case-fold around every test."""
    saved_convert = call.name_convert
    call._call_cache.clear()
    call._cancel_handlers.clear()
    call._resolve_error = None
    call.name_convert = 0
    yield
    call._call_cache.clear()
    call._cancel_handlers.clear()
    call.name_convert = saved_convert


@pytest.fixture
def module_dir(tmp_path, monkeypatch):
    """Put *tmp_path* on sys.path and clean up imported test modules."""
    monkeypatch.syspath_prepend(str(tmp_path))
    created = set(sys.modules)
    yield tmp_path
    for name in set(sys.modules) - created:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()


# ===========================================================================
# Program-name encoding (cb_encode_program_id parity)
# ===========================================================================
def test_encode_plain_alnum():
    assert call.cob_encode_program_id("PROG1") == "PROG1"


def test_encode_leading_digit():
    # A leading digit becomes _HH (uppercase hex of the byte).
    assert call.cob_encode_program_id("1ABC") == "_31ABC"


def test_encode_hyphen_becomes_double_underscore():
    assert call.cob_encode_program_id("MY-PROG") == "MY__PROG"


def test_encode_special_char():
    # '.' (0x2E) -> _2E
    assert call.cob_encode_program_id("A.B") == "A_2EB"


def test_encode_empty():
    assert call.cob_encode_program_id("") == ""


# ===========================================================================
# COB_LOAD_CASE folding
# ===========================================================================
def test_apply_case_default_is_identity():
    call.name_convert = 0
    assert call._apply_case("MixedCase") == "MixedCase"


def test_apply_case_lower():
    call.name_convert = 1
    assert call._apply_case("MixedCase") == "mixedcase"


def test_apply_case_upper():
    call.name_convert = 2
    assert call._apply_case("MixedCase") == "MIXEDCASE"


# ===========================================================================
# Search path + cache primitives
# ===========================================================================
def test_set_library_path_prepends_syspath(tmp_path):
    d1 = str(tmp_path / "one")
    d2 = str(tmp_path / "two")
    os.makedirs(d1, exist_ok=True)
    os.makedirs(d2, exist_ok=True)
    call.cob_set_library_path(d1 + os.pathsep + d2)
    assert d1 in sys.path and d2 in sys.path
    assert call._resolve_paths == [d1, d2]


def test_lookup_insert_roundtrip():
    def entry():
        return 0
    assert call.lookup("X") is None
    call.insert("X", entry)
    assert call.lookup("X") is entry


def test_set_cancel_registers_handler():
    def entry():
        return 0
    def cancel(*a):
        return 0
    call.cob_set_cancel("Y", entry, cancel)
    assert call.lookup("Y") is entry
    assert call._cancel_handlers["Y"] is cancel
    # A second call only refreshes the cancel handler, keeping the entry.
    def cancel2(*a):
        return 0
    call.cob_set_cancel("Y", None, cancel2)
    assert call.lookup("Y") is entry
    assert call._cancel_handlers["Y"] is cancel2


# ===========================================================================
# Dynamic resolution
# ===========================================================================
def _write_module(directory, modname, entry_name):
    """Write a generated-style module exposing an int-returning entry function."""
    path = directory / (modname + ".py")
    path.write_text(
        "def %s(*args):\n    return 0\n" % entry_name
    )
    return path


def test_resolve_imports_module_and_finds_entry(module_dir):
    _write_module(module_dir, "PROG1", "PROG1")
    func = call.cob_resolve("PROG1")
    assert callable(func)
    assert func() == 0
    # Cached on the second resolve.
    assert call.cob_resolve("PROG1") is func


def test_resolve_encoded_name(module_dir):
    # 'MY-PROG' -> module 'MY__PROG' with entry 'MY__PROG'.
    _write_module(module_dir, "MY__PROG", "MY__PROG")
    func = call.cob_resolve("MY-PROG")
    assert callable(func)


def test_resolve_falls_back_to_main(module_dir):
    _write_module(module_dir, "PROG2", "main")
    func = call.cob_resolve("PROG2")
    assert callable(func)


def test_resolve_not_found_sets_exception(module_dir):
    common.cob_exception_code = 0
    func = call.cob_resolve("NOSUCHPROG")
    assert func is None
    assert common.cob_exception_code != 0
    # error message is recorded and cleared on read
    err = call.cob_resolve_error()
    assert err is not None
    assert call.cob_resolve_error() is None


def test_resolve_no_entry_point(module_dir):
    # Module exists but exposes no matching entry / main.
    (module_dir / "PROG3.py").write_text("X = 1\n")
    assert call.cob_resolve("PROG3") is None


def test_resolve_1_aborts_when_missing(module_dir):
    with pytest.raises(SystemExit):
        call.cob_resolve_1("MISSINGPROG")


def test_call_resolve_via_field(module_dir):
    _write_module(module_dir, "FPROG", "FPROG")
    func = call.cob_call_resolve(strfld("FPROG", 16))
    assert callable(func)


def test_call_resolve_1_aborts(module_dir):
    with pytest.raises(SystemExit):
        call.cob_call_resolve_1(strfld("GHOSTPROG", 16))


# ===========================================================================
# CANCEL
# ===========================================================================
def test_cobcancel_runs_handler_and_evicts(module_dir):
    _write_module(module_dir, "CPROG", "CPROG")
    func = call.cob_resolve("CPROG")
    assert func is not None
    flags = {"cancelled": False}

    def cancel(*a):
        flags["cancelled"] = True
        return 0

    call._cancel_handlers["CPROG"] = cancel
    call.cobcancel("CPROG")
    assert flags["cancelled"] is True
    assert call.lookup("CPROG") is None
    assert "CPROG" not in sys.modules


def test_cobcancel_none_name_aborts():
    with pytest.raises(SystemExit):
        call.cobcancel(None)


def test_field_cancel(module_dir):
    _write_module(module_dir, "DPROG", "DPROG")
    call.cob_resolve("DPROG")
    call.cob_field_cancel(strfld("DPROG", 16))
    assert call.lookup("DPROG") is None


# ===========================================================================
# cob_init_call - environment wiring + system-routine registration
# ===========================================================================
def test_init_call_registers_system_routines(monkeypatch):
    monkeypatch.delenv("COB_LOAD_CASE", raising=False)
    monkeypatch.delenv("COB_PRE_LOAD", raising=False)
    monkeypatch.setenv("COB_LIBRARY_PATH", ".")
    call._call_cache.clear()
    call.cob_init_call()
    # Every system routine name should be resolvable from the call cache.
    assert call.lookup("CBL_AND") is system.CBL_AND
    assert call.lookup("C$GETPID") is system.cob_acuw_getpid


def test_init_call_load_case_lower(monkeypatch):
    monkeypatch.setenv("COB_LOAD_CASE", "LOWER")
    monkeypatch.setenv("COB_LIBRARY_PATH", ".")
    monkeypatch.delenv("COB_PRE_LOAD", raising=False)
    call.cob_init_call()
    assert call.name_convert == 1


def test_init_call_load_case_upper(monkeypatch):
    monkeypatch.setenv("COB_LOAD_CASE", "UPPER")
    monkeypatch.setenv("COB_LIBRARY_PATH", ".")
    monkeypatch.delenv("COB_PRE_LOAD", raising=False)
    call.cob_init_call()
    assert call.name_convert == 2


def test_init_call_pre_load(module_dir, monkeypatch):
    _write_module(module_dir, "PRELOADED", "PRELOADED")
    monkeypatch.setenv("COB_PRE_LOAD", "PRELOADED")
    monkeypatch.setenv("COB_LIBRARY_PATH", str(module_dir))
    monkeypatch.delenv("COB_LOAD_CASE", raising=False)
    # Should import the preloaded module without raising.
    call.cob_init_call()
    assert "PRELOADED" in sys.modules


# ===========================================================================
# CALL argument-passing wrappers (BY CONTENT / BY VALUE).
# These materialise the call-argument idioms the emitter lowers from the
# original C union/cast forms (codegen.c output_call).
# ===========================================================================
import sys as _sys


class TestContentInt:
    def test_fits_int_is_4_byte_buffer(self):
        b = call.cob_content_int(5, 1)
        assert isinstance(b, bytearray) and len(b) == 4
        assert int.from_bytes(b, _sys.byteorder) == 5

    def test_not_fits_int_is_8_byte_buffer(self):
        b = call.cob_content_int(5, 0)
        assert len(b) == 8 and int.from_bytes(b, _sys.byteorder) == 5

    def test_negative_twos_complement(self):
        b = call.cob_content_int(-1, 1)
        assert int.from_bytes(b, _sys.byteorder) == 0xFFFFFFFF

    def test_buffer_is_writable_by_reference(self):
        b = call.cob_content_int(0, 1)
        b[0] = 0x7F          # callee may write through (BY REFERENCE)
        assert b[0] == 0x7F


class TestContentBuffer:
    def test_independent_copy(self):
        src = bytearray(b"HELLO")
        c = call.cob_content_buffer(memoryview(src)[0:], 5)
        assert c == b"HELLO" and isinstance(c, bytearray)
        c[0] = ord("J")
        assert src[0] == ord("H")        # BY CONTENT: caller storage untouched

    def test_short_data_nul_padded(self):
        c = call.cob_content_buffer(b"AB", 5)
        assert c == b"AB\x00\x00\x00"

    def test_from_bytes_source(self):
        assert call.cob_content_buffer(b"XYZ", 3) == b"XYZ"


class TestValueInt:
    def test_unsigned_short_wrap(self):
        assert call.cob_value_int(70000, 2, 1) == 70000 & 0xFFFF

    def test_unsigned_negative_wraps(self):
        assert call.cob_value_int(-1, 2, 1) == 0xFFFF

    def test_signed_short_negative(self):
        assert call.cob_value_int(-1, 2, 0) == -1

    def test_signed_byte_sign_extends(self):
        assert call.cob_value_int(0x1FF, 1, 0) == -1     # 0xFF as signed char

    def test_four_byte_passthrough(self):
        assert call.cob_value_int(1234567, 4, 1) == 1234567


class TestValueBuffer:
    def test_two_byte_native_endian(self):
        vb = call.cob_value_buffer(258, 2, 0)
        assert isinstance(vb, bytearray) and len(vb) == 2
        assert int.from_bytes(vb, _sys.byteorder) == 258

    def test_width_respected(self):
        assert len(call.cob_value_buffer(1, 8, 1)) == 8

    def test_negative_twos_complement(self):
        vb = call.cob_value_buffer(-1, 2, 0)
        assert int.from_bytes(vb, _sys.byteorder) == 0xFFFF


# ===========================================================================
# cob_strdup (call.c L165-L175) - independent-copy semantics
# ===========================================================================
class TestStrdup:
    def test_str_returned_unchanged(self):
        # str is immutable; the duplicate is value-equal and still a str.
        result = call.cob_strdup("ABC")
        assert result == "ABC" and isinstance(result, str)

    def test_bytes_yields_mutable_bytearray(self):
        result = call.cob_strdup(b"xyz")
        assert isinstance(result, bytearray) and result == bytearray(b"xyz")
        result[0] = ord("Q")            # the copy is writable (strtok use case)
        assert result == bytearray(b"Qyz")

    def test_bytearray_copy_is_independent(self):
        src = bytearray(b"123")
        result = call.cob_strdup(src)
        result[0] = 0
        assert src == bytearray(b"123")     # source untouched

    def test_memoryview_source(self):
        src = bytearray(b"MVW")
        result = call.cob_strdup(memoryview(src))
        assert isinstance(result, bytearray) and result == bytearray(b"MVW")

    def test_none_passthrough(self):
        assert call.cob_strdup(None) is None

    def test_other_object_normalised_to_str(self):
        assert call.cob_strdup(12345) == "12345"


# ===========================================================================
# cob_get_buff (call.c L204) - reusable, growable scratch buffer
# ===========================================================================
class TestGetBuff:
    def test_returns_zeroed_buffer_of_width(self):
        buf = call.cob_get_buff(8)
        assert len(buf) == 8 and all(x == 0 for x in buf)

    def test_writable(self):
        buf = call.cob_get_buff(4)
        buf[0] = 0x41
        assert buf[0] == 0x41

    def test_smaller_request_rezeroes_window(self):
        big = call.cob_get_buff(16)
        big[0] = 0xFF
        small = call.cob_get_buff(4)         # reuse + re-zero active window
        assert len(small) == 4 and all(x == 0 for x in small)

    def test_grow_enlarges_backing_store(self):
        call.cob_get_buff(4)
        grown = call.cob_get_buff(128)
        assert len(grown) == 128

    def test_negative_treated_as_zero(self):
        assert len(call.cob_get_buff(-5)) == 0


# ===========================================================================
# cobcall / cobfunc (call.c L602 / L638) - call-by-name with argv
# ===========================================================================
@pytest.fixture
def runtime_initialized():
    """Force common.cob_initialized truthy for cobcall/cobfunc, then restore.

    cobcall publishes ``common.cob_call_params`` (the CALL argument count); this
    fixture also saves and restores that global so the value never leaks into
    sibling test modules (e.g. fileio's ``_chk_parms`` count check).
    """
    saved_init = common.cob_initialized
    saved_params = common.cob_call_params
    common.cob_initialized = 1
    yield
    common.cob_initialized = saved_init
    common.cob_call_params = saved_params


def _write_recorder(directory, modname):
    """Write a module whose entry records its args and returns their count."""
    (directory / (modname + ".py")).write_text(
        "CALLS = []\n"
        "def %s(*args):\n"
        "    CALLS.append(args)\n"
        "    return len(args)\n" % modname
    )


def test_cobcall_resolves_and_invokes(module_dir, runtime_initialized):
    _write_recorder(module_dir, "RECPROG")
    rc = call.cobcall("RECPROG", 2, ["A", "B"])
    assert rc == 2                                  # entry returned len(args)
    assert common.cob_call_params == 2              # parameter count published
    assert sys.modules["RECPROG"].CALLS[-1] == ("A", "B")


def test_cobcall_pads_short_argv_with_none(module_dir, runtime_initialized):
    _write_recorder(module_dir, "PADPROG")
    rc = call.cobcall("PADPROG", 3, ["X"])
    assert rc == 3
    assert sys.modules["PADPROG"].CALLS[-1] == ("X", None, None)


def test_cobcall_none_argv(module_dir, runtime_initialized):
    _write_recorder(module_dir, "NILPROG")
    rc = call.cobcall("NILPROG", 0, None)
    assert rc == 0
    assert sys.modules["NILPROG"].CALLS[-1] == ()


def test_cobcall_not_initialized_stops(monkeypatch):
    monkeypatch.setattr(common, "cob_initialized", 0)
    with pytest.raises(SystemExit):
        call.cobcall("ANY", 0, [])


def test_cobcall_bad_argc_stops(runtime_initialized):
    with pytest.raises(SystemExit):
        call.cobcall("ANY", 99, [])
    with pytest.raises(SystemExit):
        call.cobcall("ANY", -1, [])


def test_cobcall_none_name_stops(runtime_initialized):
    with pytest.raises(SystemExit):
        call.cobcall(None, 0, [])


def test_cobfunc_calls_then_cancels(module_dir, runtime_initialized):
    _write_recorder(module_dir, "FUNCPROG")
    rc = call.cobfunc("FUNCPROG", 1, ["Z"])
    assert rc == 1
    # cobfunc cancels after calling: the module is evicted and the cache cleared.
    assert "FUNCPROG" not in sys.modules
    assert call.lookup("FUNCPROG") is None


def test_cobfunc_not_initialized_stops(monkeypatch):
    monkeypatch.setattr(common, "cob_initialized", 0)
    with pytest.raises(SystemExit):
        call.cobfunc("ANY", 0, [])


def test_max_cobcall_parms_constant():
    assert call.COB_MAX_COBCALL_PARMS == 16


# ===========================================================================
# cob_init_call - environment fallback branches (AAP 0.7.2)
# ===========================================================================
def test_init_call_library_path_unset_uses_default(monkeypatch):
    # When COB_LIBRARY_PATH is unset, the search path defaults to "." plus the
    # built-in COB_LIBRARY_PATH (call.c L558-L560).
    monkeypatch.delenv("COB_LIBRARY_PATH", raising=False)
    monkeypatch.delenv("COB_PRE_LOAD", raising=False)
    monkeypatch.delenv("COB_LOAD_CASE", raising=False)
    call.cob_init_call()
    assert "." in call._resolve_paths


def test_init_call_pre_load_missing_module_skipped(module_dir, monkeypatch):
    # A COB_PRE_LOAD entry that cannot be imported is silently skipped, exactly
    # like the C preload loop (call.c L571-L593) - no exception escapes.
    monkeypatch.setenv("COB_PRE_LOAD", "NO_SUCH_PRELOAD_MODULE")
    monkeypatch.setenv("COB_LIBRARY_PATH", str(module_dir))
    monkeypatch.delenv("COB_LOAD_CASE", raising=False)
    call.cob_init_call()                 # must not raise
    assert "NO_SUCH_PRELOAD_MODULE" not in sys.modules
