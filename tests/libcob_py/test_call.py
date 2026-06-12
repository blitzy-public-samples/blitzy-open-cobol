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
import struct as _struct
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
