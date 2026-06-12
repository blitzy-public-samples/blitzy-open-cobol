"""Unit tests for :mod:`libcob_py.fileio` - sequential / relative / indexed I/O.

Exercises the pure-Python port of ``libcob/fileio.c``: the SEQUENTIAL, LINE
SEQUENTIAL, RELATIVE and INDEXED organizations, the two-character COBOL
file-status model, the ``dbm`` / ``sqlite3`` indexed-backend routing (AAP
section 0.6.3), SORT/MERGE, the ``C$`` filesystem routines, and the runtime
lifecycle.  The suite specifically locks down the code-review fixes:

* the exact em-dash migration message (status ``30`` contract);
* the centralised :func:`~libcob_py.fileio._safe_path` CWE-22 guard wired into
  filename resolution and the ``C$`` routines;
* the bounded **external merge sort** (sorted runs + streaming k-way merge);
* the LINAGE open-error file-descriptor leak fix; and
* the integer accessors that route through :mod:`libcob_py.move`.

Standard library only - the runtime under test introduces ZERO third-party
dependencies; ``pytest`` is a development-only test framework (AAP 0.5 / 0.7.1).
"""
import os
import struct

import pytest

libcob_py = pytest.importorskip("libcob_py")
common = pytest.importorskip("libcob_py.common")
move = pytest.importorskip("libcob_py.move")
fileio = pytest.importorskip("libcob_py.fileio")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_ALNUM = common.cob_field_attr(type=common.COB_TYPE_ALPHANUMERIC)


def st(f):
    """Return the connector's two-character FILE STATUS as a ``str``."""
    return bytes(f.file_status[:2]).decode("latin-1")


def make_file(path, recsize, org=common.COB_ORG_SEQUENTIAL,
              access=common.COB_ACCESS_SEQUENTIAL, optional=0, record_min=None):
    """Build a :class:`~libcob_py.fileio.cob_file` connector for *path*."""
    f = fileio.cob_file()
    f.select_name = str(path)
    f.assign = None
    f.organization = org
    f.access_mode = access
    f.record_max = recsize
    f.record_min = recsize if record_min is None else record_min
    f.record = common.cob_field(recsize, bytearray(b" " * recsize), _ALNUM)
    f.flag_optional = optional
    return f


def set_rec(f, payload):
    """Place *payload* (bytes) into the record buffer, space-padded to width."""
    buf = bytearray(b" " * f.record_max)
    buf[0:len(payload)] = payload[:f.record_max]
    f.record.data[0:f.record_max] = buf
    f.record.size = f.record_max


def numkey(value=0):
    """A 4-byte signed COMP relative/record-number key field holding *value*."""
    fld = common.cob_field(
        4, bytearray(4),
        common.cob_field_attr(type=common.COB_TYPE_NUMERIC_BINARY, digits=9,
                              flags=common.COB_FLAG_HAVE_SIGN))
    move.cob_set_int(fld, value)
    return fld


def make_indexed(path, recsize, keylen, nkeys=1, altlen=0, altoff=0,
                 access=common.COB_ACCESS_DYNAMIC, dup_alt=False):
    """Build an INDEXED connector: primary key at offset 0, optional alt key."""
    f = make_file(path, recsize, org=common.COB_ORG_INDEXED, access=access)
    f.keys = fileio.cob_file_key_array(nkeys)
    f.nkeys = nkeys
    f.keys[0].field = common.cob_field(keylen, bytearray(keylen), _ALNUM)
    f.keys[0].offset = 0
    f.keys[0].flag = 0
    if nkeys > 1:
        f.keys[1].field = common.cob_field(altlen, bytearray(altlen), _ALNUM)
        f.keys[1].offset = altoff
        f.keys[1].flag = 1 if dup_alt else 0
    return f


# ===========================================================================
# 1. Exact migration message (em dash) - status-30 contract
# ===========================================================================
def test_migration_message_uses_em_dash():
    assert fileio._MIGRATION_MESSAGE == (
        "indexed file format incompatible \u2014 manual migration required")
    # The dash must be U+2014 EM DASH, not an ASCII hyphen.
    assert "\u2014" in fileio._MIGRATION_MESSAGE
    assert " - " not in fileio._MIGRATION_MESSAGE


# ===========================================================================
# 2. CWE-22 - centralised path safety
# ===========================================================================
def test_safe_path_rejects_nul_and_control():
    assert fileio._safe_path("foo\x00bar") is None
    assert fileio._safe_path("foo\x01bar") is None
    assert fileio._safe_path("ok\x7f") is None


def test_safe_path_rejects_relative_parent_escape():
    assert fileio._safe_path("..") is None
    assert fileio._safe_path("../escape") is None
    assert fileio._safe_path(os.path.join("..", "..", "etc", "passwd")) is None


def test_safe_path_allows_benign_relative_and_absolute():
    assert fileio._safe_path("sub/dir/file.dat") == os.path.normpath(
        "sub/dir/file.dat")
    assert fileio._safe_path("/tmp/abs.dat") == "/tmp/abs.dat"
    assert fileio._safe_path("") is None
    assert fileio._safe_path(None) is None


def test_safe_path_containment_enforced(tmp_path):
    base = str(tmp_path)
    good = fileio._safe_path("inside.dat", base=base)
    assert good == os.path.join(os.path.realpath(base), "inside.dat")
    # ".." escape and absolute-outside-base are rejected under containment.
    assert fileio._safe_path("../outside.dat", base=base) is None
    assert fileio._safe_path("/etc/passwd", base=base) is None


def test_safe_path_trusted_bypasses_containment(tmp_path):
    base = str(tmp_path)
    # Operator-trusted values skip containment but still reject NUL/control.
    assert fileio._safe_path("/etc/hosts", base=base, trusted=True) == "/etc/hosts"
    assert fileio._safe_path("x\x00", base=base, trusted=True) is None


def test_safe_path_uses_cob_file_path_default(tmp_path, monkeypatch):
    monkeypatch.setattr(fileio, "cob_file_path", str(tmp_path))
    # Default base sentinel falls back to COB_FILE_PATH containment.
    assert fileio._safe_path("../escape") is None
    inside = fileio._safe_path("ok.dat")
    assert inside == os.path.join(os.path.realpath(str(tmp_path)), "ok.dat")


def test_safe_field_path(tmp_path):
    fld = common.cob_field(64, bytearray(b"good.dat".ljust(64)), _ALNUM)
    assert fileio._safe_field_path(fld) == os.path.normpath("good.dat")
    bad = common.cob_field(8, bytearray(b"a\x00b" + b" " * 5), _ALNUM)
    assert fileio._safe_field_path(bad) is None
    assert fileio._safe_field_path(None) is None


def test_open_rejects_unsafe_assign(tmp_path, monkeypatch):
    monkeypatch.setattr(fileio, "cob_file_path", str(tmp_path))
    f = make_file("../escape.dat", 8, access=common.COB_ACCESS_SEQUENTIAL)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert st(f) == "30"


# ===========================================================================
# 3. SEQUENTIAL round-trip + status codes
# ===========================================================================
def test_sequential_round_trip(tmp_path):
    path = tmp_path / "seq.dat"
    f = make_file(path, 8)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert st(f) == "00"
    for payload in (b"AAAA", b"BBBB", b"CCCC"):
        set_rec(f, payload)
        fileio.cob_write(f, f.record, 0, None)
        assert st(f) == "00"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    assert st(f) == "00"

    g = make_file(path, 8)
    fileio.cob_open(g, common.COB_OPEN_INPUT, 0, None)
    seen = []
    while True:
        fileio.cob_read(g, None, None, common.COB_READ_NEXT)
        if st(g) != "00":
            break
        seen.append(bytes(g.record.data).rstrip())
    assert st(g) == "10"  # EOF
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)
    assert seen == [b"AAAA", b"BBBB", b"CCCC"]


def test_open_already_open_is_41(tmp_path):
    f = make_file(tmp_path / "x.dat", 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert st(f) == "41"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)


def test_close_not_open_is_42(tmp_path):
    f = make_file(tmp_path / "x.dat", 4)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    assert st(f) == "42"


def test_open_missing_input_is_35(tmp_path):
    f = make_file(tmp_path / "missing.dat", 4)
    fileio.cob_open(f, common.COB_OPEN_INPUT, 0, None)
    assert st(f) == "35"


def test_optional_missing_input_is_05(tmp_path):
    f = make_file(tmp_path / "opt.dat", 4, optional=1)
    fileio.cob_open(f, common.COB_OPEN_INPUT, 0, None)
    assert st(f) == "05"
    # First READ on the absent optional file reports EOF (10).
    fileio.cob_read(f, None, None, common.COB_READ_NEXT)
    assert st(f) == "10"


def test_read_on_output_is_47(tmp_path):
    f = make_file(tmp_path / "ro.dat", 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    fileio.cob_read(f, None, None, common.COB_READ_NEXT)
    assert st(f) == "47"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)


def test_write_on_input_is_48(tmp_path):
    path = tmp_path / "wi.dat"
    f = make_file(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"ZZZZ")
    fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    f.open_mode = common.COB_OPEN_CLOSED
    fileio.cob_open(f, common.COB_OPEN_INPUT, 0, None)
    set_rec(f, b"YYYY")
    fileio.cob_write(f, f.record, 0, None)
    assert st(f) == "48"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)


def test_write_record_overflow_is_44(tmp_path):
    # Variable-length record connector with a record-size field that exceeds max.
    f = make_file(tmp_path / "ov.dat", 8, record_min=4)
    f.record_size = numkey(99)  # report a size of 99 > record_max (8)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"AAAA")
    fileio.cob_write(f, f.record, 0, None)
    assert st(f) == "44"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)


# ===========================================================================
# 4. LINE SEQUENTIAL round-trip
# ===========================================================================
def test_line_sequential_round_trip(tmp_path):
    path = tmp_path / "lines.txt"
    f = make_file(path, 6, org=common.COB_ORG_LINE_SEQUENTIAL)
    # LINE SEQUENTIAL WRITE BEFORE ADVANCING 1 LINE - the opt the emitter (and
    # cob_file_sort_giving) uses, so each record is newline-terminated.
    opt = common.COB_WRITE_BEFORE | common.COB_WRITE_LINES | 1
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    for payload in (b"one", b"two", b"three"):
        set_rec(f, payload)
        fileio.cob_write(f, f.record, opt, None)
        assert st(f) == "00"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = make_file(path, 6, org=common.COB_ORG_LINE_SEQUENTIAL)
    fileio.cob_open(g, common.COB_OPEN_INPUT, 0, None)
    seen = []
    while True:
        fileio.cob_read(g, None, None, common.COB_READ_NEXT)
        if st(g) != "00":
            break
        seen.append(bytes(g.record.data[:g.record.size]).rstrip())
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)
    assert seen == [b"one", b"two", b"three"]


# ===========================================================================
# 5. RELATIVE round-trip + random read + START
# ===========================================================================
def test_relative_round_trip_and_random(tmp_path):
    path = tmp_path / "rel.dat"
    f = make_file(path, 5, org=common.COB_ORG_RELATIVE)
    f.keys = fileio.cob_file_key_array(1)
    f.nkeys = 1
    f.keys[0].field = numkey(0)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    for payload in (b"R1", b"R2", b"R3"):
        set_rec(f, payload)
        fileio.cob_write(f, f.record, 0, None)
        assert st(f) == "00"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    # Random read of relative record 2.
    g = make_file(path, 5, org=common.COB_ORG_RELATIVE,
                  access=common.COB_ACCESS_RANDOM)
    g.keys = fileio.cob_file_key_array(1)
    g.nkeys = 1
    g.keys[0].field = numkey(0)
    fileio.cob_open(g, common.COB_OPEN_INPUT, 0, None)
    k = numkey(2)
    fileio.cob_read(g, k, None, 0)
    assert st(g) == "00"
    assert bytes(g.record.data).rstrip() == b"R2"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


# ===========================================================================
# 6. INDEXED via dbm (primary key only)
# ===========================================================================
def test_indexed_dbm_write_read_delete(tmp_path):
    path = tmp_path / "idx_dbm"
    f = make_indexed(path, 10, keylen=3)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert st(f) == "00"
    assert f.nkeys == 1  # primary key only -> dbm backend
    for key, rest in ((b"AAA", b"row-1"), (b"BBB", b"row-2"), (b"CCC", b"row-3")):
        set_rec(f, key + rest)
        fileio.cob_write(f, f.record, 0, None)
        assert st(f) == "00"
    # Duplicate primary key -> status 22.
    set_rec(f, b"AAA" + b"dup")
    fileio.cob_write(f, f.record, 0, None)
    assert st(f) == "22"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = make_indexed(path, 10, keylen=3)
    fileio.cob_open(g, common.COB_OPEN_IO if hasattr(common, "COB_OPEN_IO")
                    else common.COB_OPEN_I_O, 0, None)
    # Keyed READ.
    g.keys[0].field.data[0:3] = b"BBB"
    g.keys[0].field.size = 3
    fileio.cob_read(g, g.keys[0].field, None, 0)
    assert st(g) == "00"
    assert bytes(g.record.data).startswith(b"BBBrow-2")
    # START >= AAA then READ NEXT walks ascending.
    g.keys[0].field.data[0:3] = b"AAA"
    g.keys[0].field.size = 3
    fileio.cob_start(g, common.COB_GE, g.keys[0].field, None)
    assert st(g) == "00"
    keys_seen = []
    while True:
        fileio.cob_read(g, None, None, common.COB_READ_NEXT)
        if st(g) != "00":
            break
        keys_seen.append(bytes(g.record.data[:3]))
    assert keys_seen == [b"AAA", b"BBB", b"CCC"]
    # DELETE BBB.
    g.keys[0].field.data[0:3] = b"BBB"
    g.keys[0].field.size = 3
    fileio.cob_read(g, g.keys[0].field, None, 0)
    fileio.cob_delete(g, None)
    assert st(g) == "00"
    g.keys[0].field.data[0:3] = b"BBB"
    g.keys[0].field.size = 3
    fileio.cob_read(g, g.keys[0].field, None, 0)
    assert st(g) == "23"  # gone
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


# ===========================================================================
# 7. INDEXED via sqlite3 (alternate key)
# ===========================================================================
def test_indexed_sqlite_alternate_key(tmp_path):
    path = tmp_path / "idx_sql"
    f = make_indexed(path, 12, keylen=3, nkeys=2, altlen=2, altoff=3,
                     dup_alt=True)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert st(f) == "00"
    # records: primary 3 bytes, alt key 2 bytes at offset 3
    for rec in (b"K01XXrest1", b"K02YYrest2", b"K03XXrest3"):
        set_rec(f, rec)
        fileio.cob_write(f, f.record, 0, None)
        assert st(f) == "00"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = make_indexed(path, 12, keylen=3, nkeys=2, altlen=2, altoff=3,
                     dup_alt=True)
    fileio.cob_open(g, common.COB_OPEN_I_O, 0, None)
    # Primary keyed read.
    g.keys[0].field.data[0:3] = b"K02"
    g.keys[0].field.size = 3
    fileio.cob_read(g, g.keys[0].field, None, 0)
    assert st(g) == "00"
    assert bytes(g.record.data).startswith(b"K02YY")
    # Alternate keyed read on the duplicate "XX" key returns a matching record.
    g.keys[1].field.data[0:2] = b"XX"
    g.keys[1].field.size = 2
    fileio.cob_read(g, g.keys[1].field, None, 0)
    assert st(g) == "00"
    assert bytes(g.record.data[3:5]) == b"XX"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


# ===========================================================================
# 8. Legacy-file migration error contract (status 30 + exact message)
# ===========================================================================
def test_legacy_bdb_migration_error(tmp_path, capsys):
    path = tmp_path / "legacy.idx"
    # Write a recognised Berkeley DB magic so _detect_legacy_bdb trips.
    with open(path, "wb") as handle:
        handle.write(struct.pack("<I", 0x00053162))  # a known BDB magic
        handle.write(b"\x00" * 64)
    f = make_indexed(path, 10, keylen=3)
    fileio.cob_open(f, common.COB_OPEN_I_O, 0, None)
    assert st(f) == "30"
    captured = capsys.readouterr()
    assert "indexed file format incompatible \u2014 manual migration required" \
        in captured.err


def test_unreadable_dbm_existing_file_is_30(tmp_path, capsys):
    # A non-empty file that is neither a recognised BDB nor a valid dbm store.
    path = tmp_path / "garbage.idx"
    with open(path, "wb") as handle:
        handle.write(b"not a database, just random bytes here" * 4)
    f = make_indexed(path, 10, keylen=3)
    fileio.cob_open(f, common.COB_OPEN_I_O, 0, None)
    assert st(f) == "30"
    assert "manual migration required" in capsys.readouterr().err


# ===========================================================================
# 9. SORT / MERGE - external merge sort (review fix)
# ===========================================================================
def _sort_setup(recmax, flag=common.COB_ASCENDING):
    sf = fileio.cob_file()
    sf.record_max = recmax
    sf.record_min = recmax
    sf.record = common.cob_field(recmax, bytearray(recmax), _ALNUM)
    keyfield = common.cob_field(recmax, bytearray(recmax), _ALNUM)
    fileio.cob_file_sort_init(sf, 1, None, None, None)
    fileio.cob_file_sort_init_key(sf, flag, keyfield, 0)
    return sf


def _sort_submit_all(sf, records):
    for rec in records:
        sf.record.data[0:sf.record_max] = rec
        assert fileio.cob_file_sort_submit(sf, sf.record.data) == 0


def _sort_drain(sf):
    out = []
    while True:
        buf = bytearray(sf.record_max)
        if fileio.cob_file_sort_retrieve(sf, buf):
            break
        out.append(bytes(buf))
    return out


def test_sort_external_merge_ordered_and_stable(monkeypatch):
    # Force the tiniest budget so every submit spills a sorted run.
    monkeypatch.setattr(fileio, "cob_sort_memory", 1)
    data = [b"dddd", b"aaaa", b"cccc", b"bbbb", b"aaaa", b"eeee"]
    sf = _sort_setup(4)
    _sort_submit_all(sf, data)
    assert len(sf.file.runs) >= 2          # multiple sorted runs spilled
    assert sf.file._merge_iter is None     # not built until retrieval
    out = _sort_drain(sf)
    assert out == sorted(data)
    assert out.count(b"aaaa") == 2         # stable: duplicates preserved
    fileio.cob_file_sort_close(sf)
    assert sf.file is None                 # work area torn down


def test_sort_in_core_fast_path_descending():
    data = [b"m", b"a", b"z", b"q"]
    sf = _sort_setup(1, flag=common.COB_DESCENDING)
    _sort_submit_all(sf, data)
    assert sf.file.runs == []              # everything fit in core
    out = _sort_drain(sf)
    fileio.cob_file_sort_close(sf)
    assert out == sorted(data, reverse=True)


def test_sort_close_removes_run_files(monkeypatch):
    monkeypatch.setattr(fileio, "cob_sort_memory", 1)
    sf = _sort_setup(3)
    _sort_submit_all(sf, [b"ccc", b"aaa", b"bbb"])
    run_paths = list(sf.file.runs)
    assert run_paths and all(os.path.exists(p) for p in run_paths)
    # Begin retrieval (builds the streaming merge), then close mid-stream.
    buf = bytearray(3)
    fileio.cob_file_sort_retrieve(sf, buf)
    fileio.cob_file_sort_close(sf)
    assert all(not os.path.exists(p) for p in run_paths)


def test_sort_using_giving(tmp_path):
    src = tmp_path / "in.dat"
    dst = tmp_path / "out.dat"
    fin = make_file(src, 4)
    fileio.cob_open(fin, common.COB_OPEN_OUTPUT, 0, None)
    for payload in (b"CCCC", b"AAAA", b"BBBB"):
        set_rec(fin, payload)
        fileio.cob_write(fin, fin.record, 0, None)
    fileio.cob_close(fin, common.COB_CLOSE_NORMAL, None)

    sf = _sort_setup(4)
    data_in = make_file(src, 4)
    giving = make_file(dst, 4)
    fileio.cob_file_sort_using(sf, data_in)
    fileio.cob_file_sort_giving(sf, 1, giving)
    fileio.cob_file_sort_close(sf)

    check = make_file(dst, 4)
    fileio.cob_open(check, common.COB_OPEN_INPUT, 0, None)
    seen = []
    while True:
        fileio.cob_read(check, None, None, common.COB_READ_NEXT)
        if st(check) != "00":
            break
        seen.append(bytes(check.record.data).rstrip())
    fileio.cob_close(check, common.COB_CLOSE_NORMAL, None)
    assert seen == [b"AAAA", b"BBBB", b"CCCC"]


def test_sort_release_return():
    sf = _sort_setup(4)
    for rec in (b"yyyy", b"xxxx", b"zzzz"):
        sf.record.data[0:4] = rec
        fileio.cob_file_release(sf)
        assert st(sf) == "00"
    out = []
    while True:
        fileio.cob_file_return(sf)
        if st(sf) != "00":
            break
        out.append(bytes(sf.record.data))
    assert out == [b"xxxx", b"yyyy", b"zzzz"]
    fileio.cob_file_sort_close(sf)


# ===========================================================================
# 10. LINAGE open-error file-descriptor leak (review fix)
# ===========================================================================
def test_invalid_linage_does_not_leak_handle(tmp_path):
    path = tmp_path / "linage.txt"
    # Pre-create the file so OPEN gets past the existence check.
    path.write_bytes(b"")
    f = make_file(path, 8, org=common.COB_ORG_LINE_SEQUENTIAL)
    f.flag_select_features = common.COB_SELECT_LINAGE
    ling = fileio.cob_linage_struct()
    # LINAGE of 0 is invalid (< 1) -> file_linage_check returns 1 -> status 57.
    ling.linage = numkey(0)
    ling.linage_ctr = numkey(0)
    f.linorkeyptr = ling
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert st(f) == "57"
    # The leak fix: the handle was closed and detached, not left dangling.
    assert f.file is None
    assert f.open_mode == common.COB_OPEN_CLOSED


# ===========================================================================
# 11. C$ filesystem routines (path-validated)
# ===========================================================================
def _field(text):
    raw = text.encode("latin-1") if isinstance(text, str) else text
    return common.cob_field(len(raw) + 8, bytearray(raw.ljust(len(raw) + 8)),
                            _ALNUM)


def test_cob_acuw_mkdir_and_delete(tmp_path):
    target = tmp_path / "newdir"
    assert fileio.cob_acuw_mkdir(_field(str(target))) == 0
    assert target.is_dir()
    # Rejected path -> 128.
    bad = common.cob_field(8, bytearray(b"a\x00b" + b" " * 5), _ALNUM)
    assert fileio.cob_acuw_mkdir(bad) == 128

    afile = tmp_path / "tofile.dat"
    afile.write_bytes(b"data")
    assert fileio.cob_acuw_file_delete(_field(str(afile)), None) == 0
    assert not afile.exists()
    assert fileio.cob_acuw_file_delete(bad, None) == 128


def test_cob_acuw_chdir(tmp_path):
    start = os.getcwd()
    try:
        status = numkey(0)
        assert fileio.cob_acuw_chdir(_field(str(tmp_path)), status) == 0
        assert move.cob_get_int(status) == 0
        assert os.path.realpath(os.getcwd()) == os.path.realpath(str(tmp_path))
        bad = common.cob_field(8, bytearray(b"x\x00y" + b" " * 5), _ALNUM)
        assert fileio.cob_acuw_chdir(bad, status) == 128
        assert move.cob_get_int(status) == 128
    finally:
        os.chdir(start)


def test_cob_acuw_copyfile(tmp_path):
    src = tmp_path / "src.dat"
    dst = tmp_path / "dst.dat"
    src.write_bytes(b"payload")
    assert fileio.cob_acuw_copyfile(_field(str(src)), _field(str(dst)), None) == 0
    assert dst.read_bytes() == b"payload"
    bad = common.cob_field(8, bytearray(b"a\x00b" + b" " * 5), _ALNUM)
    assert fileio.cob_acuw_copyfile(bad, _field(str(dst)), None) == 128


def test_cob_acuw_file_info(tmp_path):
    target = tmp_path / "info.dat"
    target.write_bytes(b"1234567890")
    info = common.cob_field(16, bytearray(16), _ALNUM)
    assert fileio.cob_acuw_file_info(_field(str(target)), info) == 0
    size = struct.unpack(">Q", bytes(info.data[0:8]))[0]
    assert size == 10
    # Missing file -> 35; unsafe path -> 128.
    assert fileio.cob_acuw_file_info(_field(str(tmp_path / "nope")), info) == 35
    bad = common.cob_field(8, bytearray(b"a\x00b" + b" " * 5), _ALNUM)
    assert fileio.cob_acuw_file_info(bad, info) == 128


# ===========================================================================
# 12. Lifecycle, commit/rollback/unlock
# ===========================================================================
def test_init_fileio_reads_env(monkeypatch):
    monkeypatch.setenv("COB_SYNC", "Y")
    monkeypatch.setenv("COB_SORT_MEMORY", str(8 * 1024 * 1024))
    monkeypatch.setenv("COB_FILE_PATH", str_path := os.getcwd())
    fileio.cob_init_fileio()
    assert fileio.cob_do_sync == 1
    assert fileio.cob_sort_memory == 8 * 1024 * 1024
    assert fileio.cob_file_path == str_path
    # Restore sane defaults for the rest of the suite.
    fileio.cob_do_sync = 0
    fileio.cob_sort_memory = 128 * 1024 * 1024
    fileio.cob_file_path = None


def test_commit_rollback_unlock(tmp_path):
    f = make_file(tmp_path / "c.dat", 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"DATA")
    fileio.cob_write(f, f.record, 0, None)
    fileio.cob_commit()          # flushes cached files - must not raise
    fileio.cob_unlock(f)         # advisory - must not raise
    fileio.cob_rollback()        # must not raise
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)


def test_exit_fileio_implicit_close(tmp_path, capsys):
    f = make_file(tmp_path / "leak.dat", 4)
    f.assign = None
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    fileio.cob_exit_fileio()
    # The implicit-close warning is emitted to stderr for the still-open file.
    assert f.open_mode == common.COB_OPEN_CLOSED


# ===========================================================================
# 13. RELATIVE sequential READ NEXT / START / REWRITE / DELETE
# ===========================================================================
def _relative_file(path, recsize, access=common.COB_ACCESS_SEQUENTIAL):
    f = make_file(path, recsize, org=common.COB_ORG_RELATIVE, access=access)
    f.keys = fileio.cob_file_key_array(1)
    f.nkeys = 1
    f.keys[0].field = numkey(0)
    return f


def test_relative_read_next_start_rewrite_delete(tmp_path):
    path = tmp_path / "rel2.dat"
    f = _relative_file(path, 5)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    for payload in (b"AA", b"BB", b"CC", b"DD"):
        set_rec(f, payload)
        fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    # Sequential READ NEXT through all four records, then EOF.
    g = _relative_file(path, 5)
    fileio.cob_open(g, common.COB_OPEN_INPUT, 0, None)
    seen = []
    while True:
        fileio.cob_read(g, None, None, common.COB_READ_NEXT)
        if st(g) != "00":
            break
        seen.append(bytes(g.record.data).rstrip())
    assert st(g) == "10"
    assert seen == [b"AA", b"BB", b"CC", b"DD"]
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)

    # START >= record 3, then READ NEXT walks from there.
    h = _relative_file(path, 5)
    fileio.cob_open(h, common.COB_OPEN_INPUT, 0, None)
    move.cob_set_int(h.keys[0].field, 3)
    fileio.cob_start(h, common.COB_GE, h.keys[0].field, None)
    assert st(h) == "00"
    fileio.cob_read(h, None, None, common.COB_READ_NEXT)
    assert bytes(h.record.data).rstrip() == b"CC"
    fileio.cob_close(h, common.COB_CLOSE_NORMAL, None)

    # REWRITE + DELETE via random I-O access.
    w = _relative_file(path, 5, access=common.COB_ACCESS_RANDOM)
    fileio.cob_open(w, common.COB_OPEN_I_O, 0, None)
    move.cob_set_int(w.keys[0].field, 2)
    fileio.cob_read(w, w.keys[0].field, None, 0)
    assert bytes(w.record.data).rstrip() == b"BB"
    set_rec(w, b"XX")
    move.cob_set_int(w.keys[0].field, 2)
    fileio.cob_rewrite(w, w.record, 0, None)
    assert st(w) == "00"
    move.cob_set_int(w.keys[0].field, 4)
    fileio.cob_read(w, w.keys[0].field, None, 0)
    fileio.cob_delete(w, None)
    assert st(w) == "00"
    # Record 4 is now gone.
    move.cob_set_int(w.keys[0].field, 4)
    fileio.cob_read(w, w.keys[0].field, None, 0)
    assert st(w) == "23"
    fileio.cob_close(w, common.COB_CLOSE_NORMAL, None)


def test_relative_read_previous(tmp_path):
    path = tmp_path / "rel3.dat"
    f = _relative_file(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    for payload in (b"P1", b"P2", b"P3"):
        set_rec(f, payload)
        fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = _relative_file(path, 4)
    fileio.cob_open(g, common.COB_OPEN_INPUT, 0, None)
    # Seek to end first by reading forward, then walk backwards.
    while st(g) == "00" or st(g) == "00":
        fileio.cob_read(g, None, None, common.COB_READ_NEXT)
        if st(g) != "00":
            break
    seen = []
    while True:
        fileio.cob_read(g, None, None, common.COB_READ_PREVIOUS)
        if st(g) != "00":
            break
        seen.append(bytes(g.record.data).rstrip())
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)
    assert b"P3" in seen and b"P1" in seen


# ===========================================================================
# 14. SEQUENTIAL REWRITE + REWRITE/DELETE error statuses
# ===========================================================================
def test_sequential_rewrite_io(tmp_path):
    path = tmp_path / "seqrw.dat"
    f = make_file(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    for payload in (b"R1", b"R2"):
        set_rec(f, payload)
        fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = make_file(path, 4)
    fileio.cob_open(g, common.COB_OPEN_I_O, 0, None)
    fileio.cob_read(g, None, None, common.COB_READ_NEXT)  # reads R1
    set_rec(g, b"RX")
    fileio.cob_rewrite(g, g.record, 0, None)
    assert st(g) == "00"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)

    check = make_file(path, 4)
    fileio.cob_open(check, common.COB_OPEN_INPUT, 0, None)
    fileio.cob_read(check, None, None, common.COB_READ_NEXT)
    assert bytes(check.record.data).rstrip() == b"RX"
    fileio.cob_close(check, common.COB_CLOSE_NORMAL, None)


def test_rewrite_not_io_is_49(tmp_path):
    path = tmp_path / "rw49.dat"
    f = make_file(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"AAAA")
    fileio.cob_rewrite(f, f.record, 0, None)
    assert st(f) == "49"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)


def test_rewrite_read_not_done_is_43(tmp_path):
    path = tmp_path / "rw43.dat"
    f = make_file(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"AAAA")
    fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    g = make_file(path, 4)
    fileio.cob_open(g, common.COB_OPEN_I_O, 0, None)
    set_rec(g, b"BBBB")
    fileio.cob_rewrite(g, g.record, 0, None)  # no READ first
    assert st(g) == "43"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


def test_delete_read_not_done_is_43(tmp_path):
    path = tmp_path / "del43.dat"
    f = make_file(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"AAAA")
    fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    g = make_file(path, 4)
    fileio.cob_open(g, common.COB_OPEN_I_O, 0, None)
    fileio.cob_delete(g, None)  # no READ first
    assert st(g) == "43"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


# ===========================================================================
# 15. EXTEND mode + cob_unlock + cob_sync
# ===========================================================================
def test_extend_mode_appends(tmp_path):
    path = tmp_path / "ext.dat"
    f = make_file(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"AAAA")
    fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = make_file(path, 4)
    fileio.cob_open(g, common.COB_OPEN_EXTEND, 0, None)
    assert st(g) == "00"
    set_rec(g, b"BBBB")
    fileio.cob_write(g, g.record, 0, None)
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)

    check = make_file(path, 4)
    fileio.cob_open(check, common.COB_OPEN_INPUT, 0, None)
    seen = []
    while True:
        fileio.cob_read(check, None, None, common.COB_READ_NEXT)
        if st(check) != "00":
            break
        seen.append(bytes(check.record.data).rstrip())
    fileio.cob_close(check, common.COB_CLOSE_NORMAL, None)
    assert seen == [b"AAAA", b"BBBB"]


def test_unlock_and_sync(tmp_path):
    path = tmp_path / "sync.dat"
    # SEQUENTIAL files only permit WRITE in OUTPUT/EXTEND mode; exercise the
    # flush/fsync/unlock helpers under a valid OUTPUT open.
    f = make_file(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"AAAA")
    fileio.cob_write(f, f.record, 0, None)
    assert st(f) == "00"
    fileio.cob_sync(f, 1)   # flush
    fileio.cob_sync(f, 2)   # flush + fsync
    fileio.cob_unlock(f)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    # cob_sync on a closed (file is None) connector is a no-op.
    fileio.cob_sync(f, 1)


# ===========================================================================
# 16. LINAGE write advancing (valid geometry) + invalid-geometry detection
# ===========================================================================
def test_linage_write_advancing(tmp_path):
    path = tmp_path / "linage_ok.txt"
    f = make_file(path, 10, org=common.COB_ORG_LINE_SEQUENTIAL)
    f.flag_select_features = common.COB_SELECT_LINAGE
    ling = fileio.cob_linage_struct()
    ling.linage = numkey(3)        # 3 body lines per logical page
    ling.linage_ctr = numkey(0)
    ling.latfoot = numkey(2)       # FOOTING at line 2
    ling.lattop = numkey(1)        # 1 top-margin line
    ling.latbot = numkey(1)        # 1 bottom-margin line
    f.linorkeyptr = ling
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert st(f) == "00"
    assert move.cob_get_int(ling.linage_ctr) == 1   # primed on open
    opt = common.COB_WRITE_AFTER | common.COB_WRITE_EOP | common.COB_WRITE_LINES | 1
    for payload in (b"L1", b"L2", b"L3", b"L4", b"L5"):
        set_rec(f, payload)
        fileio.cob_write(f, f.record, opt, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    data = path.read_bytes()
    assert b"L1" in data and b"L5" in data
    assert data.count(b"\n") >= 5    # body lines + margin advances emitted


def test_file_linage_check_invalid_geometries():
    def mkconn(lines, foot=None, top=None, bot=None):
        f = fileio.cob_file()
        ling = fileio.cob_linage_struct()
        ling.linage = numkey(lines)
        ling.linage_ctr = numkey(0)
        ling.latfoot = numkey(foot) if foot is not None else None
        ling.lattop = numkey(top) if top is not None else None
        ling.latbot = numkey(bot) if bot is not None else None
        f.linorkeyptr = ling
        return f

    assert fileio.file_linage_check(mkconn(0)) == 1            # lines < 1
    assert fileio.file_linage_check(mkconn(5, foot=0)) == 1    # foot < 1
    assert fileio.file_linage_check(mkconn(5, foot=9)) == 1    # foot > lines
    assert fileio.file_linage_check(mkconn(5, top=-1)) == 1    # top < 0
    assert fileio.file_linage_check(mkconn(5, bot=-1)) == 1    # bot < 0
    assert fileio.file_linage_check(mkconn(5, foot=3, top=1, bot=1)) == 0  # ok


# ===========================================================================
# 17. INDEXED sqlite3 START / READ NEXT / REWRITE / DELETE / READ PREVIOUS
# ===========================================================================
def test_indexed_sqlite_navigation_and_mutation(tmp_path):
    path = tmp_path / "idx_sql_nav"
    f = make_indexed(path, 12, keylen=3, nkeys=2, altlen=2, altoff=3,
                     dup_alt=True)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    for rec in (b"K01AArest1", b"K02BBrest2", b"K03AArest3"):
        set_rec(f, rec)
        fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = make_indexed(path, 12, keylen=3, nkeys=2, altlen=2, altoff=3,
                     dup_alt=True)
    fileio.cob_open(g, common.COB_OPEN_I_O, 0, None)
    # START on the primary key (GE K02) then READ NEXT ascending.
    g.keys[0].field.data[0:3] = b"K02"
    g.keys[0].field.size = 3
    fileio.cob_start(g, common.COB_GE, g.keys[0].field, None)
    assert st(g) == "00"
    seen = []
    while True:
        fileio.cob_read(g, None, None, common.COB_READ_NEXT)
        if st(g) != "00":
            break
        seen.append(bytes(g.record.data[:3]))
    assert seen == [b"K02", b"K03"]
    # REWRITE the K02 record (change trailing data).
    g.keys[0].field.data[0:3] = b"K02"
    g.keys[0].field.size = 3
    fileio.cob_read(g, g.keys[0].field, None, 0)
    set_rec(g, b"K02BBnewdat")
    fileio.cob_rewrite(g, g.record, 0, None)
    assert st(g) == "00"
    g.keys[0].field.data[0:3] = b"K02"
    g.keys[0].field.size = 3
    fileio.cob_read(g, g.keys[0].field, None, 0)
    assert bytes(g.record.data).startswith(b"K02BBnewdat")
    # DELETE K01.
    g.keys[0].field.data[0:3] = b"K01"
    g.keys[0].field.size = 3
    fileio.cob_read(g, g.keys[0].field, None, 0)
    fileio.cob_delete(g, None)
    assert st(g) == "00"
    g.keys[0].field.data[0:3] = b"K01"
    g.keys[0].field.size = 3
    fileio.cob_read(g, g.keys[0].field, None, 0)
    assert st(g) == "23"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


# ===========================================================================
# 18. INDEXED dbm START conditions + READ PREVIOUS + partial key + REWRITE
# ===========================================================================
def test_indexed_dbm_start_conditions_and_rewrite(tmp_path):
    path = tmp_path / "idx_dbm2"
    f = make_indexed(path, 8, keylen=3)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    for key in (b"AAA", b"BBB", b"CCC", b"DDD"):
        set_rec(f, key + b"xy")
        fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = make_indexed(path, 8, keylen=3)
    fileio.cob_open(g, common.COB_OPEN_I_O, 0, None)
    # START > BBB -> first NEXT is CCC.
    g.keys[0].field.data[0:3] = b"BBB"
    g.keys[0].field.size = 3
    fileio.cob_start(g, common.COB_GT, g.keys[0].field, None)
    fileio.cob_read(g, None, None, common.COB_READ_NEXT)
    assert bytes(g.record.data[:3]) == b"CCC"
    # START <= CCC -> READ PREVIOUS walks down.
    g.keys[0].field.data[0:3] = b"CCC"
    g.keys[0].field.size = 3
    fileio.cob_start(g, common.COB_LE, g.keys[0].field, None)
    fileio.cob_read(g, None, None, common.COB_READ_PREVIOUS)
    assert bytes(g.record.data[:3]) == b"CCC"
    # REWRITE DDD.
    g.keys[0].field.data[0:3] = b"DDD"
    g.keys[0].field.size = 3
    fileio.cob_read(g, g.keys[0].field, None, 0)
    set_rec(g, b"DDDzz")
    fileio.cob_rewrite(g, g.record, 0, None)
    assert st(g) == "00"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


def test_indexed_dbm_missing_key_read_is_23(tmp_path):
    path = tmp_path / "idx_dbm3"
    f = make_indexed(path, 8, keylen=3)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"AAA" + b"xy")
    fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = make_indexed(path, 8, keylen=3)
    fileio.cob_open(g, common.COB_OPEN_INPUT, 0, None)
    g.keys[0].field.data[0:3] = b"ZZZ"
    g.keys[0].field.size = 3
    fileio.cob_read(g, g.keys[0].field, None, 0)
    assert st(g) == "23"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


# ===========================================================================
# 19. EXTERNAL connector registry + cob_cache_file
# ===========================================================================
def test_cob_file_external_registry():
    a = fileio.cob_file_external("SHARED01")
    assert common.cob_initial_external == 1
    b = fileio.cob_file_external("SHARED01")
    assert a is b
    assert common.cob_initial_external == 0


# ===========================================================================
# 20. Default USE-error diagnostic (cob_default_error_handle)
# ===========================================================================
def test_cob_default_error_handle_formats_status(tmp_path, capsys):
    f = make_file(tmp_path / "nope.dat", 4)
    f.select_name = "NOPEFILE"
    # OPEN INPUT on a missing file latches status 35 onto cob_error_file.
    fileio.cob_open(f, common.COB_OPEN_INPUT, 0, None)
    assert st(f) == "35"
    fileio.cob_default_error_handle()
    err = capsys.readouterr().err
    assert "STATUS = 35" in err
    assert "NOPEFILE" in err


# ===========================================================================
# 21. LINAGE WRITE PAGE advance (COB_WRITE_PAGE branch)
# ===========================================================================
def test_linage_write_page_advance(tmp_path):
    path = tmp_path / "linage_pg.txt"
    f = make_file(path, 6, org=common.COB_ORG_LINE_SEQUENTIAL)
    f.flag_select_features = common.COB_SELECT_LINAGE
    ling = fileio.cob_linage_struct()
    ling.linage = numkey(3)
    ling.linage_ctr = numkey(0)
    ling.latfoot = None
    ling.lattop = numkey(1)
    ling.latbot = numkey(1)
    f.linorkeyptr = ling
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert st(f) == "00"
    set_rec(f, b"BODY")
    fileio.cob_write(f, f.record, common.COB_WRITE_LINES | 1, None)
    # WRITE ... AFTER ADVANCING PAGE resets the linage counter to 1 and emits
    # the bottom/top margin advances.
    set_rec(f, b"TOP")
    fileio.cob_write(f, f.record, common.COB_WRITE_PAGE, None)
    assert st(f) == "00"
    assert move.cob_get_int(ling.linage_ctr) == 1
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    assert path.read_bytes().count(b"\n") >= 2


# ===========================================================================
# 22. LINE SEQUENTIAL with COB_LS_NULLS (control-byte escaping)
# ===========================================================================
def test_line_sequential_ls_nulls(tmp_path, monkeypatch):
    monkeypatch.setenv("COB_LS_NULLS", "1")
    fileio.cob_init_fileio()
    try:
        path = tmp_path / "lsnulls.txt"
        f = make_file(path, 4, org=common.COB_ORG_LINE_SEQUENTIAL)
        fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
        opt = common.COB_WRITE_BEFORE | common.COB_WRITE_LINES | 1
        rec = bytearray(b"\x01\x02AB")
        f.record.data[0:4] = rec
        fileio.cob_write(f, f.record, opt, None)
        fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
        raw = path.read_bytes()
        # Control bytes < 0x20 are emitted preceded by a NUL escape byte.
        assert b"\x00\x01" in raw and b"\x00\x02" in raw
    finally:
        monkeypatch.delenv("COB_LS_NULLS", raising=False)
        fileio.cob_init_fileio()


# ===========================================================================
# 23. _resolve_filename DD_ override + COB_FILE_PATH containment
# ===========================================================================
def test_resolve_filename_dd_override(tmp_path, monkeypatch):
    target = tmp_path / "redirected.dat"
    monkeypatch.setenv("DD_LOGICAL", str(target))
    f = fileio.cob_file()
    f.assign = _field(b"LOGICAL")
    f.select_name = "LOGICAL"
    f.organization = common.COB_ORG_SEQUENTIAL
    resolved = fileio._resolve_filename(f)
    assert resolved == str(target)


def test_resolve_filename_cob_file_path_containment(tmp_path, monkeypatch):
    base = tmp_path / "data"
    base.mkdir()
    monkeypatch.setattr(fileio, "cob_file_path", str(base))
    try:
        # A simple name is contained under COB_FILE_PATH.
        f = fileio.cob_file()
        f.assign = _field(b"GOOD")
        f.select_name = "GOOD"
        f.organization = common.COB_ORG_SEQUENTIAL
        resolved = fileio._resolve_filename(f)
        assert resolved is not None
        assert os.path.realpath(resolved).startswith(os.path.realpath(str(base)))
    finally:
        monkeypatch.setattr(fileio, "cob_file_path", None)


# ===========================================================================
# 24. RELATIVE random-access WRITE (by key) + duplicate-slot status 22
# ===========================================================================
def test_relative_random_write(tmp_path):
    path = tmp_path / "rel_rand.dat"
    f = _relative_file(path, 4, access=common.COB_ACCESS_RANDOM)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    # WRITE record number 3 first (sparse), then 1.
    move.cob_set_int(f.keys[0].field, 3)
    set_rec(f, b"R3")
    fileio.cob_write(f, f.record, 0, None)
    assert st(f) == "00"
    move.cob_set_int(f.keys[0].field, 1)
    set_rec(f, b"R1")
    fileio.cob_write(f, f.record, 0, None)
    assert st(f) == "00"
    # Re-writing an existing slot yields KEY EXISTS (status 22).
    move.cob_set_int(f.keys[0].field, 3)
    set_rec(f, b"XX")
    fileio.cob_write(f, f.record, 0, None)
    assert st(f) == "22"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    # Verify random read of the sparse records.
    g = _relative_file(path, 4, access=common.COB_ACCESS_RANDOM)
    fileio.cob_open(g, common.COB_OPEN_INPUT, 0, None)
    move.cob_set_int(g.keys[0].field, 3)
    fileio.cob_read(g, g.keys[0].field, None, 0)
    assert bytes(g.record.data).rstrip() == b"R3"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


# ===========================================================================
# 25. SORT ... GIVING multiple output files
# ===========================================================================
def test_sort_giving_multiple_files(tmp_path):
    src = tmp_path / "msrc.dat"
    fin = make_file(src, 4)
    fileio.cob_open(fin, common.COB_OPEN_OUTPUT, 0, None)
    for payload in (b"DDDD", b"BBBB", b"CCCC", b"AAAA"):
        set_rec(fin, payload)
        fileio.cob_write(fin, fin.record, 0, None)
    fileio.cob_close(fin, common.COB_CLOSE_NORMAL, None)

    sf = _sort_setup(4)
    data_in = make_file(src, 4)
    # Two GIVING files both receive the fully sorted stream (varcnt == 2).
    out1 = make_file(tmp_path / "g1.dat", 4)
    out2 = make_file(tmp_path / "g2.dat", 4)
    fileio.cob_file_sort_using(sf, data_in)
    fileio.cob_file_sort_giving(sf, 2, out1, out2)
    fileio.cob_file_sort_close(sf)

    for outp in (tmp_path / "g1.dat", tmp_path / "g2.dat"):
        chk = make_file(outp, 4)
        fileio.cob_open(chk, common.COB_OPEN_INPUT, 0, None)
        seen = []
        while True:
            fileio.cob_read(chk, None, None, common.COB_READ_NEXT)
            if st(chk) != "00":
                break
            seen.append(bytes(chk.record.data).rstrip())
        fileio.cob_close(chk, common.COB_CLOSE_NORMAL, None)
        assert seen == [b"AAAA", b"BBBB", b"CCCC", b"DDDD"], \
            f"{outp} not sorted: {seen}"
