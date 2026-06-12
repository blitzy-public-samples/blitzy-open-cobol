"""Unit tests for :mod:`libcob_py.system`.

Exercises the pure-Python port of the COBOL system service routines defined by
the authoritative ``libcob/system.def`` dispatch table (the ``CBL_*`` / ``C$``
families plus ``SYSTEM``) and implemented in ``libcob/common.c`` and
``libcob/fileio.c``.  Coverage targets (AAP 0.4.1 / 0.7.1 >=80% per module):

* the 43-row :data:`SYSTEM_TABLE` dispatch contract (external -> internal name,
  declared parameter counts, the high-bit ``\\221``/``\\364``/``\\365`` rows),
* the bitwise logical family (AND/OR/NOR/XOR/IMP/NIMP/EQ/NOT, XF4/XF5, X91),
* case conversion (CBL_TOUPPER / CBL_TOLOWER, letters only),
* the low-level file-handle API (open/create/read/write/close/flush/delete/copy)
  and directory operations, plus the ACUCOBOL C$ wrappers,
* the call-frame helpers (C$NARG / C$PARAMSIZE / C$GETPID / C$SLEEP / C$JUSTIFY)
  and SYSTEM command execution,
* exit/error handler (de)registration.

Standard library only; ``pytest`` is a development-only framework (AAP 0.5).
"""
import os
import struct
import sys

import pytest

libcob_py = pytest.importorskip("libcob_py")
common = pytest.importorskip("libcob_py.common")
move = pytest.importorskip("libcob_py.move")
system = pytest.importorskip("libcob_py.system")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def alnum(text, size=None):
    """Build an ALPHANUMERIC cob_field from *text* (optionally blank-padded)."""
    data = text.encode("latin-1") if isinstance(text, str) else bytes(text)
    if size is not None:
        data = data.ljust(size, b" ")
    attr = common.cob_field_attr(
        type=common.COB_TYPE_ALPHANUMERIC, digits=0, scale=0, flags=0, pic=None)
    return common.cob_field(size=len(data), data=bytearray(data), attr=attr)


def numfld(value, size):
    """Build a zoned NUMERIC DISPLAY cob_field of *size* digits."""
    data = ("%0*d" % (size, value)).encode("latin-1")
    attr = common.cob_field_attr(
        type=common.COB_TYPE_NUMERIC_DISPLAY, digits=size, scale=0, flags=0, pic=None)
    return common.cob_field(size=len(data), data=bytearray(data), attr=attr)


@pytest.fixture
def frame():
    """Install a fresh current-module frame with empty procedure parameters."""
    saved = common.cob_current_module
    saved_params = common.cob_call_params
    module = common.cob_module(cob_procedure_parameters=[None] * 8)
    common.cob_current_module = module
    common.cob_call_params = 5
    common.cob_save_call_params = 5
    yield module
    common.cob_current_module = saved
    common.cob_call_params = saved_params


# ===========================================================================
# Dispatch table - libcob/system.def authority
# ===========================================================================
def test_system_table_has_43_rows():
    assert len(system.SYSTEM_TABLE) == 43
    assert len(system.SYSTEM_DEF) == 43


def test_every_internal_name_is_resolvable():
    # call.py resolves each routine via getattr(system, internal_name).
    for internal in set(system.SYSTEM_TABLE.values()):
        assert hasattr(system, internal), internal
        assert callable(getattr(system, internal))


def test_table_mappings_match_def():
    assert system.SYSTEM_TABLE["SYSTEM"] == "SYSTEM"
    assert system.SYSTEM_TABLE["C$CHDIR"] == "cob_acuw_chdir"
    assert system.SYSTEM_TABLE["C$NARG"] == "cob_return_args"
    assert system.SYSTEM_TABLE["C$PARAMSIZE"] == "cob_parameter_size"
    # Aliases that share an internal implementation.
    assert system.SYSTEM_TABLE["C$TOUPPER"] == "CBL_TOUPPER"
    assert system.SYSTEM_TABLE["C$TOLOWER"] == "CBL_TOLOWER"


def test_high_bit_external_names_present():
    assert "\221" in system.SYSTEM_TABLE
    assert "\364" in system.SYSTEM_TABLE
    assert "\365" in system.SYSTEM_TABLE
    assert system.SYSTEM_TABLE["\221"] == "CBL_X91"
    assert system.SYSTEM_TABLE["\364"] == "CBL_XF4"
    assert system.SYSTEM_TABLE["\365"] == "CBL_XF5"


def test_param_counts_match_def():
    assert system.SYSTEM_PARAM_COUNT["CBL_AND"] == 3
    assert system.SYSTEM_PARAM_COUNT["CBL_NOT"] == 2
    assert system.SYSTEM_PARAM_COUNT["CBL_CREATE_FILE"] == 5
    assert system.SYSTEM_PARAM_COUNT["C$GETPID"] == 0


# ===========================================================================
# Bitwise logical routines
# ===========================================================================
def test_cbl_and():
    a = bytearray(b"\xf0\x0f\xaa")
    b = bytearray(b"\x0f\xf0\x55")
    assert system.CBL_AND(memoryview(a), memoryview(b), 3) == 0
    assert bytes(b) == b"\x00\x00\x00"


def test_cbl_or():
    a = bytearray(b"\xf0\x0f\xaa")
    b = bytearray(b"\x0f\xf0\x55")
    system.CBL_OR(memoryview(a), memoryview(b), 3)
    assert bytes(b) == b"\xff\xff\xff"


def test_cbl_nor():
    a = bytearray(b"\xf0")
    b = bytearray(b"\x0f")
    system.CBL_NOR(memoryview(a), memoryview(b), 1)
    assert bytes(b) == b"\x00"  # ~(0xf0 | 0x0f) = ~0xff = 0x00


def test_cbl_xor():
    a = bytearray(b"\xff\x00")
    b = bytearray(b"\x0f\x0f")
    system.CBL_XOR(memoryview(a), memoryview(b), 2)
    assert bytes(b) == b"\xf0\x0f"


def test_cbl_imp():
    a = bytearray(b"\xf0")
    b = bytearray(b"\x0f")
    system.CBL_IMP(memoryview(a), memoryview(b), 1)
    assert bytes(b) == bytes([((~0xf0) | 0x0f) & 0xFF])  # 0x1f


def test_cbl_nimp():
    a = bytearray(b"\xf0")
    b = bytearray(b"\x0f")
    system.CBL_NIMP(memoryview(a), memoryview(b), 1)
    assert bytes(b) == bytes([(0xf0 & (~0x0f)) & 0xFF])  # 0xf0


def test_cbl_eq():
    a = bytearray(b"\xaa")
    b = bytearray(b"\xaa")
    system.CBL_EQ(memoryview(a), memoryview(b), 1)
    assert bytes(b) == b"\xff"  # ~(x ^ x) = 0xff


def test_cbl_not():
    a = bytearray(b"\x0f\xf0")
    system.CBL_NOT(memoryview(a), 2)
    assert bytes(a) == b"\xf0\x0f"


def test_bitwise_nonpositive_length_is_noop():
    a = bytearray(b"\x12")
    b = bytearray(b"\x34")
    system.CBL_AND(memoryview(a), memoryview(b), 0)
    assert bytes(b) == b"\x34"
    system.CBL_NOT(memoryview(a), -1)
    assert bytes(a) == b"\x12"


def test_accepts_cob_field_and_int_length():
    # Routines coerce cob_field operands and numeric-field lengths.
    f1 = alnum("\xff\x00")
    f2 = alnum("\x0f\x0f")
    system.CBL_XOR(f1, f2, numfld(2, 4))
    assert bytes(f2.data) == b"\xf0\x0f"


# ===========================================================================
# XF4 / XF5 / X91
# ===========================================================================
def test_xf4_packs_low_bits():
    src = bytearray([1, 0, 1, 0, 1, 0, 1, 0])
    one = bytearray(1)
    system.CBL_XF4(memoryview(one), memoryview(src))
    assert one[0] == 0b10101010


def test_xf5_unpacks_bits():
    one = bytearray([0b10101010])
    out = bytearray(8)
    system.CBL_XF5(memoryview(one), memoryview(out))
    assert list(out) == [1, 0, 1, 0, 1, 0, 1, 0]


def test_x91_set_get_switches():
    for i in range(8):
        common.cob_set_switch(i, 0)
    res = bytearray(1)
    system.CBL_X91(memoryview(res), memoryview(bytearray([11])),
                   memoryview(bytearray([1, 0, 1, 1, 0, 0, 0, 0])))
    assert res[0] == 0
    assert [common.cob_get_switch(i) for i in range(4)] == [1, 0, 1, 1]
    got = bytearray(8)
    system.CBL_X91(memoryview(res), memoryview(bytearray([12])), memoryview(got))
    assert list(got[:4]) == [1, 0, 1, 1]


def test_x91_param_count_and_bad_func():
    common.cob_save_call_params = 7
    res = bytearray(1)
    pc = bytearray(1)
    system.CBL_X91(memoryview(res), memoryview(bytearray([16])), memoryview(pc))
    assert pc[0] == 7 and res[0] == 0
    system.CBL_X91(memoryview(res), memoryview(bytearray([99])), memoryview(pc))
    assert res[0] == 1


# ===========================================================================
# Case conversion
# ===========================================================================
def test_toupper_letters_only():
    s = bytearray(b"aB3z!")
    system.CBL_TOUPPER(memoryview(s), 5)
    assert bytes(s) == b"AB3Z!"


def test_tolower_letters_only():
    s = bytearray(b"aB3z!")
    system.CBL_TOLOWER(memoryview(s), 5)
    assert bytes(s) == b"ab3z!"


# ===========================================================================
# File-handle API
# ===========================================================================
def test_file_create_write_read_close(tmp_path, frame):
    os.chdir(tmp_path)
    name = alnum("data.bin", 32)
    frame.cob_procedure_parameters[0] = name
    handle = bytearray(4)
    rc = system.CBL_CREATE_FILE(memoryview(name.data), memoryview(bytearray([2])),
                                memoryview(bytearray(1)), memoryview(bytearray(1)),
                                memoryview(handle))
    assert rc == 0
    fd = struct.unpack("=i", bytes(handle))[0]
    assert fd >= 0

    rc = system.CBL_WRITE_FILE(memoryview(handle), bytearray(struct.pack(">q", 0)),
                               bytearray(struct.pack(">i", 5)), memoryview(bytearray([0])),
                               memoryview(bytearray(b"HELLO")))
    assert rc == 0
    assert system.CBL_CLOSE_FILE(memoryview(handle)) == 0

    handle2 = bytearray(4)
    frame.cob_procedure_parameters[0] = name
    rc = system.CBL_OPEN_FILE(memoryview(name.data), memoryview(bytearray([1])),
                              memoryview(bytearray(1)), memoryview(bytearray(1)),
                              memoryview(handle2))
    assert rc == 0
    buf = bytearray(5)
    foff = bytearray(struct.pack(">q", 0))
    rc = system.CBL_READ_FILE(memoryview(handle2), memoryview(foff),
                              bytearray(struct.pack(">i", 5)),
                              memoryview(bytearray([0x80])), memoryview(buf))
    assert rc == 0
    assert bytes(buf) == b"HELLO"
    # flags & 0x80 writes the file size back (big-endian).
    assert struct.unpack(">q", bytes(foff))[0] == 5
    system.CBL_CLOSE_FILE(memoryview(handle2))


def test_read_file_eof(tmp_path, frame):
    os.chdir(tmp_path)
    name = alnum("empty.bin", 32)
    frame.cob_procedure_parameters[0] = name
    handle = bytearray(4)
    system.CBL_CREATE_FILE(memoryview(name.data), memoryview(bytearray([2])),
                           memoryview(bytearray(1)), memoryview(bytearray(1)),
                           memoryview(handle))
    system.CBL_CLOSE_FILE(memoryview(handle))
    frame.cob_procedure_parameters[0] = name
    h2 = bytearray(4)
    system.CBL_OPEN_FILE(memoryview(name.data), memoryview(bytearray([1])),
                         memoryview(bytearray(1)), memoryview(bytearray(1)),
                         memoryview(h2))
    rc = system.CBL_READ_FILE(memoryview(h2), bytearray(struct.pack(">q", 0)),
                              bytearray(struct.pack(">i", 4)),
                              memoryview(bytearray([0])), memoryview(bytearray(4)))
    assert rc == 10  # end-of-file
    system.CBL_CLOSE_FILE(memoryview(h2))


def test_open_bad_access_mode(tmp_path, frame):
    os.chdir(tmp_path)
    name = alnum("x.bin", 32)
    frame.cob_procedure_parameters[0] = name
    handle = bytearray(4)
    rc = system.CBL_OPEN_FILE(memoryview(name.data), memoryview(bytearray([9])),
                              memoryview(bytearray(1)), memoryview(bytearray(1)),
                              memoryview(handle))
    assert rc == -1
    assert struct.unpack("=i", bytes(handle))[0] == -1


def test_flush_file_is_noop():
    assert system.CBL_FLUSH_FILE(memoryview(bytearray(4))) == 0


def test_copy_rename_delete(tmp_path, frame):
    os.chdir(tmp_path)
    (tmp_path / "src.txt").write_text("payload")
    frame.cob_procedure_parameters[0] = alnum("src.txt", 32)
    frame.cob_procedure_parameters[1] = alnum("dst.txt", 32)
    assert system.CBL_COPY_FILE(memoryview(bytearray(b"src.txt")),
                                memoryview(bytearray(b"dst.txt"))) == 0
    assert (tmp_path / "dst.txt").read_text() == "payload"

    frame.cob_procedure_parameters[0] = alnum("dst.txt", 32)
    frame.cob_procedure_parameters[1] = alnum("ren.txt", 32)
    assert system.CBL_RENAME_FILE(memoryview(bytearray(b"dst.txt")),
                                  memoryview(bytearray(b"ren.txt"))) == 0
    assert (tmp_path / "ren.txt").exists()

    frame.cob_procedure_parameters[0] = alnum("ren.txt", 32)
    assert system.CBL_DELETE_FILE(memoryview(bytearray(b"ren.txt"))) == 0
    assert not (tmp_path / "ren.txt").exists()


def test_delete_missing_returns_128(tmp_path, frame):
    os.chdir(tmp_path)
    frame.cob_procedure_parameters[0] = alnum("nope.txt", 32)
    assert system.CBL_DELETE_FILE(memoryview(bytearray(b"nope.txt"))) == 128


def test_check_file_exist(tmp_path, frame):
    os.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("ab")
    frame.cob_procedure_parameters[0] = alnum("f.txt", 32)
    info = bytearray(16)
    assert system.CBL_CHECK_FILE_EXIST(memoryview(bytearray(b"f.txt")),
                                       memoryview(info)) == 0
    assert struct.unpack(">q", bytes(info[0:8]))[0] == 2  # size
    # month is 1-12, year is a sensible 4-digit value (Python struct_time form).
    assert 1 <= info[9] <= 12
    assert 1970 <= struct.unpack(">h", bytes(info[10:12]))[0] <= 9999
    assert info[15] == 0


def test_check_file_exist_missing(tmp_path, frame):
    os.chdir(tmp_path)
    frame.cob_procedure_parameters[0] = alnum("ghost.txt", 32)
    assert system.CBL_CHECK_FILE_EXIST(memoryview(bytearray(b"ghost.txt")),
                                       memoryview(bytearray(16))) == 35


# ===========================================================================
# Directory operations
# ===========================================================================
def test_dir_create_change_delete(tmp_path, frame):
    os.chdir(tmp_path)
    frame.cob_procedure_parameters[0] = alnum("sub", 32)
    assert system.CBL_CREATE_DIR(memoryview(bytearray(b"sub"))) == 0
    assert (tmp_path / "sub").is_dir()
    frame.cob_procedure_parameters[0] = alnum("sub", 32)
    assert system.CBL_CHANGE_DIR(memoryview(bytearray(b"sub"))) == 0
    assert os.getcwd().endswith("sub")
    os.chdir(tmp_path)
    frame.cob_procedure_parameters[0] = alnum("sub", 32)
    assert system.CBL_DELETE_DIR(memoryview(bytearray(b"sub"))) == 0
    assert not (tmp_path / "sub").is_dir()


def test_get_current_dir(tmp_path, frame):
    os.chdir(tmp_path)
    out = bytearray(256)
    assert system.CBL_GET_CURRENT_DIR(0, 256, memoryview(out)) == 0
    assert bytes(out).rstrip(b" ").decode("latin-1") == os.getcwd()


def test_get_current_dir_bad_args(frame):
    assert system.CBL_GET_CURRENT_DIR(0, 0, memoryview(bytearray(8))) == 128
    assert system.CBL_GET_CURRENT_DIR(1, 256, memoryview(bytearray(256))) == 129


def test_get_current_dir_too_small(tmp_path, frame):
    os.chdir(tmp_path)
    out = bytearray(2)  # far too small for any real path
    assert system.CBL_GET_CURRENT_DIR(0, 2, memoryview(out)) == 128


# ===========================================================================
# ACUCOBOL C$ wrappers
# ===========================================================================
def test_acuw_mkdir(tmp_path, frame):
    os.chdir(tmp_path)
    frame.cob_procedure_parameters[0] = alnum("d2", 32)
    assert system.cob_acuw_mkdir(memoryview(bytearray(b"d2"))) == 0
    assert (tmp_path / "d2").is_dir()


def test_acuw_chdir_sets_status(tmp_path, frame):
    os.chdir(tmp_path)
    (tmp_path / "s").mkdir()
    frame.cob_procedure_parameters[0] = alnum(str(tmp_path / "s"), 128)
    status = numfld(0, 4)
    frame.cob_procedure_parameters[1] = status
    rc = system.cob_acuw_chdir(memoryview(frame.cob_procedure_parameters[0].data), status)
    assert rc == 0
    assert move.cob_get_int(status) == 0


def test_acuw_copyfile(tmp_path, frame):
    os.chdir(tmp_path)
    (tmp_path / "a").write_text("z")
    frame.cob_procedure_parameters[0] = alnum("a", 32)
    frame.cob_procedure_parameters[1] = alnum("b", 32)
    common.cob_call_params = 3
    rc = system.cob_acuw_copyfile(memoryview(bytearray(b"a")),
                                  memoryview(bytearray(b"b")),
                                  memoryview(bytearray(b"\x00")))
    assert rc == 0
    assert (tmp_path / "b").read_text() == "z"


def test_acuw_copyfile_too_few_params(frame):
    common.cob_call_params = 2
    assert system.cob_acuw_copyfile(None, None, None) == 128


def test_acuw_file_info(tmp_path, frame):
    os.chdir(tmp_path)
    (tmp_path / "fi.txt").write_text("abcd")
    frame.cob_procedure_parameters[0] = alnum("fi.txt", 32)
    common.cob_call_params = 2
    info = bytearray(16)
    assert system.cob_acuw_file_info(memoryview(bytearray(b"fi.txt")),
                                     memoryview(info)) == 0
    assert struct.unpack(">Q", bytes(info[0:8]))[0] == 4
    dt = struct.unpack(">I", bytes(info[8:12]))[0]
    # YYYYMMDD layout: a sane modern date.
    assert 19700101 <= dt <= 99991231


def test_acuw_file_delete(tmp_path, frame):
    os.chdir(tmp_path)
    (tmp_path / "del.txt").write_text("x")
    frame.cob_procedure_parameters[0] = alnum("del.txt", 32)
    common.cob_call_params = 2
    assert system.cob_acuw_file_delete(memoryview(bytearray(b"del.txt")),
                                       memoryview(bytearray(b"\x00"))) == 0
    assert not (tmp_path / "del.txt").exists()


# ===========================================================================
# Call-frame helpers
# ===========================================================================
def test_getpid():
    assert system.cob_acuw_getpid() == os.getpid()


def test_narg(frame):
    out = numfld(0, 4)
    frame.cob_procedure_parameters[0] = out
    common.cob_save_call_params = 4
    system.cob_return_args(memoryview(out.data))
    assert move.cob_get_int(out) == 4


def test_paramsize(frame):
    # Caller frame supplies a parameter whose size is queried.
    caller = common.cob_module(cob_procedure_parameters=[alnum("xyz", 3)])
    frame.next = caller
    idx = numfld(1, 4)
    frame.cob_procedure_parameters[0] = idx
    common.cob_save_call_params = 1
    assert system.cob_parameter_size(memoryview(idx.data)) == 3


def test_justify_right(frame):
    data = bytearray(b"hi   ")
    system.cob_acuw_justify(data)  # default direction = right-justify
    assert bytes(data) == b"   hi"


def test_justify_left(frame):
    # The direction operand is honoured only when cob_call_params > 1 (the
    # ``frame`` fixture sets it to 5, matching the C varargs guard).
    data = bytearray(b"   hi")
    system.cob_acuw_justify(data, bytearray(b"L"))
    assert bytes(data) == b"hi   "


def test_nanosleep_zero_is_noop(frame):
    p = numfld(0, 8)
    frame.cob_procedure_parameters[0] = p
    assert system.CBL_OC_NANOSLEEP(memoryview(p.data)) == 0


# ===========================================================================
# SYSTEM command
# ===========================================================================
def test_system_runs_command(frame):
    cmd = alnum("true", 16)  # /bin/true exits 0
    frame.cob_procedure_parameters[0] = cmd
    rc = system.SYSTEM(memoryview(cmd.data))
    assert rc == 0


def test_system_empty_command_returns_1(frame):
    cmd = alnum("    ", 8)  # all blanks
    frame.cob_procedure_parameters[0] = cmd
    assert system.SYSTEM(memoryview(cmd.data)) == 1


# ===========================================================================
# Exit / error handler registration
# ===========================================================================
def test_exit_proc_register_and_remove():
    system._exit_handlers.clear()

    def handler():
        return 0

    # selector 0 installs.
    assert system.CBL_EXIT_PROC(bytearray([0]), handler) == 0
    assert handler in system._exit_handlers
    # selector 1 removes (and does not reinstall).
    assert system.CBL_EXIT_PROC(bytearray([1]), handler) == 0
    assert handler not in system._exit_handlers


def test_exit_proc_no_handler_returns_minus1():
    assert system.CBL_EXIT_PROC(bytearray([0]), None) == -1


def test_error_proc_register():
    system._error_handlers.clear()

    def handler(msg):
        return 0

    assert system.CBL_ERROR_PROC(bytearray([0]), handler) == 0
    assert handler in system._error_handlers
    # any non-zero selector removes without reinstalling.
    assert system.CBL_ERROR_PROC(bytearray([1]), handler) == 0
    assert handler not in system._error_handlers


def test_error_proc_no_handler():
    assert system.CBL_ERROR_PROC(bytearray([0]), None) == -1


# ===========================================================================
# Filename extraction helper (cob_str_from_fld semantics)
# ===========================================================================
def test_fld_str_trims_trailing_blanks():
    assert system._fld_str(bytearray(b"name.txt    ")) == "name.txt"


def test_fld_str_strips_quotes():
    assert system._fld_str(bytearray(b'"a b.txt"   ')) == "a b.txt"


def test_fld_str_stops_at_unquoted_blank():
    assert system._fld_str(bytearray(b"a b")) == "a"


# ===========================================================================
# Subsystem init
# ===========================================================================
def test_cob_init_system_resets_handlers():
    system._exit_handlers.append(lambda: 0)
    system._error_handlers.append(lambda m: 0)
    system.cob_init_system()
    assert system._exit_handlers == []
    assert system._error_handlers == []
