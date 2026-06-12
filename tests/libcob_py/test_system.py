"""Unit tests for :mod:`libcob_py.system`.

Verifies the pure-Python port of the COBOL *system service* routines against the
authoritative ``COB_SYSTEM_GEN`` enumeration in ``libcob/system.def`` (the
``CBL_*`` / ``C$`` families, the ``SYSTEM`` shell call, and the three
high-bit octal-escape externals).  Part of the GNU Cobol C->Python backend
refactor (AAP sections 0.2.1, 0.4.1, 0.6.1, 0.7.1).

What is asserted
----------------
* **Dispatch-table completeness (headline).**  ``system.def`` enumerates
  EXACTLY **43** rows (the AAP narrative's "44" is the leading format comment,
  not an entry -- resolved per the .def-authority hierarchy, AAP 0.6.1 rule 3).
  :data:`EXPECTED_SYSTEM_DEF` below is an *independent* transcription of those
  43 rows used as the oracle, so the tests do not merely echo the module under
  test.  Every external name, its declared parameter count, and its internal
  function name are checked.
* **Octal-escape externals.**  ``"\\221"`` / ``"\\364"`` / ``"\\365"`` are the
  control bytes 0x91 / 0xF4 / 0xF5; the octal==hex identity is asserted to
  document intent, and the keys are resolved tolerantly (latin-1 ``str`` -- the
  representation ``system.py`` actually uses -- or ``bytes``).
* **C$ aliases.**  ``C$TOUPPER`` / ``C$TOLOWER`` resolve to the *same callable
  object* as ``CBL_TOUPPER`` / ``CBL_TOLOWER``.
* **Functional spot-checks.**  Case folding, the bitwise logical family, the
  current-directory / directory-create-delete services, ``C$GETPID``, the
  low-level file-handle API, the ACUCOBOL ``C$`` filesystem wrappers, ``SYSTEM``
  command execution, and exit/error handler (de)registration.

HARD CONSTRAINTS (AAP 0.5 / 0.7.1)
---------------------------------
* **Standard library ONLY.**  ``os`` and ``struct`` are standard library;
  ``pytest`` is a development-only test framework (never a runtime dependency).
  No third-party package is imported.  Crucially, ``libcob_py.move`` is *not*
  imported here: it is outside this test's declared dependency set
  (``system`` / ``common`` / ``fileio`` / ``conftest``).  The one place a
  numeric-field length is exercised reaches ``move`` *transitively* through
  ``common._lazy_get_int`` and is guarded so the suite degrades to a clean skip
  if that runtime helper is unavailable.
* **Clean skip when the runtime is absent.**  The module under test is obtained
  with :func:`pytest.importorskip` at module top level, matching the suite
  convention documented in ``conftest.py``.
* Filesystem mutations are confined to a throwaway directory via the shared
  ``work_dir`` fixture (``conftest.py``), so the source tree stays clean.
"""

# --- Standard-library imports (stdlib only; no third-party packages) -------
import os
import struct

import pytest

# --- Runtime under test + its declared in-package dependencies -------------
# importorskip yields a clean SKIP (not a collection error) when the
# parallel-built runtime package is not yet importable (conftest convention).
system = pytest.importorskip("libcob_py.system")
common = pytest.importorskip("libcob_py.common")


# ===========================================================================
# Independent oracle - the authoritative 43-row libcob/system.def table
# ===========================================================================
# Transcribed directly from libcob/system.def (NOT read back from system.py) so
# these tests are a genuine cross-check of the dispatch contract.  Layout:
# (external-name, declared-parameter-count, internal-function-name).
#
# The three high-bit externals are single control bytes.  In a Python source
# literal "\221" is an OCTAL escape == chr(0o221) == chr(0x91); we build the
# keys via chr() of the documented hex value for unmistakable intent (and assert
# the octal/hex identity in :func:`test_octal_escape_keys`).
_X91_KEY = chr(0x91)   # system.def "\221" (octal 221 == 0x91)
_XF4_KEY = chr(0xF4)   # system.def "\364" (octal 364 == 0xF4)
_XF5_KEY = chr(0xF5)   # system.def "\365" (octal 365 == 0xF5)

EXPECTED_SYSTEM_DEF = (
    # --- SYSTEM group (1) --------------------------------------------------
    ("SYSTEM", 1, "SYSTEM"),
    # --- CBL_* group (27) --------------------------------------------------
    ("CBL_AND", 3, "CBL_AND"),
    ("CBL_CHANGE_DIR", 1, "CBL_CHANGE_DIR"),
    ("CBL_CHECK_FILE_EXIST", 2, "CBL_CHECK_FILE_EXIST"),
    ("CBL_CLOSE_FILE", 1, "CBL_CLOSE_FILE"),
    ("CBL_COPY_FILE", 2, "CBL_COPY_FILE"),
    ("CBL_CREATE_DIR", 1, "CBL_CREATE_DIR"),
    ("CBL_CREATE_FILE", 5, "CBL_CREATE_FILE"),
    ("CBL_DELETE_DIR", 1, "CBL_DELETE_DIR"),
    ("CBL_DELETE_FILE", 1, "CBL_DELETE_FILE"),
    ("CBL_EQ", 3, "CBL_EQ"),
    ("CBL_ERROR_PROC", 2, "CBL_ERROR_PROC"),
    ("CBL_EXIT_PROC", 2, "CBL_EXIT_PROC"),
    ("CBL_FLUSH_FILE", 1, "CBL_FLUSH_FILE"),
    ("CBL_GET_CURRENT_DIR", 3, "CBL_GET_CURRENT_DIR"),
    ("CBL_IMP", 3, "CBL_IMP"),
    ("CBL_NIMP", 3, "CBL_NIMP"),
    ("CBL_NOR", 3, "CBL_NOR"),
    ("CBL_NOT", 2, "CBL_NOT"),
    ("CBL_OC_NANOSLEEP", 1, "CBL_OC_NANOSLEEP"),
    ("CBL_OPEN_FILE", 5, "CBL_OPEN_FILE"),
    ("CBL_OR", 3, "CBL_OR"),
    ("CBL_READ_FILE", 5, "CBL_READ_FILE"),
    ("CBL_RENAME_FILE", 2, "CBL_RENAME_FILE"),
    ("CBL_TOLOWER", 2, "CBL_TOLOWER"),
    ("CBL_TOUPPER", 2, "CBL_TOUPPER"),
    ("CBL_WRITE_FILE", 5, "CBL_WRITE_FILE"),
    ("CBL_XOR", 3, "CBL_XOR"),
    # --- C$* group (12) ----------------------------------------------------
    ("C$CHDIR", 2, "cob_acuw_chdir"),
    ("C$COPY", 3, "cob_acuw_copyfile"),
    ("C$DELETE", 2, "cob_acuw_file_delete"),
    ("C$FILEINFO", 2, "cob_acuw_file_info"),
    ("C$GETPID", 0, "cob_acuw_getpid"),
    ("C$JUSTIFY", 1, "cob_acuw_justify"),
    ("C$MAKEDIR", 1, "cob_acuw_mkdir"),
    ("C$NARG", 1, "cob_return_args"),
    ("C$SLEEP", 1, "cob_acuw_sleep"),
    ("C$PARAMSIZE", 1, "cob_parameter_size"),
    ("C$TOUPPER", 2, "CBL_TOUPPER"),    # alias of CBL_TOUPPER
    ("C$TOLOWER", 2, "CBL_TOLOWER"),    # alias of CBL_TOLOWER
    # --- Octal-escape control-byte externals (3) ---------------------------
    (_X91_KEY, 2, "CBL_X91"),
    (_XF4_KEY, 2, "CBL_XF4"),
    (_XF5_KEY, 2, "CBL_XF5"),
)

#: external-name -> declared parameter count (independent oracle).
EXPECTED_PARAM_COUNT = {ext: n for (ext, n, _i) in EXPECTED_SYSTEM_DEF}

#: external-name -> internal function name (independent oracle).
EXPECTED_INTERNAL = {ext: i for (ext, _n, i) in EXPECTED_SYSTEM_DEF}

#: The three octal-escape rows, with their octal and hex code points, used to
#: document the octal==hex identity and to drive tolerant key resolution.
OCTAL_EXTERNALS = (
    (_X91_KEY, 0o221, 0x91, "CBL_X91"),
    (_XF4_KEY, 0o364, 0xF4, "CBL_XF4"),
    (_XF5_KEY, 0o365, 0xF5, "CBL_XF5"),
)


# ===========================================================================
# Helpers
# ===========================================================================
def _alnum(text, size=None):
    """Build an ALPHANUMERIC :class:`common.cob_field` from *text*.

    *text* may be ``str`` (encoded latin-1, so high bytes round-trip) or
    ``bytes``.  When *size* is given the data is right-blank-padded, matching a
    fixed-length COBOL data item.
    """
    data = text.encode("latin-1") if isinstance(text, str) else bytes(text)
    if size is not None:
        data = data.ljust(size, b" ")
    attr = common.cob_field_attr(
        type=common.COB_TYPE_ALPHANUMERIC, digits=0, scale=0, flags=0, pic=None)
    return common.cob_field(size=len(data), data=bytearray(data), attr=attr)


def _numdisp(value, size):
    """Build a zoned NUMERIC DISPLAY :class:`common.cob_field` of *size* digits.

    Used only by the (guarded) numeric-field-length coercion test; decoding such
    a field back to an int routes through ``common._lazy_get_int`` -> the sibling
    ``move`` runtime, which is why that single test is defensively skipped if the
    helper is unavailable.
    """
    data = ("%0*d" % (size, value)).encode("latin-1")
    attr = common.cob_field_attr(
        type=common.COB_TYPE_NUMERIC_DISPLAY, digits=size, scale=0,
        flags=0, pic=None)
    return common.cob_field(size=len(data), data=bytearray(data), attr=attr)


def _table_internal(external):
    """Resolve *external* -> internal name in ``SYSTEM_TABLE`` tolerantly.

    ``system.py`` stores the octal-escape externals as latin-1 ``str`` keys; a
    future re-implementation could store them as ``bytes``.  This resolver
    accepts either representation so the contract assertions are robust.
    """
    table = system.SYSTEM_TABLE
    if external in table:
        return table[external]
    if isinstance(external, str):
        as_bytes = external.encode("latin-1")
        if as_bytes in table:
            return table[as_bytes]
    raise KeyError(external)


def _table_param_count(external):
    """Resolve *external* -> declared parameter count tolerantly (str or bytes)."""
    counts = system.SYSTEM_PARAM_COUNT
    if external in counts:
        return counts[external]
    if isinstance(external, str):
        as_bytes = external.encode("latin-1")
        if as_bytes in counts:
            return counts[as_bytes]
    raise KeyError(external)


def _routine(external):
    """Return the callable registered for *external*, exactly as ``call.py`` does.

    Honours an optional ``lookup``/``resolve`` accessor when the runtime exposes
    one; otherwise indexes ``SYSTEM_TABLE`` and binds the internal name via
    ``getattr(system, internal)`` (the documented resolution path).
    """
    for accessor_name in ("lookup", "resolve"):
        accessor = getattr(system, accessor_name, None)
        if callable(accessor):
            result = accessor(external)
            if callable(result):
                return result
            if isinstance(result, (tuple, list)):
                for item in result:
                    if callable(item):
                        return item
    return getattr(system, _table_internal(external))


@pytest.fixture
def frame():
    """Install a fresh current-module frame with empty procedure parameters.

    Several routines consult ``common.cob_current_module`` (filename extraction,
    C$NARG / C$PARAMSIZE inspection, the SYSTEM command text) and the file/
    filesystem wrappers honour ``common.cob_call_params``.  This fixture installs
    a deterministic 8-slot frame and restores the previous global state at
    teardown so tests stay isolated.
    """
    saved_module = common.cob_current_module
    saved_call = common.cob_call_params
    saved_save = common.cob_save_call_params
    module = common.cob_module(cob_procedure_parameters=[None] * 8)
    common.cob_current_module = module
    common.cob_call_params = 5
    common.cob_save_call_params = 5
    yield module
    common.cob_current_module = saved_module
    common.cob_call_params = saved_call
    common.cob_save_call_params = saved_save


# ===========================================================================
# Phase 1 - Registration-table completeness & exact parameter counts
# ===========================================================================
def test_system_table_has_43_entries():
    """The dispatch table has EXACTLY 43 entries (NOT the narrative's 44)."""
    assert len(system.SYSTEM_TABLE) == 43
    # The supporting structures agree on the count.
    assert len(system.SYSTEM_PARAM_COUNT) == 43
    assert len(system.SYSTEM_DEF) == 43
    # And our independent oracle is itself exactly 43 rows (1 + 27 + 12 + 3).
    assert len(EXPECTED_SYSTEM_DEF) == 43


def test_param_counts_exact():
    """Every one of the 43 externals carries its exact declared parameter count.

    Covers ALL 43 rows (the 40 string-named entries plus the 3 octal-byte ones)
    and additionally checks that each external maps to the expected internal
    function name.
    """
    assert len(EXPECTED_PARAM_COUNT) == 43
    for external, expected_n in EXPECTED_PARAM_COUNT.items():
        assert _table_param_count(external) == expected_n, external
        assert _table_internal(external) == EXPECTED_INTERNAL[external], external


def test_octal_escape_keys():
    """The 3 high-bit externals resolve to CBL_X91/XF4/XF5 with param count 2.

    Documents the octal==hex identity (0o221==0x91, 0o364==0xF4, 0o365==0xF5)
    and resolves the keys tolerantly (latin-1 str, as system.py stores them, or
    bytes).
    """
    # octal -> hex identity (documents intent of the system.def escapes).
    assert 0o221 == 0x91
    assert 0o364 == 0xF4
    assert 0o365 == 0xF5

    for key, octal_cp, hex_cp, internal in OCTAL_EXTERNALS:
        assert octal_cp == hex_cp                  # same code point
        assert ord(key) == hex_cp                  # key really is that byte
        assert _table_internal(key) == internal    # maps to CBL_X91/XF4/XF5
        assert _table_param_count(key) == 2         # each takes 2 parameters
        # The internal routine is a real callable on the module.
        assert callable(getattr(system, internal))


def test_c_dollar_aliases():
    """C$TOUPPER/C$TOLOWER alias CBL_*; C$ routines map to the documented internals."""
    table = system.SYSTEM_TABLE

    # Aliases must resolve to the SAME callable object, not merely the same name.
    assert table["C$TOUPPER"] == "CBL_TOUPPER"
    assert table["C$TOLOWER"] == "CBL_TOLOWER"
    assert getattr(system, table["C$TOUPPER"]) is system.CBL_TOUPPER
    assert getattr(system, table["C$TOLOWER"]) is system.CBL_TOLOWER
    assert _routine("C$TOUPPER") is system.CBL_TOUPPER
    assert _routine("C$TOLOWER") is system.CBL_TOLOWER

    # The ACUCOBOL filesystem / call-frame C$ routines map to their cob_acuw_* /
    # cob_return_args / cob_parameter_size internals.
    assert table["C$CHDIR"] == "cob_acuw_chdir"
    assert table["C$COPY"] == "cob_acuw_copyfile"
    assert table["C$DELETE"] == "cob_acuw_file_delete"
    assert table["C$FILEINFO"] == "cob_acuw_file_info"
    assert table["C$GETPID"] == "cob_acuw_getpid"
    assert table["C$JUSTIFY"] == "cob_acuw_justify"
    assert table["C$MAKEDIR"] == "cob_acuw_mkdir"
    assert table["C$NARG"] == "cob_return_args"
    assert table["C$SLEEP"] == "cob_acuw_sleep"
    assert table["C$PARAMSIZE"] == "cob_parameter_size"


# ===========================================================================
# Phase 2 - Functional spot-checks (filesystem work confined to work_dir)
# ===========================================================================
def test_cbl_toupper_tolower():
    """CBL_TOUPPER 'abc'->'ABC' and CBL_TOLOWER 'ABC'->'abc', via the registry."""
    toupper = _routine("CBL_TOUPPER")
    buf = bytearray(b"abc")
    assert toupper(memoryview(buf), 3) == 0
    assert bytes(buf) == b"ABC"

    tolower = _routine("CBL_TOLOWER")
    buf = bytearray(b"ABC")
    assert tolower(memoryview(buf), 3) == 0
    assert bytes(buf) == b"abc"

    # Non-alphabetic bytes and the wrong-case letters are left untouched.
    mixed = bytearray(b"aB3z!")
    _routine("CBL_TOUPPER")(memoryview(mixed), 5)
    assert bytes(mixed) == b"AB3Z!"


def test_cbl_bit_logic():
    """CBL_AND/OR/XOR/NOT produce the correct byte-wise result over *length*."""
    # AND: data_2 &= data_1.  0xFF AND 0x0F over 1 byte -> 0x0F (prompt example).
    a, b = bytearray(b"\xff"), bytearray(b"\x0f")
    assert _routine("CBL_AND")(memoryview(a), memoryview(b), 1) == 0
    assert b[0] == 0x0F

    # OR: data_2 |= data_1.
    a, b = bytearray(b"\xf0"), bytearray(b"\x0f")
    assert _routine("CBL_OR")(memoryview(a), memoryview(b), 1) == 0
    assert b[0] == 0xFF

    # XOR: data_2 ^= data_1.
    a, b = bytearray(b"\xff"), bytearray(b"\x0f")
    assert _routine("CBL_XOR")(memoryview(a), memoryview(b), 1) == 0
    assert b[0] == 0xF0

    # NOT: data_1 = ~data_1 (single operand).
    a = bytearray(b"\x0f")
    assert _routine("CBL_NOT")(memoryview(a), 1) == 0
    assert a[0] == 0xF0

    # Multi-byte AND honours the length operand exactly.
    a = bytearray(b"\xf0\x0f\xaa")
    b = bytearray(b"\x0f\xf0\x55")
    system.CBL_AND(memoryview(a), memoryview(b), 3)
    assert bytes(b) == b"\x00\x00\x00"


def test_cbl_nor_imp_nimp_eq():
    """The remaining bitwise routines match the C byte-wise truth tables."""
    a, b = bytearray(b"\xf0"), bytearray(b"\x0f")
    system.CBL_NOR(memoryview(a), memoryview(b), 1)
    assert b[0] == (~(0xF0 | 0x0F)) & 0xFF          # ~0xFF == 0x00

    a, b = bytearray(b"\xf0"), bytearray(b"\x0f")
    system.CBL_IMP(memoryview(a), memoryview(b), 1)
    assert b[0] == ((~0xF0) | 0x0F) & 0xFF          # 0x1F

    a, b = bytearray(b"\xf0"), bytearray(b"\x0f")
    system.CBL_NIMP(memoryview(a), memoryview(b), 1)
    assert b[0] == (0xF0 & (~0x0F)) & 0xFF          # 0xF0

    a, b = bytearray(b"\xaa"), bytearray(b"\xaa")
    system.CBL_EQ(memoryview(a), memoryview(b), 1)
    assert b[0] == 0xFF                             # ~(x ^ x) == 0xFF


def test_bitwise_nonpositive_length_is_noop():
    """A non-positive length leaves the buffers untouched (the C length<=0 guard)."""
    a, b = bytearray(b"\x12"), bytearray(b"\x34")
    system.CBL_AND(memoryview(a), memoryview(b), 0)
    assert bytes(b) == b"\x34"
    system.CBL_NOT(memoryview(a), -1)
    assert bytes(a) == b"\x12"


def test_xf4_packs_low_bits():
    """CBL_XF4 packs the low bit of 8 bytes into one byte (MSB first)."""
    src = bytearray([1, 0, 1, 0, 1, 0, 1, 0])
    one = bytearray(1)
    assert system.CBL_XF4(memoryview(one), memoryview(src)) == 0
    assert one[0] == 0b10101010


def test_xf5_unpacks_bits():
    """CBL_XF5 unpacks one byte into 8 bytes of 0/1 (MSB first)."""
    one = bytearray([0b10101010])
    out = bytearray(8)
    assert system.CBL_XF5(memoryview(one), memoryview(out)) == 0
    assert list(out) == [1, 0, 1, 0, 1, 0, 1, 0]


def test_x91_switches_and_param_count():
    """CBL_X91 program-switch get/set (func 11/12), param count (16), bad func."""
    # CBL_X91 mutates global program switches and reads the saved CALL parameter
    # count; snapshot and restore both so the test leaves no global residue for
    # other modules in the suite.
    saved_switches = [common.cob_get_switch(i) for i in range(8)]
    saved_save = common.cob_save_call_params
    try:
        for i in range(8):
            common.cob_set_switch(i, 0)

        res = bytearray(1)
        # func 11: set the 8 program switches from parm.
        system.CBL_X91(memoryview(res), memoryview(bytearray([11])),
                       memoryview(bytearray([1, 0, 1, 1, 0, 0, 0, 0])))
        assert res[0] == 0
        assert [common.cob_get_switch(i) for i in range(4)] == [1, 0, 1, 1]

        # func 12: read the switches back into parm.
        got = bytearray(8)
        system.CBL_X91(memoryview(res), memoryview(bytearray([12])), memoryview(got))
        assert list(got[:4]) == [1, 0, 1, 1]

        # func 16: store the saved CALL parameter count.
        common.cob_save_call_params = 7
        pc = bytearray(1)
        system.CBL_X91(memoryview(res), memoryview(bytearray([16])), memoryview(pc))
        assert pc[0] == 7 and res[0] == 0

        # unknown function -> result byte set to 1.
        system.CBL_X91(memoryview(res), memoryview(bytearray([99])), memoryview(pc))
        assert res[0] == 1
    finally:
        for i in range(8):
            common.cob_set_switch(i, saved_switches[i])
        common.cob_save_call_params = saved_save


def test_cbl_get_current_dir(work_dir):
    """CBL_GET_CURRENT_DIR writes the cwd into the buffer; it matches os.getcwd()."""
    size = 256
    buf = bytearray(size)
    rc = system.CBL_GET_CURRENT_DIR(0, size, memoryview(buf))
    assert rc == 0
    got = bytes(buf).rstrip(b" \x00").decode("latin-1")
    assert got == os.getcwd()
    assert got == str(work_dir)


def test_cbl_get_current_dir_errors_and_quoting(work_dir):
    """CBL_GET_CURRENT_DIR error codes (128/129) and the space-quoting branch."""
    # dir_length < 1 -> 128.
    assert system.CBL_GET_CURRENT_DIR(0, 0, memoryview(bytearray(8))) == 128
    # non-zero flags -> 129.
    assert system.CBL_GET_CURRENT_DIR(1, 16, memoryview(bytearray(16))) == 129
    # buffer too small for the path -> 128.
    assert system.CBL_GET_CURRENT_DIR(0, 1, memoryview(bytearray(1))) == 128

    # A path containing a space is wrapped in double quotes.
    os.mkdir("has space")
    os.chdir("has space")
    size = 512
    buf = bytearray(size)
    assert system.CBL_GET_CURRENT_DIR(0, size, memoryview(buf)) == 0
    out = bytes(buf).rstrip(b" ")
    assert out[:1] == b'"' and out[-1:] == b'"'
    assert out[1:-1].decode("latin-1") == os.getcwd()


def test_c_getpid():
    """C$GETPID (param count 0) returns the current process id."""
    getpid = _routine("C$GETPID")          # -> cob_acuw_getpid
    assert getpid() == os.getpid()
    assert system.cob_acuw_getpid() == os.getpid()
    assert _table_param_count("C$GETPID") == 0


def test_cbl_create_dir_delete(work_dir):
    """CBL_CREATE_DIR / CBL_CHECK_FILE_EXIST / CBL_DELETE_DIR over a temp dir."""
    name = _alnum("subdir")
    assert system.CBL_CREATE_DIR(memoryview(name.data)) == 0
    assert os.path.isdir("subdir")

    # CHECK_FILE_EXIST succeeds (0) for the existing entry and fills the block.
    info = bytearray(16)
    assert system.CBL_CHECK_FILE_EXIST(memoryview(name.data),
                                       memoryview(info)) == 0

    assert system.CBL_DELETE_DIR(memoryview(name.data)) == 0
    assert not os.path.exists("subdir")

    # A missing entry reports COBOL status 35.
    assert system.CBL_CHECK_FILE_EXIST(memoryview(_alnum("ghost").data),
                                       memoryview(bytearray(16))) == 35
    # No operand at all -> -1 (the C "no name" guard).
    assert system.CBL_CREATE_DIR(None) == -1
    assert system.CBL_DELETE_DIR(None) == -1


def test_cbl_change_dir(work_dir):
    """CBL_CHANGE_DIR enters an existing directory and rejects a missing one."""
    os.mkdir("into")
    assert system.CBL_CHANGE_DIR(memoryview(_alnum("into").data)) == 0
    assert os.path.basename(os.getcwd()) == "into"
    assert system.CBL_CHANGE_DIR(memoryview(_alnum("no_such_dir").data)) == 128


# ===========================================================================
# File-handle API round trip + error paths (needs struct + the frame fixture)
# ===========================================================================
def test_file_handle_round_trip(work_dir, frame):
    """CREATE -> WRITE -> CLOSE -> OPEN -> READ -> FLUSH -> CLOSE round trip."""
    name = _alnum("data.bin", 32)
    frame.cob_procedure_parameters[0] = name

    handle = bytearray(4)
    # access byte 2 == create/truncate/write.
    rc = system.CBL_CREATE_FILE(
        memoryview(name.data), memoryview(bytearray([2])),
        memoryview(bytearray(1)), memoryview(bytearray(1)), memoryview(handle))
    assert rc == 0

    payload = b"HELLO-COBOL"
    offset = bytearray(struct.pack(">q", 0))
    length = bytearray(struct.pack(">i", len(payload)))
    flags = bytearray([0])
    rc = system.CBL_WRITE_FILE(
        memoryview(handle), memoryview(offset), memoryview(length),
        memoryview(flags), memoryview(bytearray(payload)))
    assert rc == 0
    assert system.CBL_FLUSH_FILE(memoryview(handle)) == 0
    assert system.CBL_CLOSE_FILE(memoryview(handle)) == 0

    # Reopen read-only (access byte 1) and read the payload back.
    handle2 = bytearray(4)
    rc = system.CBL_OPEN_FILE(
        memoryview(name.data), memoryview(bytearray([1])),
        memoryview(bytearray(1)), memoryview(bytearray(1)), memoryview(handle2))
    assert rc == 0

    readbuf = bytearray(len(payload))
    # flags bit 0x80 also writes the file size back into the offset field.
    offset_io = bytearray(struct.pack(">q", 0))
    rc = system.CBL_READ_FILE(
        memoryview(handle2), memoryview(offset_io),
        memoryview(struct.pack(">i", len(payload))),
        memoryview(bytearray([0x80])), memoryview(readbuf))
    assert rc == 0
    assert bytes(readbuf) == payload
    assert struct.unpack(">q", bytes(offset_io))[0] == len(payload)
    assert system.CBL_CLOSE_FILE(memoryview(handle2)) == 0


def test_file_handle_error_paths(work_dir):
    """CBL_OPEN_FILE: bad access mode -> -1; open of a missing file -> 35."""
    handle = bytearray(4)
    # access byte 0 is not a valid mode -> -1, handle set to -1.
    rc = system.CBL_OPEN_FILE(
        memoryview(_alnum("x.bin").data), memoryview(bytearray([0])),
        memoryview(bytearray(1)), memoryview(bytearray(1)), memoryview(handle))
    assert rc == -1
    assert struct.unpack("=i", bytes(handle))[0] == -1

    # Opening a non-existent file for read -> 35.
    rc = system.CBL_OPEN_FILE(
        memoryview(_alnum("absent.bin").data), memoryview(bytearray([1])),
        memoryview(bytearray(1)), memoryview(bytearray(1)), memoryview(handle))
    assert rc == 35


def test_delete_copy_rename(work_dir):
    """CBL_COPY_FILE / CBL_RENAME_FILE / CBL_DELETE_FILE happy + error paths."""
    with open("a.txt", "wb") as fh:
        fh.write(b"abc-123")

    assert system.CBL_COPY_FILE(memoryview(_alnum("a.txt").data),
                                memoryview(_alnum("b.txt").data)) == 0
    assert os.path.exists("b.txt")

    assert system.CBL_RENAME_FILE(memoryview(_alnum("b.txt").data),
                                  memoryview(_alnum("c.txt").data)) == 0
    assert os.path.exists("c.txt") and not os.path.exists("b.txt")

    assert system.CBL_DELETE_FILE(memoryview(_alnum("c.txt").data)) == 0
    assert not os.path.exists("c.txt")

    # Deleting a missing file -> 128; deleting with no name -> -1.
    assert system.CBL_DELETE_FILE(memoryview(_alnum("ghost.txt").data)) == 128
    assert system.CBL_DELETE_FILE(None) == -1
    # Copy/rename with a missing operand -> -1.
    assert system.CBL_COPY_FILE(None, None) == -1
    assert system.CBL_RENAME_FILE(None, None) == -1


def test_check_file_exist_fills_info_block(work_dir):
    """CBL_CHECK_FILE_EXIST fills the 16-byte size|date|time block for a real file."""
    payload = b"0123456789"
    with open("sized.bin", "wb") as fh:
        fh.write(payload)
    info = bytearray(16)
    assert system.CBL_CHECK_FILE_EXIST(memoryview(_alnum("sized.bin").data),
                                       memoryview(info)) == 0
    # First 8 bytes are the big-endian file size.
    assert struct.unpack(">q", bytes(info[0:8]))[0] == len(payload)
    # Month byte is 1-12 (Python semantics preserved) and the trailing byte is 0.
    assert 1 <= info[9] <= 12
    assert info[15] == 0


# ===========================================================================
# SYSTEM shell execution, nanosleep, exit/error handlers
# ===========================================================================
def test_system_shell_execution(frame):
    """SYSTEM runs the procedure-parameter command; empty text returns 1."""
    cmd = _alnum("exit 0")
    frame.cob_procedure_parameters[0] = cmd
    assert system.SYSTEM(memoryview(cmd.data)) == 0

    frame.cob_procedure_parameters[0] = _alnum("exit 3")
    assert system.SYSTEM(memoryview(bytearray(b""))) == 3

    # An all-blank command is treated as empty -> 1.
    frame.cob_procedure_parameters[0] = _alnum("    ")
    assert system.SYSTEM(memoryview(bytearray(b""))) == 1


def test_cbl_oc_nanosleep_no_param(frame):
    """CBL_OC_NANOSLEEP with no usable parameter is a no-op returning 0."""
    frame.cob_procedure_parameters[0] = None
    # A bare buffer (not a cob_field) cannot supply a nanosecond count -> no sleep.
    assert system.CBL_OC_NANOSLEEP(memoryview(bytearray(8))) == 0


def test_exit_and_error_proc_registration():
    """CBL_EXIT_PROC / CBL_ERROR_PROC (de)register handlers; bad handler -> -1."""
    def handler():
        return None

    # Install via a memoryview selector (first byte in {0,2,3} installs).
    assert system.CBL_EXIT_PROC(memoryview(bytearray([0])), handler) == 0
    assert handler in system._exit_handlers
    # Selector 1 only removes any existing copy.
    assert system.CBL_EXIT_PROC(memoryview(bytearray([1])), handler) == 0
    assert handler not in system._exit_handlers
    # An int selector and a cob_field selector are both accepted (_selector_byte).
    assert system.CBL_EXIT_PROC(2, handler) == 0
    assert system.CBL_EXIT_PROC(_alnum("\x00"), handler) == 0
    # No usable handler -> -1.
    assert system.CBL_EXIT_PROC(memoryview(bytearray([0])), None) == -1

    assert system.CBL_ERROR_PROC(memoryview(bytearray([0])), handler) == 0
    assert handler in system._error_handlers
    assert system.CBL_ERROR_PROC(memoryview(bytearray([0])), None) == -1


# ===========================================================================
# ACUCOBOL C$ filesystem wrappers (delegated to libcob_py.fileio)
# ===========================================================================
def test_c_makedir_wrapper(work_dir):
    """C$MAKEDIR (cob_acuw_mkdir) delegates to fileio and creates the directory."""
    assert system.cob_acuw_mkdir(_alnum("wrapdir")) == 0
    assert os.path.isdir("wrapdir")
    # A second create over the same name fails (128).
    assert system.cob_acuw_mkdir(_alnum("wrapdir")) == 128


def test_c_chdir_wrapper(work_dir):
    """C$CHDIR (cob_acuw_chdir) changes directory and writes a status field."""
    os.mkdir("cdsub")
    status = _numdisp(0, 4)
    assert system.cob_acuw_chdir(_alnum("cdsub"), status) == 0
    assert os.path.basename(os.getcwd()) == "cdsub"


def test_c_copy_and_fileinfo_and_delete_wrappers(work_dir, frame):
    """C$COPY / C$FILEINFO / C$DELETE wrappers delegate to fileio correctly.

    The frame fixture sets ``cob_call_params`` >= the routines' required counts,
    so fileio's parameter-count guards pass.
    """
    with open("src.txt", "wb") as fh:
        fh.write(b"payload-123")

    # C$COPY needs >= 3 parameters.
    assert system.cob_acuw_copyfile(_alnum("src.txt"),
                                    _alnum("dst.txt"), None) == 0
    with open("dst.txt", "rb") as fh:
        assert fh.read() == b"payload-123"

    # C$FILEINFO writes a 16-byte block back into the caller buffer.
    info = bytearray(16)
    assert system.cob_acuw_file_info(_alnum("src.txt"), memoryview(info)) == 0
    assert struct.unpack(">Q", bytes(info[0:8]))[0] == len(b"payload-123")
    # A missing file reports 35.
    assert system.cob_acuw_file_info(_alnum("nope.txt"),
                                     memoryview(bytearray(16))) == 35

    # C$DELETE removes the file.
    assert system.cob_acuw_file_delete(_alnum("dst.txt"), None) == 0
    assert not os.path.exists("dst.txt")


def test_c_call_frame_helpers(frame):
    """C$NARG / C$PARAMSIZE / C$SLEEP / C$JUSTIFY are wired to common's helpers."""
    # C$NARG stores the saved CALL parameter count into procedure parameter 0.
    out = _numdisp(0, 4)
    frame.cob_procedure_parameters[0] = out
    common.cob_save_call_params = 3
    assert system.cob_return_args(None) == 0

    # C$PARAMSIZE returns 0 when there is no caller frame to inspect.
    probe = _numdisp(0, 4)
    frame.cob_procedure_parameters[0] = probe
    assert system.cob_parameter_size(None) == 0

    # C$SLEEP with a non-positive count is a no-op returning 0.
    frame.cob_procedure_parameters[0] = _numdisp(0, 4)
    assert system.cob_acuw_sleep(None) == 0

    # C$JUSTIFY is exposed and callable (re-exported from common).
    assert callable(system.cob_acuw_justify)


# ===========================================================================
# Argument-coercion helpers
# ===========================================================================
def test_cob_field_operands_are_coerced():
    """A :class:`common.cob_field` operand is accepted directly (via _as_buf)."""
    f1 = _alnum("\xff")
    f2 = _alnum("\x0f")
    assert system.CBL_AND(f1, f2, 1) == 0
    assert bytes(f2.data) == b"\x0f"          # 0x0F & 0xFF == 0x0F


def test_numeric_field_length_is_coerced():
    """A NUMERIC DISPLAY length field is decoded to an int (via _as_int).

    This exercises ``_as_int``'s cob_field branch, which defers to
    ``common._lazy_get_int`` -> the sibling ``move`` runtime.  ``move`` is NOT
    imported by this test module (it is outside the declared dependency set);
    the decode is reached transitively and the test degrades to a clean skip if
    that runtime path is unavailable.
    """
    try:
        length = _numdisp(2, 4)               # value 2, 4 digits ("0002")
        a = bytearray(b"\xff\x00\xaa")
        b = bytearray(b"\x0f\x0f\x0f")
        rc = system.CBL_XOR(memoryview(a), memoryview(b), length)
    except Exception:                          # pragma: no cover - skip guard
        pytest.skip("numeric-field length decode unavailable (libcob_py.move)")
    assert rc == 0
    # Only the first two bytes are XORed; the third is untouched.
    assert bytes(b) == bytes([0x0F ^ 0xFF, 0x0F ^ 0x00, 0x0F])


# ===========================================================================
# Subsystem initialisation
# ===========================================================================
def test_cob_init_system_resets_handlers():
    """cob_init_system clears the exit/error handler lists."""
    def handler():
        return None

    system.CBL_EXIT_PROC(memoryview(bytearray([0])), handler)
    system.CBL_ERROR_PROC(memoryview(bytearray([0])), handler)
    system.cob_init_system()
    assert system._exit_handlers == []
    assert system._error_handlers == []


# ===========================================================================
# Phase 3 - Registration coordination (smoke)
# ===========================================================================
def test_routines_callable():
    """Every internal name in SYSTEM_TABLE resolves to a real callable.

    This is exactly the lookup ``call.py`` performs at ``cob_init_call`` time
    (``getattr(system, internal_name)``), so a passing smoke test guarantees no
    dispatch entry points at a missing routine.  Routines that require complex
    COBOL field operands are only checked for callability, never invoked here.
    """
    for external, internal in system.SYSTEM_TABLE.items():
        assert hasattr(system, internal), "%r -> %s missing" % (external, internal)
        assert callable(getattr(system, internal)), internal


# ===========================================================================
# SECURITY - REVIEW FIX: CWE-78 (SYSTEM command injection) and CWE-22
# (CBL_* filesystem path traversal).
#
# These tests pin the two security findings raised against ``system.py``:
#   * SYSTEM truncates its operand at the first embedded NUL (C-string
#     fidelity), so text smuggled after a NUL never reaches the shell; and
#   * every CBL_* filesystem routine funnels its COBOL-supplied path through
#     the centralised ``fileio._safe_path`` guard (via ``_safe_filename_arg``),
#     rejecting NUL/ASCII-control bytes and relative ``..`` traversal escapes
#     with each routine's *documented* failure code (35 / 128 / -1), while a
#     legitimate relative name continues to succeed (no false positives).
# ===========================================================================
class TestSystemSecurityHardening:
    """SYSTEM command-injection guardrail + CBL_* path-traversal rejection."""

    # ---- CWE-78: SYSTEM NUL truncation -----------------------------------
    def test_system_truncates_at_embedded_nul(self, work_dir, frame):
        """An embedded NUL terminates the command; the post-NUL payload (which
        would create ``pwned`` if it reached the shell) must never run."""
        smuggle = work_dir / "pwned"
        cmd = _alnum("exit 0\x00; touch pwned")
        frame.cob_procedure_parameters[0] = cmd
        assert system.SYSTEM(memoryview(cmd.data)) == 0
        assert not smuggle.exists()

    def test_system_only_nul_is_empty(self, frame):
        """A command that is just a NUL (then blanks) trims to empty -> 1."""
        cmd = _alnum("\x00   ")
        frame.cob_procedure_parameters[0] = cmd
        assert system.SYSTEM(memoryview(cmd.data)) == 1

    # ---- CWE-22: helper-level validation ---------------------------------
    def test_safe_filename_arg_rejects_control_byte(self):
        with pytest.raises(OSError):
            system._safe_filename_arg(memoryview(_alnum("foo\x01bar").data), 0)

    def test_safe_filename_arg_rejects_parent_traversal(self):
        with pytest.raises(OSError):
            system._safe_filename_arg(memoryview(_alnum("../escape").data), 0)

    def test_safe_filename_arg_allows_clean_name(self):
        assert system._safe_filename_arg(
            memoryview(_alnum("clean.dat").data), 0) == "clean.dat"

    # ---- CWE-22: per-routine return-code contract preservation -----------
    # The two attack shapes exercised against every routine: a path with an
    # embedded ASCII control byte, and a relative parent-directory escape.
    _BAD = ("foo\x01bar", "../escape")

    def test_open_and_create_reject_unsafe_path(self, work_dir):
        for bad in self._BAD:
            handle = bytearray(4)
            assert system.CBL_OPEN_FILE(
                memoryview(_alnum(bad).data), memoryview(bytearray([1])),
                memoryview(bytearray(1)), memoryview(bytearray(1)),
                memoryview(handle)) == 35
            assert struct.unpack("=i", bytes(handle))[0] == -1
            assert system.CBL_CREATE_FILE(
                memoryview(_alnum(bad).data), memoryview(bytearray([2])),
                memoryview(bytearray(1)), memoryview(bytearray(1)),
                memoryview(bytearray(4))) == 35
        # No false positive: a clean relative name still creates successfully.
        assert system.CBL_CREATE_FILE(
            memoryview(_alnum("ok.bin").data), memoryview(bytearray([2])),
            memoryview(bytearray(1)), memoryview(bytearray(1)),
            memoryview(bytearray(4))) == 0

    def test_delete_and_rename_reject_unsafe_path(self, work_dir):
        for bad in self._BAD:
            assert system.CBL_DELETE_FILE(memoryview(_alnum(bad).data)) == 128
            assert system.CBL_RENAME_FILE(
                memoryview(_alnum(bad).data),
                memoryview(_alnum("ok.txt").data)) == 128
            # An unsafe *destination* is rejected too (both operands validated).
            assert system.CBL_RENAME_FILE(
                memoryview(_alnum("ok.txt").data),
                memoryview(_alnum(bad).data)) == 128

    def test_copy_rejects_unsafe_source_and_destination(self, work_dir):
        with open("real.txt", "wb") as fh:
            fh.write(b"payload")
        for bad in self._BAD:
            assert system.CBL_COPY_FILE(
                memoryview(_alnum(bad).data),
                memoryview(_alnum("dst.txt").data)) == -1
            assert system.CBL_COPY_FILE(
                memoryview(_alnum("real.txt").data),
                memoryview(_alnum(bad).data)) == -1

    def test_check_file_exist_rejects_unsafe_path(self, work_dir):
        for bad in self._BAD:
            assert system.CBL_CHECK_FILE_EXIST(
                memoryview(_alnum(bad).data), memoryview(bytearray(16))) == 35

    def test_directory_routines_reject_unsafe_path(self, work_dir):
        for bad in self._BAD:
            assert system.CBL_CREATE_DIR(memoryview(_alnum(bad).data)) == 128
            assert system.CBL_CHANGE_DIR(memoryview(_alnum(bad).data)) == 128
            assert system.CBL_DELETE_DIR(memoryview(_alnum(bad).data)) == 128
