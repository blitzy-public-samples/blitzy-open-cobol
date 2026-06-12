"""Unit tests for :mod:`libcob_py.fileio` - the pure-Python port of ``libcob/fileio.c``.

This suite is one of the thirteen test modules in the new ``tests/libcob_py/``
package mandated by the C->Python backend refactor (AAP sections 0.2.1, 0.4.1,
0.6.3 and 0.7.1).  It verifies the runtime file-I/O subsystem against the
behaviour of the original C runtime:

* the SEQUENTIAL, LINE SEQUENTIAL, RELATIVE and INDEXED organizations;
* the two-character COBOL FILE STATUS model
  (``00``/``02``/``05``/``10``/``22``/``23``/``30``/``35`` and the
  ``41``/``42``/``43``/``44``/``47``/``48``/``49`` sequencing/mode codes);
* the ``dbm`` (primary-key-only) versus ``sqlite3`` (alternate-key / complex)
  indexed-backend routing that replaces Berkeley DB (AAP section 0.6.3);
* the **HARD** indexed-file migration error contract - an unreadable legacy
  Berkeley DB store yields file status ``30`` *and* logs the exact message
  ``indexed file format incompatible - manual migration required`` with no
  silent data loss (the dash is a U+2014 EM DASH; it is a byte-for-byte
  contract);
* SORT and MERGE (the latter realised through the same multi-``USING`` sort
  machinery the C runtime used);
* the ``C$`` filesystem helper routines; and
* the runtime initialisation / shutdown lifecycle.

HARD CONSTRAINTS honoured by this module (AAP 0.5 / 0.7.1):

* **Standard library only.**  The runtime under test introduces ZERO
  third-party dependencies, so this test imports nothing beyond ``os`` /
  ``struct`` and the development-only ``pytest`` framework.
* **Whitelist-clean imports.**  Only the two runtime modules in this file's
  declared ``depends_on_files`` - :mod:`libcob_py.fileio` and
  :mod:`libcob_py.common` - are imported.  Numeric RELATIVE/record keys are
  built through the in-scope ``fileio._cob_set_int`` / ``fileio._cob_get_int``
  helpers (which themselves route to :mod:`libcob_py.move`) so this test never
  imports ``move`` directly.
* **Temp-dir isolation.**  Every file artifact is created under the
  ``work_dir`` / ``tmp_path`` fixtures, so the immutable source tree (and the
  read-only ``tests/data-rep.src`` / ``tests/cobol85`` oracles) is never
  touched.
* **Clean skip.**  ``pytest.importorskip`` at module top yields a clean skip
  when the parallel-built runtime is not yet importable.
"""
import os
import struct
import subprocess
import sys

import pytest

# Whitelist-clean runtime imports (depends_on_files): a clean skip is produced
# when the parallel-built ``libcob_py`` runtime is not importable yet.
fileio = pytest.importorskip("libcob_py.fileio")
common = pytest.importorskip("libcob_py.common")


# ===========================================================================
# Shared helpers - build the connector / record / key structures exactly as
# the rewritten emitter (cobc/codegen.c ``output_file_initialization``) does.
# ===========================================================================
#: A reusable ALPHANUMERIC attribute for record areas and character keys.
_ALNUM = common.cob_field_attr(type=common.COB_TYPE_ALPHANUMERIC)


def st(f):
    """Return the connector's two-character COBOL FILE STATUS as a ``str``."""
    return bytes(f.file_status[:2]).decode("latin-1")


def make_file(path, recsize, org=common.COB_ORG_SEQUENTIAL,
              access=common.COB_ACCESS_SEQUENTIAL, optional=0, record_min=None):
    """Build a :class:`~libcob_py.fileio.cob_file` connector for *path*.

    The connector is wired the way the emitter wires a ``SELECT`` clause: an
    absolute ASSIGN name (``select_name``), the organization / access mode, a
    fixed-width record area and the OPTIONAL flag.
    """
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
    """Place *payload* in the record buffer, space-padded to the record width."""
    buf = bytearray(b" " * f.record_max)
    buf[0:len(payload)] = payload[:f.record_max]
    f.record.data[0:f.record_max] = buf
    f.record.size = f.record_max


def numkey(value=0):
    """A 4-byte signed COMP RELATIVE/record-number key field holding *value*.

    The integer is encoded through the in-scope ``fileio._cob_set_int`` helper
    (which routes to :mod:`libcob_py.move`), so this test never imports the
    ``move`` module directly - keeping every import inside the declared
    ``depends_on_files`` whitelist.
    """
    fld = common.cob_field(
        4, bytearray(4),
        common.cob_field_attr(type=common.COB_TYPE_NUMERIC_BINARY, digits=9,
                              flags=common.COB_FLAG_HAVE_SIGN))
    fileio._cob_set_int(fld, value)
    return fld


def set_key_int(field, value):
    """Set a numeric RELATIVE key *field* to *value* (whitelist-clean)."""
    fileio._cob_set_int(field, value)


def get_key_int(field):
    """Read the integer held by a numeric key *field* (whitelist-clean)."""
    return fileio._cob_get_int(field)


def make_relative(path, recsize, access=common.COB_ACCESS_SEQUENTIAL):
    """Build a RELATIVE connector with a single numeric relative key."""
    f = make_file(path, recsize, org=common.COB_ORG_RELATIVE, access=access)
    f.keys = fileio.cob_file_key_array(1)
    f.nkeys = 1
    f.keys[0].field = numkey(0)
    return f


def make_indexed(path, recsize, keylen, nkeys=1, altlen=0, altoff=0,
                 access=common.COB_ACCESS_DYNAMIC, dup_alt=False):
    """Build an INDEXED connector: primary key at offset 0, optional alt key.

    ``nkeys == 1`` (primary only) routes the runtime to the ``dbm`` backend;
    ``nkeys > 1`` (an ALTERNATE RECORD KEY) routes it to ``sqlite3``.  The
    alternate key carries the WITH DUPLICATES flag when *dup_alt* is true.
    """
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


def set_search_key(field, value):
    """Load *value* bytes into a character search-key *field* and size it."""
    raw = value if isinstance(value, (bytes, bytearray)) else value.encode("latin-1")
    field.data[0:len(raw)] = raw
    field.size = len(raw)


def char_field(text, pad=8):
    """A character field big enough to hold *text* plus *pad* trailing spaces."""
    raw = text.encode("latin-1") if isinstance(text, str) else bytes(text)
    return common.cob_field(len(raw) + pad,
                            bytearray(raw.ljust(len(raw) + pad)), _ALNUM)


# A ``WRITE BEFORE ADVANCING 1 LINE`` option (newline-terminated records) - the
# option the emitter and ``cob_file_sort_giving`` use for LINE SEQUENTIAL.
_LS_WRITE_OPT = common.COB_WRITE_BEFORE | common.COB_WRITE_LINES | 1


def _open_io(f):
    """OPEN *f* I-O, tolerating either spelling of the I-O open-mode constant."""
    mode = getattr(common, "COB_OPEN_IO", None)
    if mode is None:
        mode = common.COB_OPEN_I_O
    fileio.cob_open(f, mode, 0, None)


# ===========================================================================
# Phase 1 - SEQUENTIAL round-trip
# ===========================================================================
def test_sequential_write_read(work_dir):
    """OPEN OUTPUT a SEQUENTIAL file, WRITE 3 fixed-length records, CLOSE; then
    OPEN INPUT and READ them back in order (status ``00``), with a READ past
    the last record returning EOF (status ``10``).  Record bytes must round-trip
    byte-for-byte.
    """
    path = work_dir / "seq.dat"
    payloads = [b"AAAA", b"BBBB", b"CCCC"]

    writer = make_file(path, 4)
    fileio.cob_open(writer, common.COB_OPEN_OUTPUT, 0, None)
    assert st(writer) == "00"
    for payload in payloads:
        set_rec(writer, payload)
        fileio.cob_write(writer, writer.record, 0, None)
        assert st(writer) == "00"
    fileio.cob_close(writer, common.COB_CLOSE_NORMAL, None)
    assert st(writer) == "00"

    reader = make_file(path, 4)
    fileio.cob_open(reader, common.COB_OPEN_INPUT, 0, None)
    assert st(reader) == "00"
    seen = []
    for _ in range(len(payloads)):
        fileio.cob_read(reader, None, None, common.COB_READ_NEXT)
        assert st(reader) == "00"
        seen.append(bytes(reader.record.data[:reader.record.size]))
    # One READ past the final record reports end-of-file (status 10).
    fileio.cob_read(reader, None, None, common.COB_READ_NEXT)
    assert st(reader) == "10"
    fileio.cob_close(reader, common.COB_CLOSE_NORMAL, None)

    assert seen == payloads


def test_open_input_missing_file(work_dir):
    """OPEN INPUT a non-existent (non-OPTIONAL) file yields file status ``35``."""
    f = make_file(work_dir / "does_not_exist.dat", 8)
    fileio.cob_open(f, common.COB_OPEN_INPUT, 0, None)
    assert st(f) == "35"


def test_sequential_status_sequencing(work_dir):
    """Open/close sequencing statuses: ``41`` already-open, ``42`` not-open."""
    path = work_dir / "seq_seq.dat"
    f = make_file(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert st(f) == "00"
    # A second OPEN on an already-open connector reports 41.
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert st(f) == "41"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    # A CLOSE on the now-closed connector reports 42.
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    assert st(f) == "42"


def test_sequential_mode_violations(work_dir):
    """READ on an OUTPUT file is ``47``; WRITE on an INPUT file is ``48``."""
    path = work_dir / "seq_mode.dat"
    f = make_file(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    fileio.cob_read(f, None, None, common.COB_READ_NEXT)
    assert st(f) == "47"
    set_rec(f, b"DATA")
    fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = make_file(path, 4)
    fileio.cob_open(g, common.COB_OPEN_INPUT, 0, None)
    set_rec(g, b"NOPE")
    fileio.cob_write(g, g.record, 0, None)
    assert st(g) == "48"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


def test_optional_missing_input_is_05_then_eof(work_dir):
    """An OPTIONAL missing INPUT file opens ``05`` and first READ is EOF ``10``."""
    f = make_file(work_dir / "opt.dat", 4, optional=1)
    fileio.cob_open(f, common.COB_OPEN_INPUT, 0, None)
    assert st(f) == "05"
    fileio.cob_read(f, None, None, common.COB_READ_NEXT)
    assert st(f) == "10"


def test_line_sequential_round_trip(work_dir):
    """LINE SEQUENTIAL records round-trip through newline-terminated writes."""
    path = work_dir / "lines.txt"
    payloads = [b"one", b"two", b"three"]

    f = make_file(path, 6, org=common.COB_ORG_LINE_SEQUENTIAL)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    for payload in payloads:
        set_rec(f, payload)
        fileio.cob_write(f, f.record, _LS_WRITE_OPT, None)
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
    assert seen == payloads


# ===========================================================================
# Phase 2 - RELATIVE random access
# ===========================================================================
def test_relative_random(work_dir):
    """RELATIVE file: WRITE at sparse relative keys 1, 3, 5; random READ of key
    3 returns its record (status ``00``); READ of an empty slot (key 2) is
    ``23``; DELETE key 3 then READ key 3 is ``23``.
    """
    path = work_dir / "rel.dat"

    # Random-access WRITE at relative record numbers 1, 3 and 5 (sparse).
    writer = make_relative(path, 4, access=common.COB_ACCESS_RANDOM)
    fileio.cob_open(writer, common.COB_OPEN_OUTPUT, 0, None)
    assert st(writer) == "00"
    for keynum, payload in ((1, b"R1"), (3, b"R3"), (5, b"R5")):
        set_key_int(writer.keys[0].field, keynum)
        set_rec(writer, payload)
        fileio.cob_write(writer, writer.record, 0, None)
        assert st(writer) == "00"
    fileio.cob_close(writer, common.COB_CLOSE_NORMAL, None)

    reader = make_relative(path, 4, access=common.COB_ACCESS_RANDOM)
    _open_io(reader)
    assert st(reader) == "00"

    # Random READ of an occupied slot (key 3) returns the record.
    set_key_int(reader.keys[0].field, 3)
    fileio.cob_read(reader, reader.keys[0].field, None, 0)
    assert st(reader) == "00"
    assert bytes(reader.record.data).rstrip() == b"R3"

    # Random READ of an empty slot (key 2) reports record-not-found (23).
    set_key_int(reader.keys[0].field, 2)
    fileio.cob_read(reader, reader.keys[0].field, None, 0)
    assert st(reader) == "23"

    # DELETE key 3, then READ key 3 -> 23 (gone).
    set_key_int(reader.keys[0].field, 3)
    fileio.cob_read(reader, reader.keys[0].field, None, 0)
    assert st(reader) == "00"
    fileio.cob_delete(reader, None)
    assert st(reader) == "00"
    set_key_int(reader.keys[0].field, 3)
    fileio.cob_read(reader, reader.keys[0].field, None, 0)
    assert st(reader) == "23"
    fileio.cob_close(reader, common.COB_CLOSE_NORMAL, None)


def test_relative_sequential_navigation(work_dir):
    """RELATIVE sequential READ NEXT walks every record then EOF; START + READ
    NEXT positions; READ PREVIOUS walks back; random REWRITE and DELETE work.
    """
    path = work_dir / "rel_nav.dat"
    payloads = [b"AA", b"BB", b"CC", b"DD"]

    f = make_relative(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    for payload in payloads:
        set_rec(f, payload)
        fileio.cob_write(f, f.record, 0, None)
        assert st(f) == "00"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    # Sequential READ NEXT through all records, then EOF (10).
    g = make_relative(path, 4)
    fileio.cob_open(g, common.COB_OPEN_INPUT, 0, None)
    seen = []
    while True:
        fileio.cob_read(g, None, None, common.COB_READ_NEXT)
        if st(g) != "00":
            break
        seen.append(bytes(g.record.data).rstrip())
    assert st(g) == "10"
    assert seen == payloads
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)

    # START >= relative record 3, then READ NEXT begins at record 3.
    h = make_relative(path, 4)
    fileio.cob_open(h, common.COB_OPEN_INPUT, 0, None)
    set_key_int(h.keys[0].field, 3)
    fileio.cob_start(h, common.COB_GE, h.keys[0].field, None)
    assert st(h) == "00"
    fileio.cob_read(h, None, None, common.COB_READ_NEXT)
    assert st(h) == "00"
    assert bytes(h.record.data).rstrip() == b"CC"
    # Direction change: the RELATIVE cursor is a byte offset and READ PREVIOUS
    # steps back one whole slot *before* reading, so the first READ PREVIOUS
    # after a READ NEXT re-reads the current record (CC), and the following one
    # yields the prior record (BB).  This mirrors the C runtime's offset cursor.
    fileio.cob_read(h, None, None, common.COB_READ_PREVIOUS)
    assert st(h) == "00"
    assert bytes(h.record.data).rstrip() == b"CC"
    fileio.cob_read(h, None, None, common.COB_READ_PREVIOUS)
    assert st(h) == "00"
    assert bytes(h.record.data).rstrip() == b"BB"
    fileio.cob_close(h, common.COB_CLOSE_NORMAL, None)

    # Random REWRITE record 2 then verify; DELETE record 4.
    w = make_relative(path, 4, access=common.COB_ACCESS_RANDOM)
    _open_io(w)
    set_key_int(w.keys[0].field, 2)
    fileio.cob_read(w, w.keys[0].field, None, 0)
    assert bytes(w.record.data).rstrip() == b"BB"
    set_rec(w, b"XX")
    set_key_int(w.keys[0].field, 2)
    fileio.cob_rewrite(w, w.record, 0, None)
    assert st(w) == "00"
    set_key_int(w.keys[0].field, 2)
    fileio.cob_read(w, w.keys[0].field, None, 0)
    assert bytes(w.record.data).rstrip() == b"XX"
    set_key_int(w.keys[0].field, 4)
    fileio.cob_read(w, w.keys[0].field, None, 0)
    fileio.cob_delete(w, None)
    assert st(w) == "00"
    set_key_int(w.keys[0].field, 4)
    fileio.cob_read(w, w.keys[0].field, None, 0)
    assert st(w) == "23"
    fileio.cob_close(w, common.COB_CLOSE_NORMAL, None)


def test_relative_duplicate_slot_is_22(work_dir):
    """Re-writing an occupied RELATIVE slot reports KEY EXISTS (status ``22``)."""
    path = work_dir / "rel_dup.dat"
    f = make_relative(path, 4, access=common.COB_ACCESS_RANDOM)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_key_int(f.keys[0].field, 2)
    set_rec(f, b"R2")
    fileio.cob_write(f, f.record, 0, None)
    assert st(f) == "00"
    # Writing the same slot again -> 22.
    set_key_int(f.keys[0].field, 2)
    set_rec(f, b"XX")
    fileio.cob_write(f, f.record, 0, None)
    assert st(f) == "22"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)


# ===========================================================================
# Phase 3 - INDEXED via dbm (primary key only)
# ===========================================================================
def test_indexed_primary_dbm(work_dir):
    """INDEXED file with ONLY a primary key routes to the ``dbm`` backend.

    Exercises keyed WRITE/READ, START + READ NEXT ordered traversal and the
    duplicate-primary-key WRITE status (``22``).  The backend-introspection
    assertion (``_IndexedDbm``) is guarded so behavioural coverage is retained
    even if the runtime renames the internal class.
    """
    path = work_dir / "idx_dbm"
    rows = [(b"AAA", b"AAArow1"), (b"BBB", b"BBBrow2"), (b"CCC", b"CCCrow3")]

    f = make_indexed(path, 8, keylen=3)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert st(f) == "00"
    assert f.nkeys == 1  # primary key only -> dbm route
    dbm_cls = getattr(fileio, "_IndexedDbm", None)
    if dbm_cls is not None:
        assert isinstance(f.file, dbm_cls), \
            "primary-key-only INDEXED file must use the dbm backend"

    for _key, record in rows:
        set_rec(f, record)
        fileio.cob_write(f, f.record, 0, None)
        assert st(f) == "00"
    # Duplicate primary key -> status 22.
    set_rec(f, b"AAAdup00")
    fileio.cob_write(f, f.record, 0, None)
    assert st(f) == "22"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = make_indexed(path, 8, keylen=3)
    _open_io(g)
    assert st(g) == "00"

    # Keyed READ of the middle record.
    set_search_key(g.keys[0].field, b"BBB")
    fileio.cob_read(g, g.keys[0].field, None, 0)
    assert st(g) == "00"
    assert bytes(g.record.data).startswith(b"BBBrow2")

    # READ of an absent key -> 23.
    set_search_key(g.keys[0].field, b"ZZZ")
    fileio.cob_read(g, g.keys[0].field, None, 0)
    assert st(g) == "23"

    # START >= AAA then READ NEXT walks ascending across all keys.
    set_search_key(g.keys[0].field, b"AAA")
    fileio.cob_start(g, common.COB_GE, g.keys[0].field, None)
    assert st(g) == "00"
    ordered = []
    while True:
        fileio.cob_read(g, None, None, common.COB_READ_NEXT)
        if st(g) != "00":
            break
        ordered.append(bytes(g.record.data[:3]))
    assert ordered == [b"AAA", b"BBB", b"CCC"]

    # DELETE BBB then confirm it is gone (23).
    set_search_key(g.keys[0].field, b"BBB")
    fileio.cob_read(g, g.keys[0].field, None, 0)
    assert st(g) == "00"
    fileio.cob_delete(g, None)
    assert st(g) == "00"
    set_search_key(g.keys[0].field, b"BBB")
    fileio.cob_read(g, g.keys[0].field, None, 0)
    assert st(g) == "23"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


def test_indexed_dbm_start_conditions(work_dir):
    """dbm START honours GT / LE conditions and feeds READ NEXT / PREVIOUS."""
    path = work_dir / "idx_dbm2"
    f = make_indexed(path, 6, keylen=3)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    for key in (b"AAA", b"BBB", b"CCC", b"DDD"):
        set_rec(f, key + b"xy")
        fileio.cob_write(f, f.record, 0, None)
        assert st(f) == "00"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = make_indexed(path, 6, keylen=3)
    _open_io(g)
    # START > BBB -> first READ NEXT is CCC.
    set_search_key(g.keys[0].field, b"BBB")
    fileio.cob_start(g, common.COB_GT, g.keys[0].field, None)
    assert st(g) == "00"
    fileio.cob_read(g, None, None, common.COB_READ_NEXT)
    assert bytes(g.record.data[:3]) == b"CCC"
    # START <= CCC -> READ PREVIOUS yields CCC.
    set_search_key(g.keys[0].field, b"CCC")
    fileio.cob_start(g, common.COB_LE, g.keys[0].field, None)
    assert st(g) == "00"
    fileio.cob_read(g, None, None, common.COB_READ_PREVIOUS)
    assert bytes(g.record.data[:3]) == b"CCC"
    # REWRITE DDD through the I-O connector.
    set_search_key(g.keys[0].field, b"DDD")
    fileio.cob_read(g, g.keys[0].field, None, 0)
    set_rec(g, b"DDDzz")
    fileio.cob_rewrite(g, g.record, 0, None)
    assert st(g) == "00"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


# ===========================================================================
# Phase 4 - INDEXED via sqlite3 (alternate keys / duplicates)
# ===========================================================================
def test_indexed_alternate_sqlite(work_dir):
    """INDEXED file with an ALTERNATE RECORD KEY (WITH DUPLICATES) routes to the
    ``sqlite3`` backend (table + one index per key).

    The COBOL alternate-key duplicate contract is verified exactly as the
    runtime implements it:

    * a record whose DUPLICATES-allowed alternate key repeats an existing value
      WRITEs successfully (the duplicate is *permitted*);
    * traversing the duplicates by START + READ NEXT on the alternate key
      surfaces file status ``02`` while another record with the same alternate
      value is still ahead, then ``00`` on the final one;
    * a duplicate on a *unique* (no-DUPLICATES) alternate key is rejected with
      ``22``; and
    * a duplicate primary key is rejected with ``22``.
    """
    path = work_dir / "idx_sql"
    # record layout: primary key 3 bytes @0, alternate key 2 bytes @3.
    f = make_indexed(path, 12, keylen=3, nkeys=2, altlen=2, altoff=3,
                     dup_alt=True)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert st(f) == "00"
    assert f.nkeys == 2  # alternate key present -> sqlite3 route
    sqlite_cls = getattr(fileio, "_IndexedSqlite", None)
    if sqlite_cls is not None:
        assert isinstance(f.file, sqlite_cls), \
            "alternate-key INDEXED file must use the sqlite3 backend"

    # Two records share the duplicate alternate value "XX"; one carries "YY".
    for record in (b"K01XXrecone", b"K02XXrectwo", b"K03YYrecthr"):
        set_rec(f, record)
        fileio.cob_write(f, f.record, 0, None)
        # A duplicate on the DUPLICATES-allowed alternate key WRITEs cleanly.
        assert st(f) in ("00", "02")
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = make_indexed(path, 12, keylen=3, nkeys=2, altlen=2, altoff=3,
                     dup_alt=True)
    _open_io(g)
    assert st(g) == "00"

    # Primary keyed READ.
    set_search_key(g.keys[0].field, b"K02")
    fileio.cob_read(g, g.keys[0].field, None, 0)
    assert st(g) == "00"
    assert bytes(g.record.data).startswith(b"K02XX")

    # Alternate keyed READ of the duplicate "XX" returns a matching record.
    set_search_key(g.keys[1].field, b"XX")
    fileio.cob_read(g, g.keys[1].field, None, 0)
    assert st(g) == "00"
    assert bytes(g.record.data[3:5]) == b"XX"

    # START on the alternate key, then READ NEXT walks the duplicates: the first
    # of the pair reports 02 (a duplicate is still ahead), the second reports 00.
    set_search_key(g.keys[1].field, b"XX")
    fileio.cob_start(g, common.COB_GE, g.keys[1].field, None)
    assert st(g) == "00"
    statuses = []
    primaries = []
    for _ in range(2):
        fileio.cob_read(g, None, None, common.COB_READ_NEXT)
        statuses.append(st(g))
        primaries.append(bytes(g.record.data[:3]))
    assert primaries == [b"K01", b"K02"]
    assert statuses[0] == "02"   # duplicate alternate key ahead
    assert statuses[1] == "00"   # last record of the duplicate group
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


def test_indexed_sqlite_unique_alt_duplicate_is_22(work_dir):
    """A duplicate value on a *unique* alternate key is rejected with ``22``."""
    path = work_dir / "idx_sql_uniq"
    # Alternate key WITHOUT duplicates -> must stay unique.
    f = make_indexed(path, 12, keylen=3, nkeys=2, altlen=2, altoff=3,
                     dup_alt=False)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"K01XXrecone")
    fileio.cob_write(f, f.record, 0, None)
    assert st(f) == "00"
    # Distinct primary key but a repeated unique alternate value -> 22.
    set_rec(f, b"K02XXrectwo")
    fileio.cob_write(f, f.record, 0, None)
    assert st(f) == "22"
    # Duplicate primary key is likewise rejected with 22.
    set_rec(f, b"K01ZZrecdup")
    fileio.cob_write(f, f.record, 0, None)
    assert st(f) == "22"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)


def test_indexed_sqlite_navigation_and_mutation(work_dir):
    """sqlite primary START + READ NEXT ascending, REWRITE and DELETE."""
    path = work_dir / "idx_sql_nav"
    f = make_indexed(path, 12, keylen=3, nkeys=2, altlen=2, altoff=3,
                     dup_alt=True)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    for record in (b"K01AArecone", b"K02BBrectwo", b"K03AArecthr"):
        set_rec(f, record)
        fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = make_indexed(path, 12, keylen=3, nkeys=2, altlen=2, altoff=3,
                     dup_alt=True)
    _open_io(g)
    # START on primary key GE K02 then READ NEXT ascending.
    set_search_key(g.keys[0].field, b"K02")
    fileio.cob_start(g, common.COB_GE, g.keys[0].field, None)
    assert st(g) == "00"
    ordered = []
    while True:
        fileio.cob_read(g, None, None, common.COB_READ_NEXT)
        if st(g) != "00":
            break
        ordered.append(bytes(g.record.data[:3]))
    assert ordered == [b"K02", b"K03"]

    # REWRITE K02's trailing data.
    set_search_key(g.keys[0].field, b"K02")
    fileio.cob_read(g, g.keys[0].field, None, 0)
    set_rec(g, b"K02BBnewdat")
    fileio.cob_rewrite(g, g.record, 0, None)
    assert st(g) == "00"
    set_search_key(g.keys[0].field, b"K02")
    fileio.cob_read(g, g.keys[0].field, None, 0)
    assert bytes(g.record.data).startswith(b"K02BBnewdat")

    # DELETE K01.
    set_search_key(g.keys[0].field, b"K01")
    fileio.cob_read(g, g.keys[0].field, None, 0)
    fileio.cob_delete(g, None)
    assert st(g) == "00"
    set_search_key(g.keys[0].field, b"K01")
    fileio.cob_read(g, g.keys[0].field, None, 0)
    assert st(g) == "23"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


# ===========================================================================
# Phase 5 - Indexed-file migration error contract (HARD - AAP section 0.6.3)
# ===========================================================================
def test_migration_message_is_exact_em_dash():
    """The migration message is the byte-for-byte contract text (U+2014 dash)."""
    expected = "indexed file format incompatible \u2014 manual migration required"
    assert fileio._MIGRATION_MESSAGE == expected
    # The dash MUST be the U+2014 EM DASH, never an ASCII hyphen-minus.
    assert "\u2014" in fileio._MIGRATION_MESSAGE
    assert " - " not in fileio._MIGRATION_MESSAGE


def test_unreadable_indexed_status_30(work_dir, capsys):
    """Opening a legacy Berkeley DB indexed store yields status ``30`` AND logs
    the exact migration message - with no exception, crash or truncation.
    """
    path = work_dir / "legacy.idx"
    # Plant a recognised Berkeley DB magic number so the legacy detector trips;
    # pad past the 16-byte minimum the detector inspects.
    with open(path, "wb") as handle:
        handle.write(struct.pack("<I", 0x00053162))  # a known BDB magic
        handle.write(b"\x00" * 64)
    original = path.read_bytes()

    f = make_indexed(path, 10, keylen=3)
    # Must not raise; the contract maps the failure to a COBOL status, not an
    # exception.
    _open_io(f)
    assert st(f) == "30"

    captured = capsys.readouterr()
    assert "indexed file format incompatible \u2014 manual migration required" \
        in captured.err, "exact em-dash migration message must be logged"

    # HARD: no silent data loss - the legacy file is left byte-for-byte intact.
    assert path.read_bytes() == original


def test_unreadable_garbage_indexed_status_30(work_dir, capsys):
    """A non-empty, non-dbm, non-BDB garbage file also maps to ``30`` + message."""
    path = work_dir / "garbage.idx"
    with open(path, "wb") as handle:
        handle.write(b"this is not any known database container at all!" * 4)

    f = make_indexed(path, 10, keylen=3)
    _open_io(f)
    assert st(f) == "30"
    assert "manual migration required" in capsys.readouterr().err


# ===========================================================================
# Phase 6 - SORT / MERGE
# ===========================================================================
def _sort_work(recmax, flag=common.COB_ASCENDING):
    """Initialise a SORT work area with a single key of width *recmax*."""
    sf = fileio.cob_file()
    sf.record_max = recmax
    sf.record_min = recmax
    sf.record = common.cob_field(recmax, bytearray(recmax), _ALNUM)
    keyfield = common.cob_field(recmax, bytearray(recmax), _ALNUM)
    fileio.cob_file_sort_init(sf, 1, None, None, None)
    fileio.cob_file_sort_init_key(sf, flag, keyfield, 0)
    return sf


def _sorted_input_file(path, recsize, records):
    """Write *records* (already in the desired order) to a SEQUENTIAL file."""
    f = make_file(path, recsize)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    for rec in records:
        set_rec(f, rec)
        fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    return f


def _drain_sequential(path, recsize):
    """OPEN INPUT *path* and return its records (trailing spaces stripped)."""
    f = make_file(path, recsize)
    fileio.cob_open(f, common.COB_OPEN_INPUT, 0, None)
    out = []
    while True:
        fileio.cob_read(f, None, None, common.COB_READ_NEXT)
        if st(f) != "00":
            break
        out.append(bytes(f.record.data).rstrip())
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    return out


def test_sort(work_dir):
    """SORT: RELEASE unordered records, then RETURN them in ascending key order;
    a second pass exercises a DESCENDING key and the USING/GIVING file path.
    """
    # Ascending RELEASE/RETURN round-trip.
    sf = _sort_work(4, flag=common.COB_ASCENDING)
    unordered = [b"dddd", b"aaaa", b"cccc", b"bbbb", b"aaaa"]
    for rec in unordered:
        sf.record.data[0:4] = rec
        fileio.cob_file_release(sf)
        assert st(sf) == "00"
    returned = []
    while True:
        fileio.cob_file_return(sf)
        if st(sf) != "00":
            break
        returned.append(bytes(sf.record.data))
    assert st(sf) == "10"  # RETURN past the last record reports EOF
    fileio.cob_file_sort_close(sf)
    assert returned == sorted(unordered)
    assert returned.count(b"aaaa") == 2          # stable: duplicates preserved

    # Descending key variant.
    sd = _sort_work(1, flag=common.COB_DESCENDING)
    for rec in (b"m", b"a", b"z", b"q"):
        sd.record.data[0:1] = rec
        fileio.cob_file_release(sd)
    desc = []
    while True:
        fileio.cob_file_return(sd)
        if st(sd) != "00":
            break
        desc.append(bytes(sd.record.data))
    fileio.cob_file_sort_close(sd)
    assert desc == sorted([b"m", b"a", b"z", b"q"], reverse=True)

    # SORT ... USING <input> GIVING <output> ordering.
    src = work_dir / "sort_in.dat"
    dst = work_dir / "sort_out.dat"
    _sorted_input_file(src, 4, [b"CCCC", b"AAAA", b"BBBB"])
    sg = _sort_work(4)
    fileio.cob_file_sort_using(sg, make_file(src, 4))
    fileio.cob_file_sort_giving(sg, 1, make_file(dst, 4))
    fileio.cob_file_sort_close(sg)
    assert _drain_sequential(dst, 4) == [b"AAAA", b"BBBB", b"CCCC"]


def test_sort_external_merge_spills_runs(work_dir, monkeypatch):
    """A tiny COB_SORT_MEMORY budget forces the bounded external merge sort to
    spill sorted runs to temp files; the merged output is still fully ordered.
    """
    monkeypatch.setattr(fileio, "cob_sort_memory", 1)
    data = [b"dddd", b"aaaa", b"cccc", b"bbbb", b"eeee", b"aaaa"]
    sf = _sort_work(4)
    for rec in data:
        sf.record.data[0:4] = rec
        assert fileio.cob_file_sort_submit(sf, sf.record.data) == 0
    # Multiple sorted runs spilled to disk, not yet merged.
    assert len(sf.file.runs) >= 2
    run_paths = list(sf.file.runs)
    assert all(os.path.exists(p) for p in run_paths)

    out = []
    while True:
        buf = bytearray(4)
        if fileio.cob_file_sort_retrieve(sf, buf):
            break
        out.append(bytes(buf))
    assert out == sorted(data)
    fileio.cob_file_sort_close(sf)
    # close tears down the work area and removes every run temp file.
    assert sf.file is None
    assert all(not os.path.exists(p) for p in run_paths)


def test_merge(work_dir):
    """MERGE two PRE-SORTED inputs into one ordered output.

    The C runtime (and this port) realise COBOL MERGE through the same sort
    machinery: each input file is fed with ``cob_file_sort_using`` and the
    combined, ordered stream is written with ``cob_file_sort_giving``.  No
    separate ``cob_file_merge`` entry point exists, so MERGE is exercised via
    that documented path (the test is skipped only if SORT itself is absent).
    """
    if not all(hasattr(fileio, name) for name in
               ("cob_file_sort_using", "cob_file_sort_giving",
                "cob_file_sort_init")):
        pytest.skip("SORT/MERGE machinery not exposed by this runtime build")

    left = work_dir / "merge_a.dat"
    right = work_dir / "merge_b.dat"
    out = work_dir / "merge_out.dat"
    # Two independently pre-sorted inputs.
    _sorted_input_file(left, 4, [b"AAAA", b"CCCC", b"EEEE"])
    _sorted_input_file(right, 4, [b"BBBB", b"DDDD", b"FFFF"])

    sf = _sort_work(4)
    fileio.cob_file_sort_using(sf, make_file(left, 4))
    fileio.cob_file_sort_using(sf, make_file(right, 4))
    fileio.cob_file_sort_giving(sf, 1, make_file(out, 4))
    fileio.cob_file_sort_close(sf)

    merged = _drain_sequential(out, 4)
    assert merged == [b"AAAA", b"BBBB", b"CCCC", b"DDDD", b"EEEE", b"FFFF"]


def test_sort_giving_multiple_files(work_dir):
    """SORT ... GIVING two files writes the ordered stream to each output."""
    src = work_dir / "msrc.dat"
    _sorted_input_file(src, 4, [b"DDDD", b"BBBB", b"CCCC", b"AAAA"])
    sf = _sort_work(4)
    out1 = work_dir / "g1.dat"
    out2 = work_dir / "g2.dat"
    fileio.cob_file_sort_using(sf, make_file(src, 4))
    fileio.cob_file_sort_giving(sf, 2, make_file(out1, 4), make_file(out2, 4))
    fileio.cob_file_sort_close(sf)
    expected = [b"AAAA", b"BBBB", b"CCCC", b"DDDD"]
    assert _drain_sequential(out1, 4) == expected
    assert _drain_sequential(out2, 4) == expected


# ===========================================================================
# Phase 7 - C$ filesystem helpers and runtime lifecycle
# ===========================================================================
def test_acuw_mkdir_delete(work_dir):
    """``cob_acuw_mkdir`` creates a directory, ``cob_acuw_file_delete`` removes a
    file, ``cob_acuw_copyfile`` copies, ``cob_acuw_chdir`` changes directory and
    ``cob_acuw_file_info`` returns size info - every artifact confined to the
    temp work_dir.  Unsafe (NUL-bearing) paths are rejected with code ``128``.
    """
    bad = common.cob_field(8, bytearray(b"a\x00b" + b" " * 5), _ALNUM)

    # mkdir
    target = work_dir / "newdir"
    assert fileio.cob_acuw_mkdir(char_field(str(target))) == 0
    assert target.is_dir()
    assert fileio.cob_acuw_mkdir(bad) == 128

    # file_delete
    victim = work_dir / "victim.dat"
    victim.write_bytes(b"bye")
    assert fileio.cob_acuw_file_delete(char_field(str(victim)), None) == 0
    assert not victim.exists()
    assert fileio.cob_acuw_file_delete(bad, None) == 128

    # copyfile
    src = work_dir / "src.dat"
    dst = work_dir / "dst.dat"
    src.write_bytes(b"payload-bytes")
    assert fileio.cob_acuw_copyfile(char_field(str(src)),
                                    char_field(str(dst)), None) == 0
    assert dst.read_bytes() == b"payload-bytes"
    assert fileio.cob_acuw_copyfile(bad, char_field(str(dst)), None) == 128

    # file_info (8-byte big-endian size in the first columns of the info field)
    info = common.cob_field(16, bytearray(16), _ALNUM)
    assert fileio.cob_acuw_file_info(char_field(str(src)), info) == 0
    assert struct.unpack(">Q", bytes(info.data[0:8]))[0] == len(b"payload-bytes")
    # Missing file -> 35; unsafe path -> 128.
    assert fileio.cob_acuw_file_info(char_field(str(work_dir / "nope")), info) == 35
    assert fileio.cob_acuw_file_info(bad, info) == 128

    # chdir (restore the cwd afterwards so the rest of the suite is unaffected)
    start = os.getcwd()
    try:
        status = numkey(0)
        sub = work_dir / "newdir"
        assert fileio.cob_acuw_chdir(char_field(str(sub)), status) == 0
        assert get_key_int(status) == 0
        assert os.path.realpath(os.getcwd()) == os.path.realpath(str(sub))
        assert fileio.cob_acuw_chdir(bad, status) == 128
        assert get_key_int(status) == 128
    finally:
        os.chdir(start)


def test_init_exit_fileio(work_dir):
    """``cob_init_fileio`` and ``cob_exit_fileio`` are callable without error,
    and ``cob_exit_fileio`` implicitly closes a still-open connector.
    """
    # init is a pure (re)configuration step - must not raise.
    fileio.cob_init_fileio()

    f = make_file(work_dir / "lifecycle.dat", 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert f.open_mode != common.COB_OPEN_CLOSED

    # exit implicitly closes any file left open - must not raise.
    fileio.cob_exit_fileio()
    assert f.open_mode == common.COB_OPEN_CLOSED


def test_init_fileio_reads_environment(monkeypatch, work_dir):
    """``cob_init_fileio`` reads COB_SYNC / COB_SORT_MEMORY / COB_FILE_PATH."""
    monkeypatch.setenv("COB_SYNC", "Y")
    monkeypatch.setenv("COB_SORT_MEMORY", str(8 * 1024 * 1024))
    monkeypatch.setenv("COB_FILE_PATH", str(work_dir))
    # Restore the module defaults after the assertions regardless of outcome.
    saved = (fileio.cob_do_sync, fileio.cob_sort_memory, fileio.cob_file_path)
    try:
        fileio.cob_init_fileio()
        assert fileio.cob_do_sync == 1
        assert fileio.cob_sort_memory == 8 * 1024 * 1024
        assert fileio.cob_file_path == str(work_dir)
    finally:
        (fileio.cob_do_sync, fileio.cob_sort_memory,
         fileio.cob_file_path) = saved


# ===========================================================================
# Supporting coverage - CWE-22 path safety (_safe_path / _safe_field_path)
# ===========================================================================
def test_safe_path_rejects_nul_and_control():
    assert fileio._safe_path("foo\x00bar") is None
    assert fileio._safe_path("foo\x01bar") is None
    assert fileio._safe_path("ok\x7f") is None
    assert fileio._safe_path("") is None
    assert fileio._safe_path(None) is None


def test_safe_path_gates_relative_parent_escape():
    assert fileio._safe_path("..") is None
    assert fileio._safe_path("../escape") is None
    assert fileio._safe_path(os.path.join("..", "..", "etc", "passwd")) is None


def test_safe_path_allows_benign_relative_and_absolute():
    assert fileio._safe_path("sub/dir/file.dat") == os.path.normpath(
        "sub/dir/file.dat")
    assert fileio._safe_path("/tmp/abs.dat") == "/tmp/abs.dat"


def test_safe_path_containment_and_trusted(tmp_path):
    base = str(tmp_path)
    good = fileio._safe_path("inside.dat", base=base)
    assert good == os.path.join(os.path.realpath(base), "inside.dat")
    # Escapes and absolute-outside-base are rejected under containment.
    assert fileio._safe_path("../outside.dat", base=base) is None
    assert fileio._safe_path("/etc/passwd", base=base) is None
    # Operator-trusted values skip containment but still reject NUL bytes.
    assert fileio._safe_path("/etc/hosts", base=base, trusted=True) == "/etc/hosts"
    assert fileio._safe_path("x\x00", base=base, trusted=True) is None


def test_safe_path_uses_cob_file_path_default(tmp_path, monkeypatch):
    monkeypatch.setattr(fileio, "cob_file_path", str(tmp_path))
    assert fileio._safe_path("../escape") is None
    inside = fileio._safe_path("ok.dat")
    assert inside == os.path.join(os.path.realpath(str(tmp_path)), "ok.dat")


def test_safe_field_path():
    good = common.cob_field(64, bytearray(b"good.dat".ljust(64)), _ALNUM)
    assert fileio._safe_field_path(good) == os.path.normpath("good.dat")
    bad = common.cob_field(8, bytearray(b"a\x00b" + b" " * 5), _ALNUM)
    assert fileio._safe_field_path(bad) is None
    assert fileio._safe_field_path(None) is None


def test_open_rejects_unsafe_assign(tmp_path, monkeypatch):
    """An OPEN whose resolved name escapes COB_FILE_PATH maps to status ``30``."""
    monkeypatch.setattr(fileio, "cob_file_path", str(tmp_path))
    f = make_file("../escape.dat", 8)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert st(f) == "30"


# ===========================================================================
# Supporting coverage - REWRITE / DELETE / overflow status codes
# ===========================================================================
def test_write_record_overflow_is_44(work_dir):
    """A reported record size beyond record_max yields overflow status ``44``."""
    f = make_file(work_dir / "ov.dat", 8, record_min=4)
    f.record_size = numkey(99)  # claim a 99-byte record (> record_max of 8)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"AAAA")
    fileio.cob_write(f, f.record, 0, None)
    assert st(f) == "44"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)


def test_sequential_rewrite_io(work_dir):
    """SEQUENTIAL REWRITE replaces the last-read record under OPEN I-O."""
    path = work_dir / "seqrw.dat"
    f = make_file(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    for payload in (b"R1", b"R2"):
        set_rec(f, payload)
        fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = make_file(path, 4)
    _open_io(g)
    fileio.cob_read(g, None, None, common.COB_READ_NEXT)  # reads R1
    set_rec(g, b"RX")
    fileio.cob_rewrite(g, g.record, 0, None)
    assert st(g) == "00"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)

    assert _drain_sequential(path, 4) == [b"RX", b"R2"]


def test_rewrite_not_io_is_49(work_dir):
    """REWRITE on a file not opened I-O is ``49``."""
    f = make_file(work_dir / "rw49.dat", 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"AAAA")
    fileio.cob_rewrite(f, f.record, 0, None)
    assert st(f) == "49"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)


def test_rewrite_without_prior_read_is_43(work_dir):
    """Sequential REWRITE without a prior READ is ``43``."""
    path = work_dir / "rw43.dat"
    f = make_file(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"AAAA")
    fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    g = make_file(path, 4)
    _open_io(g)
    set_rec(g, b"BBBB")
    fileio.cob_rewrite(g, g.record, 0, None)  # no READ first
    assert st(g) == "43"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


def test_delete_without_prior_read_is_43(work_dir):
    """Sequential DELETE without a prior READ is ``43``."""
    path = work_dir / "del43.dat"
    f = make_file(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"AAAA")
    fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    g = make_file(path, 4)
    _open_io(g)
    fileio.cob_delete(g, None)  # no READ first
    assert st(g) == "43"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


# ===========================================================================
# Supporting coverage - EXTEND mode, sync, unlock
# ===========================================================================
def test_extend_mode_appends(work_dir):
    """OPEN EXTEND appends to the existing records."""
    path = work_dir / "ext.dat"
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

    assert _drain_sequential(path, 4) == [b"AAAA", b"BBBB"]


def test_sync_unlock_commit_rollback(work_dir):
    """cob_sync/cob_unlock/cob_commit/cob_rollback run without error."""
    path = work_dir / "sync.dat"
    f = make_file(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"AAAA")
    fileio.cob_write(f, f.record, 0, None)
    assert st(f) == "00"
    fileio.cob_sync(f, 1)        # flush
    fileio.cob_sync(f, 2)        # flush + fsync
    fileio.cob_unlock(f)         # advisory unlock
    fileio.cob_commit()          # flush cached files
    fileio.cob_rollback()        # release locks
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    # cob_sync on a now-closed (file is None) connector is a safe no-op.
    fileio.cob_sync(f, 1)


def test_close_with_lock_then_reopen_is_38(work_dir):
    """CLOSE WITH LOCK latches the connector; reopening it reports ``38``."""
    lock_opt = getattr(common, "COB_CLOSE_LOCK", None)
    if lock_opt is None:
        pytest.skip("COB_CLOSE_LOCK not exposed by this runtime build")
    path = work_dir / "locked.dat"
    f = make_file(path, 4)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    set_rec(f, b"AAAA")
    fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, lock_opt, None)
    assert f.open_mode == common.COB_OPEN_LOCKED
    fileio.cob_open(f, common.COB_OPEN_INPUT, 0, None)
    assert st(f) == "38"


# ===========================================================================
# Supporting coverage - LINAGE geometry and the open-error handle-leak fix
# ===========================================================================
def test_linage_invalid_geometry_no_handle_leak(work_dir):
    """An invalid LINAGE geometry reports ``57`` and detaches the file handle."""
    if not hasattr(common, "COB_SELECT_LINAGE"):
        pytest.skip("LINAGE feature flag not exposed by this runtime build")
    path = work_dir / "linage_bad.txt"
    path.write_bytes(b"")  # pre-create so OPEN passes the existence check
    f = make_file(path, 8, org=common.COB_ORG_LINE_SEQUENTIAL)
    f.flag_select_features = common.COB_SELECT_LINAGE
    ling = fileio.cob_linage_struct()
    ling.linage = numkey(0)       # LINAGE of 0 is invalid (< 1) -> status 57
    ling.linage_ctr = numkey(0)
    f.linorkeyptr = ling
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert st(f) == "57"
    # The leak-fix contract: no dangling handle is left behind.
    assert f.file is None
    assert f.open_mode == common.COB_OPEN_CLOSED


def test_linage_write_advancing(work_dir):
    """A valid LINAGE geometry primes the counter and advances on WRITE."""
    if not hasattr(common, "COB_SELECT_LINAGE"):
        pytest.skip("LINAGE feature flag not exposed by this runtime build")
    path = work_dir / "linage_ok.txt"
    f = make_file(path, 10, org=common.COB_ORG_LINE_SEQUENTIAL)
    f.flag_select_features = common.COB_SELECT_LINAGE
    ling = fileio.cob_linage_struct()
    ling.linage = numkey(3)
    ling.linage_ctr = numkey(0)
    ling.latfoot = numkey(2)
    ling.lattop = numkey(1)
    ling.latbot = numkey(1)
    f.linorkeyptr = ling
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    assert st(f) == "00"
    assert get_key_int(ling.linage_ctr) == 1  # primed on open
    opt = common.COB_WRITE_AFTER | common.COB_WRITE_LINES | 1
    for payload in (b"L1", b"L2", b"L3", b"L4", b"L5"):
        set_rec(f, payload)
        fileio.cob_write(f, f.record, opt, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    data = path.read_bytes()
    assert b"L1" in data and b"L5" in data
    assert data.count(b"\n") >= 5


def test_file_linage_check_geometries():
    """``file_linage_check`` validates the LINAGE/FOOTING/TOP/BOTTOM geometry."""
    def conn(lines, foot=None, top=None, bot=None):
        f = fileio.cob_file()
        ling = fileio.cob_linage_struct()
        ling.linage = numkey(lines)
        ling.linage_ctr = numkey(0)
        ling.latfoot = numkey(foot) if foot is not None else None
        ling.lattop = numkey(top) if top is not None else None
        ling.latbot = numkey(bot) if bot is not None else None
        f.linorkeyptr = ling
        return f

    assert fileio.file_linage_check(conn(0)) == 1            # lines < 1
    assert fileio.file_linage_check(conn(5, foot=0)) == 1    # foot < 1
    assert fileio.file_linage_check(conn(5, foot=9)) == 1    # foot > lines
    assert fileio.file_linage_check(conn(5, top=-1)) == 1    # top < 0
    assert fileio.file_linage_check(conn(5, bot=-1)) == 1    # bot < 0
    assert fileio.file_linage_check(conn(5, foot=3, top=1, bot=1)) == 0  # valid


def test_linage_write_page_advance(work_dir):
    """WRITE ... ADVANCING PAGE emits the bottom/top margins and resets the
    LINAGE-COUNTER to 1 (exercises the COB_WRITE_PAGE LINAGE branch).
    """
    if not hasattr(common, "COB_SELECT_LINAGE"):
        pytest.skip("LINAGE feature flag not exposed by this runtime build")
    path = work_dir / "linage_pg.txt"
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
    # WRITE ... ADVANCING PAGE resets the LINAGE counter to 1.
    set_rec(f, b"TOP")
    fileio.cob_write(f, f.record, common.COB_WRITE_PAGE, None)
    assert st(f) == "00"
    assert get_key_int(ling.linage_ctr) == 1
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
    assert path.read_bytes().count(b"\n") >= 2


# ===========================================================================
# Supporting coverage - indexed partial-key READ + OPTIONAL-missing OPEN
# ===========================================================================
def test_indexed_dbm_partial_key_read(work_dir):
    """A dbm keyed READ with a search key shorter than the full key matches the
    first record whose primary key shares that prefix.
    """
    path = work_dir / "idx_partial"
    f = make_indexed(path, 8, keylen=3)
    fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
    for key in (b"AB1", b"AB2", b"XY9"):
        set_rec(f, key + b"data")
        fileio.cob_write(f, f.record, 0, None)
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)

    g = make_indexed(path, 8, keylen=3)
    fileio.cob_open(g, common.COB_OPEN_INPUT, 0, None)
    # A 2-byte partial key "AB" matches the first "AB*" record.
    set_search_key(g.keys[0].field, b"AB")
    fileio.cob_read(g, g.keys[0].field, None, 0)
    assert st(g) == "00"
    assert bytes(g.record.data[:3]) == b"AB1"
    fileio.cob_close(g, common.COB_CLOSE_NORMAL, None)


def test_indexed_optional_missing_is_05(work_dir):
    """OPEN INPUT of an OPTIONAL, non-existent INDEXED file reports ``05`` and a
    first READ reports EOF (``10``).
    """
    f = make_indexed(work_dir / "opt_idx", 8, keylen=3)
    f.flag_optional = 1
    fileio.cob_open(f, common.COB_OPEN_INPUT, 0, None)
    assert st(f) == "05"
    fileio.cob_read(f, None, None, common.COB_READ_NEXT)
    assert st(f) == "10"
    fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)


# ===========================================================================
# Supporting coverage - LINE SEQUENTIAL COB_LS_NULLS control-byte escaping
# ===========================================================================
def test_line_sequential_ls_nulls(work_dir, monkeypatch):
    """With COB_LS_NULLS set, LINE SEQUENTIAL escapes control bytes as NUL+byte."""
    monkeypatch.setenv("COB_LS_NULLS", "1")
    saved = fileio.cob_ls_nulls
    fileio.cob_init_fileio()
    try:
        path = work_dir / "lsnulls.txt"
        f = make_file(path, 4, org=common.COB_ORG_LINE_SEQUENTIAL)
        fileio.cob_open(f, common.COB_OPEN_OUTPUT, 0, None)
        f.record.data[0:4] = bytearray(b"\x01\x02AB")
        fileio.cob_write(f, f.record, _LS_WRITE_OPT, None)
        fileio.cob_close(f, common.COB_CLOSE_NORMAL, None)
        raw = path.read_bytes()
        assert b"\x00\x01" in raw and b"\x00\x02" in raw
    finally:
        monkeypatch.delenv("COB_LS_NULLS", raising=False)
        fileio.cob_ls_nulls = saved
        fileio.cob_init_fileio()


# ===========================================================================
# Supporting coverage - filename resolution (DD_ override + containment)
# ===========================================================================
def test_resolve_filename_dd_override(work_dir, monkeypatch):
    target = work_dir / "redirected.dat"
    monkeypatch.setenv("DD_LOGICAL", str(target))
    f = fileio.cob_file()
    f.assign = char_field("LOGICAL")
    f.select_name = "LOGICAL"
    f.organization = common.COB_ORG_SEQUENTIAL
    assert fileio._resolve_filename(f) == str(target)


def test_resolve_filename_cob_file_path_containment(tmp_path, monkeypatch):
    base = tmp_path / "data"
    base.mkdir()
    monkeypatch.setattr(fileio, "cob_file_path", str(base))
    f = fileio.cob_file()
    f.assign = char_field("GOOD")
    f.select_name = "GOOD"
    f.organization = common.COB_ORG_SEQUENTIAL
    resolved = fileio._resolve_filename(f)
    assert resolved is not None
    assert os.path.realpath(resolved).startswith(os.path.realpath(str(base)))


# ===========================================================================
# Supporting coverage - EXTERNAL connector registry + default error handler
# ===========================================================================
def test_external_connector_registry():
    """A repeated EXTERNAL reference returns the identical connector object."""
    a = fileio.cob_file_external("SHARED01")
    assert common.cob_initial_external == 1
    b = fileio.cob_file_external("SHARED01")
    assert a is b
    assert common.cob_initial_external == 0


def test_default_error_handle_formats_status(work_dir, capsys):
    """``cob_default_error_handle`` emits the status text and the file name."""
    f = make_file(work_dir / "nope.dat", 4)
    f.select_name = "NOPEFILE"
    fileio.cob_open(f, common.COB_OPEN_INPUT, 0, None)  # missing -> 35
    assert st(f) == "35"
    fileio.cob_default_error_handle()
    err = capsys.readouterr().err
    assert "STATUS = 35" in err
    assert "NOPEFILE" in err


# ===========================================================================
# QA FIX Q1 - emitter<->runtime sequential-READ contract coverage.
#
# Background (QA findings C1 + Q1).  The unit tests above drive ``cob_read``
# with ``key=None`` (sequential) or a real key *field* (keyed) - but the EMITTER
# never produces ``None`` for a NULL sequential-read key.  The IMMUTABLE
# front-end (cobc/typeck.c L5276) builds the call with ``cb_int0`` and
# cobc/codegen.c therefore emits the LITERAL integer ``0``:
#
#     fileio.cob_read (h_F, 0, f_7, 1)
#
# Because no test exercised ``cob_read(f, 0, ...)``, the runtime regression where
# every SEQUENTIAL / LINE SEQUENTIAL READ returned status 23 (and left the record
# buffer stale - silent data loss) shipped green.  The tests below close that
# gap at two levels:
#   * a UNIT test that calls ``cob_read`` with the exact emitted ``key=0`` and
#     asserts sequential READ NEXT semantics (status 00 + populated record); and
#   * an end-to-end INTEGRATION test that compiles+runs a real LINE SEQUENTIAL
#     write/read program through the refactored Python backend, plus an optional
#     dual-cobc byte-for-byte parity check against the original C backend.
#
# Standard-library only: the helpers use ``subprocess`` / ``sys`` / ``os`` and
# the dev-only ``pytest`` framework; cobc-binary discovery comes from the
# stdlib-only conftest helpers.  No new runtime dependency is introduced.
# ===========================================================================
from tests.libcob_py.conftest import (  # noqa: E402  (after the importorskip guard)
    REPO_ROOT,
    cobc_orig_path,
    cobc_path,
    cobc_py_path,
    requires_cobc,
    requires_dual_cobc,
    run_cobc,
)

# A self-contained LINE SEQUENTIAL program: OPEN OUTPUT, WRITE two records,
# CLOSE; then OPEN INPUT, READ the first record, DISPLAY its FILE STATUS and the
# record bytes.  Fixed-format (area A col 8 / area B col 12) to match the proven
# parity-harness programs.  Under the C1 defect the READ reported ``R1-ST=23``
# and left the buffer holding the last-written ``TWO``; the correct (C-runtime)
# behaviour is ``R1-ST=00`` with ``REC=[ONE  ]``.
_Q1_SEQ_READ_PROG = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. SEQRDQ1.\n"
    "       ENVIRONMENT DIVISION.\n"
    "       INPUT-OUTPUT SECTION.\n"
    "       FILE-CONTROL.\n"
    "           SELECT F ASSIGN TO \"q1seq.dat\"\n"
    "               ORGANIZATION IS LINE SEQUENTIAL\n"
    "               FILE STATUS IS WS-ST.\n"
    "       DATA DIVISION.\n"
    "       FILE SECTION.\n"
    "       FD F.\n"
    "       01 F-REC PIC X(5).\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01 WS-ST PIC XX.\n"
    "       PROCEDURE DIVISION.\n"
    "           OPEN OUTPUT F.\n"
    "           MOVE \"ONE  \" TO F-REC.\n"
    "           WRITE F-REC.\n"
    "           MOVE \"TWO  \" TO F-REC.\n"
    "           WRITE F-REC.\n"
    "           CLOSE F.\n"
    "           OPEN INPUT F.\n"
    "           DISPLAY \"OPEN-ST=\" WS-ST.\n"
    "           READ F.\n"
    "           DISPLAY \"R1-ST=\" WS-ST \" REC=[\" F-REC \"]\".\n"
    "           CLOSE F.\n"
    "           STOP RUN.\n"
)


@pytest.mark.parametrize(
    "org", [common.COB_ORG_SEQUENTIAL, common.COB_ORG_LINE_SEQUENTIAL])
def test_cob_read_zero_key_is_sequential_next(work_dir, org):
    """QA C1/Q1 (unit): ``cob_read`` with the EMITTER's literal ``0`` key.

    The emitter passes integer ``0`` for a NULL ``cob_field *`` sequential-read
    key (never ``None``).  That falsy key must dispatch to sequential READ NEXT -
    status ``00`` with the record populated in order - and must NOT be routed to
    the keyed backend (which returns ``23`` for a non-keyed organization).  This
    locks the emitter<->runtime contract that finding C1 violated, for both
    SEQUENTIAL and LINE SEQUENTIAL files.
    """
    ext = ".txt" if org == common.COB_ORG_LINE_SEQUENTIAL else ".dat"
    path = work_dir / ("q1zerokey" + ext)
    write_opt = _LS_WRITE_OPT if org == common.COB_ORG_LINE_SEQUENTIAL else 0

    writer = make_file(path, 5, org=org)
    fileio.cob_open(writer, common.COB_OPEN_OUTPUT, 0, None)
    assert st(writer) == "00"
    for payload in (b"ONE  ", b"TWO  "):
        set_rec(writer, payload)
        fileio.cob_write(writer, writer.record, write_opt, None)
        assert st(writer) == "00"
    fileio.cob_close(writer, common.COB_CLOSE_NORMAL, None)

    reader = make_file(path, 5, org=org)
    fileio.cob_open(reader, common.COB_OPEN_INPUT, 0, None)
    assert st(reader) == "00"

    # The EXACT argument cobc emits for a sequential READ: the integer 0
    # (NOT None).  Pre-fix this returned 23 (mis-routed to the keyed backend).
    fileio.cob_read(reader, 0, None, common.COB_READ_NEXT)
    assert st(reader) == "00", (
        "sequential READ with emitted key=0 must be status 00 (was 23 under C1), "
        "got %s" % st(reader))
    assert bytes(reader.record.data[:reader.record.size]).rstrip() == b"ONE"

    # A second 0-key read advances to the next record (still sequential).
    fileio.cob_read(reader, 0, None, common.COB_READ_NEXT)
    assert st(reader) == "00"
    assert bytes(reader.record.data[:reader.record.size]).rstrip() == b"TWO"

    # Reading past the final record reports end-of-file (10), proving the 0-key
    # path follows the sequential state machine, not the keyed one.
    fileio.cob_read(reader, 0, None, common.COB_READ_NEXT)
    assert st(reader) == "10"
    fileio.cob_close(reader, common.COB_CLOSE_NORMAL, None)


def _q1_translate_and_run(prog_cob, work_dir, py_name, cobc_binary=None):
    """Translate *prog_cob* to Python with a refactored ``cobc`` and run it.

    Mirrors the emitter end-to-end path used by the numeric-parity harness:
    ``cobc -C -x`` emits a runnable ``main`` as *Python source* (no .pyz
    packaging), which is then executed under ``COB_PYTHON`` (else this
    interpreter) with the ``libcob_py`` runtime importable via ``PYTHONPATH``
    (the repository root).  ``COB_CONFIG_DIR`` is pinned to the in-tree dialects
    so the test does not depend on an installed compiler's compiled-in default.
    A non-zero compile/run status is an assertion failure (a genuine
    regression), never a skip - the ``@requires_cobc`` marker already handles an
    absent binary.  Returns the program's stdout decoded as Latin-1 text.
    """
    cobc = cobc_binary if cobc_binary is not None else cobc_path()
    py_path = work_dir / py_name
    prev_cfg = os.environ.get("COB_CONFIG_DIR")
    os.environ["COB_CONFIG_DIR"] = str(REPO_ROOT / "config")
    try:
        comp = run_cobc(cobc, ["-C", "-x", "-o", str(py_path), str(prog_cob)],
                        cwd=work_dir)
    finally:
        if prev_cfg is None:
            os.environ.pop("COB_CONFIG_DIR", None)
        else:
            os.environ["COB_CONFIG_DIR"] = prev_cfg
    assert comp.returncode == 0, (
        "cobc translate failed:\n%s" % comp.stderr.decode("latin-1", "replace"))
    assert py_path.is_file(), "cobc did not emit the expected .py module"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    interp = os.environ.get("COB_PYTHON") or sys.executable
    run = subprocess.run(
        [interp, str(py_path)], cwd=str(work_dir), capture_output=True,
        timeout=120, check=False, env=env)
    assert run.returncode == 0, (
        "generated program crashed (rc=%d):\n%s"
        % (run.returncode, run.stderr.decode("latin-1", "replace")))
    return run.stdout.decode("latin-1", "replace")


@requires_cobc
def test_emitted_sequential_read_end_to_end(tmp_path):
    """QA C1/Q1 (integration, THE regression guard): compile+run a real LINE
    SEQUENTIAL write/read program through the refactored Python backend.

    This exercises the actual emitted ``fileio.cob_read(h, 0, f, 1)`` call - the
    code path the unit suite never reached and the reason C1 shipped green.  The
    READ must succeed (``R1-ST=00``) and return the FIRST record (``ONE``), not
    the stale last-written buffer (``TWO``) that the status-23 defect produced.
    """
    prog = tmp_path / "seqrdq1.cob"
    prog.write_text(_Q1_SEQ_READ_PROG, encoding="latin-1")
    out = _q1_translate_and_run(prog, tmp_path, "seqrdq1.py")
    assert "OPEN-ST=00" in out, out
    assert "R1-ST=00" in out, out            # the READ must succeed (was 23)
    assert "REC=[ONE  ]" in out, out         # first record, not a stale buffer
    assert "R1-ST=23" not in out, out        # the C1 defect must not recur


@requires_dual_cobc
def test_sequential_read_dual_cobc_byte_parity(tmp_path):
    """QA C1/Q1 (dual-cobc parity, bonus): the SAME sequential-file program must
    produce byte-identical output from the original C backend and the refactored
    Python backend.  Skips cleanly when ``COBC_ORIG``/``COBC_PY`` are unset.
    """
    prog = tmp_path / "seqrddual.cob"
    prog.write_text(_Q1_SEQ_READ_PROG, encoding="latin-1")

    # Original C backend: native executable, run directly.
    exe_c = tmp_path / "seq_orig"
    comp_c = run_cobc(cobc_orig_path(), ["-x", "-o", str(exe_c), str(prog)],
                      cwd=tmp_path, timeout=180)
    assert comp_c.returncode == 0, comp_c.stderr.decode("latin-1", "replace")
    run_c = subprocess.run([str(exe_c)], cwd=str(tmp_path), capture_output=True,
                           timeout=120, check=False)
    assert run_c.returncode == 0, run_c.stderr.decode("latin-1", "replace")
    out_c = run_c.stdout.decode("latin-1", "replace")

    # Refactored Python backend: emit Python source and run it under the runtime
    # (robust even when a packaged .pyz would not be self-contained).
    out_py = _q1_translate_and_run(prog, tmp_path, "seq_py.py", cobc_py_path())

    assert out_c == out_py, (
        "sequential-file program output diverged between the C and Python "
        "backends (C1 regression)\nC : %r\nPy: %r" % (out_c, out_py))
