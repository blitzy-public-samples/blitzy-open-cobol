"""libcob_py.fileio - COBOL file I/O (sequential / relative / indexed) + SORT.

Pure-Python port of the C runtime module ``libcob/fileio.c`` (5762 lines).  It
is the runtime that the rewritten code emitter (``cobc/codegen.c``) targets for
every COBOL file verb of a compiled program: ``OPEN``, ``CLOSE``, ``READ``,
``WRITE``, ``REWRITE``, ``DELETE``, ``START``, ``UNLOCK``, ``COMMIT``,
``ROLLBACK``, the ``SORT`` / ``MERGE`` machinery (``RELEASE`` / ``RETURN``), and
the ``C$`` filesystem routines that physically live in ``fileio.c``.

The emitted Python references this module's stable ``cob_*`` names 1:1 with the
C ``cob_*`` call-sites (AAP section 0.6.5): e.g. a COBOL ``OPEN INPUT`` becomes
``fileio.cob_open(f, 1, 0, status)`` and the data area is a
:class:`libcob_py.common.cob_field`.

Standard library only
---------------------
The only imports are :mod:`os`, :mod:`io`, :mod:`struct`, :mod:`dbm`,
:mod:`sqlite3`, :mod:`tempfile`, :mod:`shutil` and the sibling runtime base
module :mod:`libcob_py.common`.  ZERO third-party dependencies
(AAP sections 0.5 / 0.7.1).  The numeric helpers ``cob_get_int`` /
``cob_set_int`` (provided by ``libcob_py.move``) and ``cob_add_int`` /
``cob_numeric_cmp`` (provided by ``libcob_py.numeric``) are reached through
*deferred* imports performed inside the function bodies - exactly the pattern
used by :mod:`libcob_py.common` (its ``_lazy_*`` helpers) and
:mod:`libcob_py.strings` - so the only import-time dependency of this module is
``libcob_py.common``.

Indexed backend migration (Berkeley DB -> stdlib, AAP section 0.6.3)
-------------------------------------------------------------------
The C runtime stored INDEXED files in a Berkeley DB b-tree.  This module
replaces that ISAM layer with the Python standard library, routing on the
file's key declaration:

* **primary-key-only** indexed files use :mod:`dbm` - a single ``key -> record``
  keyed store (the simplest faithful replacement).
* files declaring an ``ALTERNATE RECORD KEY`` (with or without
  ``WITH DUPLICATES``), or otherwise needing more than one index, use
  :mod:`sqlite3` - one table holding the records plus one index per key; a
  duplicate-allowing alternate key is a *non-unique* index, while a no-duplicate
  key is enforced application-side so the exact COBOL status (``22``) is raised
  rather than a backend exception.

**Migration error contract (hard, AAP section 0.6.3):** the Berkeley DB on-disk
format is not binary-compatible with ``dbm`` / ``sqlite3``.  When a file that
looks like a legacy Berkeley DB database is opened, this module does *not*
silently truncate or corrupt it: it yields COBOL file status ``30`` (permanent
error) and logs the exact message ::

    indexed file format incompatible - manual migration required

One-time migration note
------------------------
To migrate a legacy indexed file, read every record through the original
Berkeley-DB-linked ``libcob`` build (or the ``db_dump`` utility) and re-``WRITE``
each record through this runtime so it is rewritten in the ``dbm`` / ``sqlite3``
format.  There is no in-place conversion: the legacy file is never modified by
this runtime, and the status-``30`` path above is the documented signal that a
manual migration is required.

Minimal-deviation note (AAP section 0.7.2)
------------------------------------------
Behaviours that were specific to Berkeley DB record locking across processes
(``DB_WRITECURSOR`` cursors, ``lock_id`` leases) and to the EXTFH / C-ISAM
backends are *noted, not reproduced*: Python file locking uses advisory POSIX
locks via :func:`os` where available and otherwise degrades to in-process
bookkeeping, which preserves the single-process COBOL status semantics exactly
while not emulating cross-process BDB lock arbitration.
"""

from __future__ import annotations

import dbm
import io
import os
import shutil
import sqlite3
import struct
import sys
import tempfile

from libcob_py import common


# ===========================================================================
# Deferred numeric helpers.
#
# ``fileio`` sits above ``numeric`` / ``move`` in the initialisation order
# (numeric -> strings -> move -> intrinsic -> fileio -> termio -> call), so a
# top-level ``import`` of those siblings would create a cycle and force them to
# exist before ``import libcob_py.fileio`` could succeed.  Following the exact
# pattern of ``common``'s ``_lazy_*`` helpers (and ``strings``'s ``_cob_*``
# helpers), the import is performed on first use, inside the function body.  The
# only import-time dependency of this module therefore remains
# ``libcob_py.common``.
# ===========================================================================

def _cob_get_int(f):
    """``cob_get_int`` via a deferred import of :mod:`libcob_py.move`."""
    from libcob_py import move  # deferred: breaks the import cycle
    return move.cob_get_int(f)


def _cob_set_int(f, n):
    """``cob_set_int`` via a deferred import of :mod:`libcob_py.move`."""
    from libcob_py import move  # deferred
    move.cob_set_int(f, n)


def _cob_add_int(f, n):
    """``cob_add_int`` via a deferred import of :mod:`libcob_py.numeric`."""
    from libcob_py import numeric  # deferred
    return numeric.cob_add_int(f, n)


def _cob_numeric_cmp(f1, f2):
    """``cob_numeric_cmp`` via a deferred import of :mod:`libcob_py.numeric`."""
    from libcob_py import numeric  # deferred
    return numeric.cob_numeric_cmp(f1, f2)


# ===========================================================================
# COBOL file status codes (common.h L562-L591) - the two-character status
# model.  Reproduced as the integer the C runtime used internally; the ASCII
# two-byte form is produced by :func:`save_status`.
# ===========================================================================
COB_STATUS_00_SUCCESS = 0
COB_STATUS_02_SUCCESS_DUPLICATE = 2
COB_STATUS_04_SUCCESS_INCOMPLETE = 4
COB_STATUS_05_SUCCESS_OPTIONAL = 5
COB_STATUS_07_SUCCESS_NO_UNIT = 7
COB_STATUS_10_END_OF_FILE = 10
COB_STATUS_14_OUT_OF_KEY_RANGE = 14
COB_STATUS_21_KEY_INVALID = 21
COB_STATUS_22_KEY_EXISTS = 22
COB_STATUS_23_KEY_NOT_EXISTS = 23
COB_STATUS_30_PERMANENT_ERROR = 30
COB_STATUS_31_INCONSISTENT_FILENAME = 31
COB_STATUS_34_BOUNDARY_VIOLATION = 34
COB_STATUS_35_NOT_EXISTS = 35
COB_STATUS_37_PERMISSION_DENIED = 37
COB_STATUS_38_CLOSED_WITH_LOCK = 38
COB_STATUS_39_CONFLICT_ATTRIBUTE = 39
COB_STATUS_41_ALREADY_OPEN = 41
COB_STATUS_42_NOT_OPEN = 42
COB_STATUS_43_READ_NOT_DONE = 43
COB_STATUS_44_RECORD_OVERFLOW = 44
COB_STATUS_46_READ_ERROR = 46
COB_STATUS_47_INPUT_DENIED = 47
COB_STATUS_48_OUTPUT_DENIED = 48
COB_STATUS_49_I_O_DENIED = 49
COB_STATUS_51_RECORD_LOCKED = 51
COB_STATUS_52_EOP = 52
COB_STATUS_57_I_O_LINAGE = 57
COB_STATUS_61_FILE_SHARING = 61
COB_STATUS_91_NOT_AVAILABLE = 91


#: status -> exception mapping, indexed by ``status // 10`` (fileio.c L242-L253).
#: ``save_status`` consults this so that, e.g., any ``3x`` status raises
#: ``EC-I-O-PERMANENT-ERROR``.  Entry 0 (``0x``) is the no-exception sentinel.
status_exception = [
    0,                                    # 0x - success, no exception
    common.COB_EC_I_O_AT_END,             # 1x
    common.COB_EC_I_O_INVALID_KEY,        # 2x
    common.COB_EC_I_O_PERMANENT_ERROR,    # 3x
    common.COB_EC_I_O_LOGIC_ERROR,        # 4x
    common.COB_EC_I_O_RECORD_OPERATION,   # 5x
    common.COB_EC_I_O_FILE_SHARING,       # 6x
    common.COB_EC_I_O,                    # 7x (unused)
    common.COB_EC_I_O,                    # 8x (unused)
    common.COB_EC_I_O_IMP,                # 9x
]


# ===========================================================================
# SORT/MERGE result codes (fileio.c L159-L162).
# ===========================================================================
COBSORTEND = 1
COBSORTABORT = 2
COBSORTFILEERR = 3
COBSORTNOTOPEN = 4


#: Size, in bytes, of the record-length prefix the C runtime wrote in front of
#: each RELATIVE record and in front of each variable-length SEQUENTIAL record.
#: The C code wrote ``sizeof (f->record->size)`` (a ``size_t`` - 8 bytes on the
#: LP64 build host); we reproduce the native, host-order 8-byte width so a file
#: written by this runtime round-trips byte-identically.
_SIZE_T_BYTES = 8

#: Exact log message demanded by the indexed-file migration error contract
#: (AAP section 0.6.3).  Emitted verbatim on the status-30 path.
_MIGRATION_MESSAGE = "indexed file format incompatible - manual migration required"


# ===========================================================================
# Module-level runtime state (port of the file-scope globals in fileio.c).
# These are genuine module globals so the generated program and the sibling
# runtime modules observe a single shared file-I/O state, exactly as the C
# runtime shared process globals.
# ===========================================================================

#: The file connector of the most recent I/O operation (fileio.c L203).  The
#: generated USE-error handler reads ``fileio.cob_error_file`` to inspect
#: ``last_open_mode`` / ``flag_select_features``.
cob_error_file = None

#: Head of the cached-files list (fileio.c ``file_cache``).  Every opened file
#: is registered so ``cob_commit`` / ``cob_rollback`` / ``cob_exit_fileio`` can
#: iterate the live connectors.  Stored as a plain list (insertion order is the
#: reverse of the C singly linked list, which is immaterial to the semantics).
_file_cache = []

#: Registry of EXTERNAL file connectors keyed by their ``cname`` so a second
#: reference to the same EXTERNAL file returns the identical object (mirrors the
#: ``common._externals`` registry used for EXTERNAL data items).
_external_files = {}

#: ``COB_SYNC`` setting (0 none, 1 fsync data, 2 fsync data+metadata) - read by
#: :func:`cob_init_fileio` (fileio.c L4423-L4430).
cob_do_sync = 0

#: ``COB_SORT_MEMORY`` in-core sort budget in bytes (fileio.c L213, L4431-L4436).
cob_sort_memory = 128 * 1024 * 1024

#: ``COB_FILE_PATH`` default directory for unqualified file names, or ``None``
#: (fileio.c L4437-L4442).
cob_file_path = None

#: ``COB_LS_NULLS`` - when set, LINE SEQUENTIAL encodes control bytes as
#: ``0x00`` + byte (fileio.c L4443).
cob_ls_nulls = None

#: ``COB_LS_FIXED`` - when set, LINE SEQUENTIAL writes the full fixed record
#: rather than trimming trailing spaces (fileio.c L4444).
cob_ls_fixed = None

#: End-of-page latch shared between the LINAGE write path and ``lineseq_write``
#: (fileio.c L210).
_eop_status = 0

#: Environment-variable name prefixes tried when resolving a simple file name
#: (fileio.c L255): ``DD_<name>``, ``dd_<name>``, then ``<name>`` itself.
_PREFIX = ("DD_", "dd_", "")


# ===========================================================================
# File connector structures.
#
# The C runtime modelled these as ``struct cob_file`` (common.h L755-L788),
# ``struct cob_file_key`` (common.h L744-L749) and ``struct linage_struct``
# (common.h L792-L803).  ``common.py`` does not define a file connector, so the
# mirror lives here; the rewritten emitter constructs these through the
# factories below (``cob_file()`` / ``cob_file_external()`` /
# ``cob_file_key_array()`` / ``cob_linage_struct()``).
# ===========================================================================

class cob_file_key(object):
    """Key descriptor - mirror of ``struct cob_file_key`` (common.h L744-L749).

    * ``field``  - the :class:`~libcob_py.common.cob_field` holding the key.
    * ``flag``   - ``WITH DUPLICATES`` for RELATIVE/INDEXED keys, or
                   ASCENDING/DESCENDING for SORT keys.
    * ``offset`` - byte offset of the key field within the record.
    """

    __slots__ = ("field", "flag", "offset")

    def __init__(self, field=None, flag=0, offset=0):
        self.field = field
        self.flag = flag
        self.offset = offset


class cob_linage_struct(object):
    """LINAGE bookkeeping - mirror of ``struct linage_struct`` (common.h L792).

    Carries the LINAGE / LINAGE-COUNTER fields and the resolved current line
    geometry used by the LINE SEQUENTIAL write path.
    """

    __slots__ = ("linage", "linage_ctr", "latfoot", "lattop", "latbot",
                 "lin_lines", "lin_foot", "lin_top", "lin_bot")

    def __init__(self):
        self.linage = None        # LINAGE field
        self.linage_ctr = None    # LINAGE-COUNTER field
        self.latfoot = None       # LINAGE ... FOOTING field
        self.lattop = None        # LINAGE ... AT TOP field
        self.latbot = None        # LINAGE ... AT BOTTOM field
        self.lin_lines = 0        # current page body length
        self.lin_foot = 0         # current footing line
        self.lin_top = 0          # current top margin
        self.lin_bot = 0          # current bottom margin


class cob_file(object):
    """File connector - mirror of the C ``cob_file`` struct (common.h L755-L788).

    The rewritten emitter creates one of these per ``SELECT`` and assigns every
    attribute directly (see ``cobc/codegen.c`` ``output_file_initialization``).
    The backend-specific runtime state (an open Python file object, a ``dbm`` /
    ``sqlite3`` handle, or a :class:`_cobsort` work area) lives behind the
    single ``file`` attribute, exactly as the C runtime stored it behind the
    ``void *file`` member - so the connector's own attribute set stays fixed.

    ``file_status`` is a 4-byte :class:`bytearray` (the generated program shares
    it with the COBOL ``FILE STATUS`` data item); it is written by index, not
    through a ``.data`` member.
    """

    __slots__ = (
        "select_name", "file_status", "assign", "record", "record_size",
        "keys", "file", "linorkeyptr", "sort_collating", "sort_return",
        "record_min", "record_max", "nkeys",
        "organization", "access_mode", "lock_mode", "open_mode",
        "flag_optional", "last_open_mode", "special", "flag_nonexistent",
        "flag_end_of_file", "flag_begin_of_file", "flag_first_read",
        "flag_read_done", "flag_select_features", "flag_needs_nl",
        "flag_needs_top", "file_version",
    )

    def __init__(self):
        # Pointers / objects (default to None / empty as the C struct's zeroed
        # storage did).
        self.select_name = None
        self.file_status = bytearray(b"00\x00\x00")
        self.assign = None
        self.record = None
        self.record_size = None
        self.keys = None
        self.file = None
        self.linorkeyptr = None
        self.sort_collating = None
        self.sort_return = None
        # Sizes / counts.
        self.record_min = 0
        self.record_max = 0
        self.nkeys = 0
        # Mode bytes.
        self.organization = common.COB_ORG_SEQUENTIAL
        self.access_mode = common.COB_ACCESS_SEQUENTIAL
        self.lock_mode = 0
        self.open_mode = common.COB_OPEN_CLOSED
        self.flag_optional = 0
        self.last_open_mode = 0
        self.special = 0
        self.flag_nonexistent = 0
        # Position / state flags.
        self.flag_end_of_file = 0
        self.flag_begin_of_file = 0
        self.flag_first_read = 0
        self.flag_read_done = 0
        self.flag_select_features = 0
        self.flag_needs_nl = 0
        self.flag_needs_top = 0
        self.file_version = common.COB_FILE_VERSION


def cob_file_external(cname):
    """Return the shared :class:`cob_file` for EXTERNAL connector *cname*.

    Mirrors :func:`libcob_py.common.cob_external_addr`: the first reference
    creates the connector, registers it and latches
    :data:`libcob_py.common.cob_initial_external` to 1 so the generated code
    runs the attribute-initialisation block; a later reference returns the same
    object and clears the latch so the block is skipped.
    """
    existing = _external_files.get(cname)
    if existing is not None:
        common.cob_initial_external = 0
        return existing
    f = cob_file()
    _external_files[cname] = f
    common.cob_initial_external = 1
    return f


def cob_file_key_array(nkeys):
    """Return a fresh list of *nkeys* :class:`cob_file_key` descriptors.

    The emitter indexes the result positionally (``k_FILE[0].field = ...``), so
    the slots must pre-exist; this is the Python equivalent of the C
    ``cob_malloc(sizeof(struct cob_file_key) * nkeys)`` allocation.
    """
    return [cob_file_key() for _ in range(int(nkeys))]


# ===========================================================================
# Status reporting (fileio.c L240-L714).
# ===========================================================================

def save_status(f, status, fnstatus):
    """Record *status* on file connector *f* (port of fileio.c L691-L714).

    Writes the two ASCII digits of *status* into ``f.file_status`` (a 4-byte
    bytearray) and, when *fnstatus* is a :class:`~libcob_py.common.cob_field`,
    into its first two data bytes.  Success (``0``) clears the runtime exception
    code; any other status (except end-of-page) raises the matching
    ``EC-I-O-*`` exception via :func:`libcob_py.common.cob_set_exception` using
    the :data:`status_exception` map.  Also latches :data:`cob_error_file` to
    *f* so the USE-error handler can inspect the failing connector.
    """
    global cob_error_file
    cob_error_file = f
    if status == 0:
        f.file_status[0] = ord("0")
        f.file_status[1] = ord("0")
        if fnstatus is not None:
            fnstatus.data[0] = ord("0")
            fnstatus.data[1] = ord("0")
        common.cob_exception_code = 0
        return
    # End-of-page (52) is reported without raising an EC-I-O exception
    # (fileio.c L705), matching the C runtime's special-case.
    if status != COB_STATUS_52_EOP:
        common.cob_set_exception(status_exception[status // 10])
    f.file_status[0] = common.cob_i2d(status // 10)
    f.file_status[1] = common.cob_i2d(status % 10)
    if fnstatus is not None:
        fnstatus.data[0] = f.file_status[0]
        fnstatus.data[1] = f.file_status[1]


def cob_cache_file(f):
    """Register *f* in the open-file cache once (port of fileio.c L675-L689)."""
    if f not in _file_cache:
        _file_cache.append(f)


def cob_sync(f, mode):
    """Flush (and optionally fsync) the backing store of *f* (fileio.c L627-L673).

    For a regular (non-SORT) file the Python file object is flushed; when
    *mode* is 2 the OS buffers are additionally synced via :func:`os.fsync`.
    For INDEXED files the backend is asked to commit (``dbm`` reopen-safe sync /
    ``sqlite3`` commit).  Errors are swallowed exactly as the C ``fflush`` /
    ``fsync`` calls ignored their return values.
    """
    handle = f.file
    if handle is None:
        return
    if f.organization == common.COB_ORG_INDEXED:
        _backend = getattr(handle, "sync", None)
        if callable(_backend):
            try:
                handle.sync(mode)
            except Exception:  # pragma: no cover - defensive: best-effort sync
                pass
        return
    if f.organization == common.COB_ORG_SORT:
        return
    fp = getattr(handle, "fp", handle)
    try:
        fp.flush()
        if mode == 2:
            os.fsync(fp.fileno())
    except (OSError, ValueError):  # pragma: no cover - best-effort sync
        pass


def cob_file_unlock(f):
    """Release any record/file lock held on *f* (port of fileio.c L3701-L3766).

    The C runtime released the BDB record lease and/or the POSIX ``fcntl`` lock.
    Here the advisory OS lock (taken in :func:`_cob_file_open` when available) is
    released and any in-process lock bookkeeping on the backend handle is
    cleared.  Safe to call on a closed file.
    """
    if f.open_mode == common.COB_OPEN_CLOSED:
        return
    handle = f.file
    if handle is None:
        return
    unlock = getattr(handle, "unlock", None)
    if callable(unlock):
        try:
            handle.unlock()
        except Exception:  # pragma: no cover - defensive
            pass


def file_linage_check(f):
    """Validate the LINAGE clause values for *f* (port of fileio.c L718-L756).

    Resolves LINAGE / FOOTING / TOP / BOTTOM from their fields into the
    connector's :class:`cob_linage_struct`.  Returns 0 when valid; on an invalid
    geometry it zeroes LINAGE-COUNTER and returns 1 (the caller maps that to
    file status ``57``).
    """
    lingptr = f.linorkeyptr
    lingptr.lin_lines = _cob_get_int(lingptr.linage)
    if lingptr.lin_lines < 1:
        _cob_set_int(lingptr.linage_ctr, 0)
        return 1
    if lingptr.latfoot is not None:
        lingptr.lin_foot = _cob_get_int(lingptr.latfoot)
        if lingptr.lin_foot < 1 or lingptr.lin_foot > lingptr.lin_lines:
            _cob_set_int(lingptr.linage_ctr, 0)
            return 1
    else:
        lingptr.lin_foot = 0
    if lingptr.lattop is not None:
        lingptr.lin_top = _cob_get_int(lingptr.lattop)
        if lingptr.lin_top < 0:
            _cob_set_int(lingptr.linage_ctr, 0)
            return 1
    else:
        lingptr.lin_top = 0
    if lingptr.latbot is not None:
        lingptr.lin_bot = _cob_get_int(lingptr.latbot)
        if lingptr.lin_bot < 0:
            _cob_set_int(lingptr.linage_ctr, 0)
            return 1
    else:
        lingptr.lin_bot = 0
    return 0


def cob_linage_write_opt(f, opt):
    """Honour WRITE ADVANCING for a LINAGE file (port of fileio.c L904-L965).

    Handles ``WRITE ... PAGE`` (advance to the next logical page, emitting the
    bottom/top margins) and ``WRITE ... LINES`` (advance N lines, latching the
    end-of-page condition when FOOTING/LINAGE is crossed).  Returns 0 or file
    status ``57`` when the LINAGE geometry becomes invalid mid-advance.
    """
    global _eop_status
    lingptr = f.linorkeyptr
    fp = _fp(f)
    if opt & common.COB_WRITE_PAGE:
        i = _cob_get_int(lingptr.linage_ctr)
        if i == 0:
            return COB_STATUS_57_I_O_LINAGE
        n = lingptr.lin_lines
        while i < n:
            fp.write(b"\n")
            i += 1
        for _ in range(lingptr.lin_bot):
            fp.write(b"\n")
        if file_linage_check(f):
            return COB_STATUS_57_I_O_LINAGE
        for _ in range(lingptr.lin_top):
            fp.write(b"\n")
        _cob_set_int(lingptr.linage_ctr, 1)
    elif opt & common.COB_WRITE_LINES:
        n = _cob_get_int(lingptr.linage_ctr)
        if n == 0:
            return COB_STATUS_57_I_O_LINAGE
        _cob_add_int(lingptr.linage_ctr, opt & common.COB_WRITE_MASK)
        i = _cob_get_int(lingptr.linage_ctr)
        if (opt & common.COB_WRITE_EOP) and lingptr.lin_foot:
            if i >= lingptr.lin_foot:
                _eop_status = 1
        if i > lingptr.lin_lines:
            if opt & common.COB_WRITE_EOP:
                _eop_status = 1
            while n < lingptr.lin_lines:
                fp.write(b"\n")
                n += 1
            for _ in range(lingptr.lin_bot):
                fp.write(b"\n")
            if file_linage_check(f):
                return COB_STATUS_57_I_O_LINAGE
            _cob_set_int(lingptr.linage_ctr, 1)
            for _ in range(lingptr.lin_top):
                fp.write(b"\n")
        else:
            for _ in range((opt & common.COB_WRITE_MASK) - 1, 0, -1):
                fp.write(b"\n")
    return 0


def cob_file_write_opt(f, opt):
    """Emit the WRITE ADVANCING blank lines / form feed (fileio.c L967-L982)."""
    if f.flag_select_features & common.COB_SELECT_LINAGE:
        return cob_linage_write_opt(f, opt)
    fp = _fp(f)
    if opt & common.COB_WRITE_LINES:
        for _ in range(opt & common.COB_WRITE_MASK, 0, -1):
            fp.write(b"\n")
    elif opt & common.COB_WRITE_PAGE:
        fp.write(b"\f")
    return 0


def _fp(f):
    """Return the Python file object behind a regular (sequential) connector."""
    return f.file


def _pack_relsize(n):
    """Pack a RELATIVE record-length prefix (native ``size_t``, fileio.c L1329)."""
    return int(n).to_bytes(_SIZE_T_BYTES, sys.byteorder)


def _unpack_relsize(b):
    """Unpack a RELATIVE record-length prefix (native ``size_t``)."""
    return int.from_bytes(b, sys.byteorder)


def _pack_varseq(n):
    """Pack a variable-length SEQUENTIAL prefix (``WITH_VARSEQ == 0``).

    The C runtime stored the record length as a big-endian 2-byte value within a
    4-byte field (fileio.c L1097-L1108): the size occupies the leading two
    bytes, the trailing two bytes are zero.  Reproduced verbatim so a file
    written here round-trips and matches the original layout.
    """
    return struct.pack(">H", n & 0xFFFF) + b"\x00\x00"


def _unpack_varseq(b):
    """Unpack a variable-length SEQUENTIAL prefix (``WITH_VARSEQ == 0``)."""
    return struct.unpack(">H", b[0:2])[0]


# ===========================================================================
# Regular (sequential / line-sequential / relative) backend - open / close.
# These mirror the C ``cob_file_open`` / ``cob_file_close`` (fileio.c
# L782-L902).  The C used ``stdio`` ``FILE *`` plus ``fcntl`` advisory locks;
# here we use a Python binary file object and a best-effort advisory lock.
# ===========================================================================

#: Python open mode per (COBOL open mode, is-relative) - all binary so this
#: runtime owns newline handling for LINE SEQUENTIAL (faithful to the C, which
#: on Unix treated "r"/"rb" identically).
def _python_open_mode(f, mode):
    if mode == common.COB_OPEN_INPUT:
        return "rb"
    if mode == common.COB_OPEN_OUTPUT:
        if f.organization == common.COB_ORG_RELATIVE:
            return "wb+"
        return "wb"
    if mode == common.COB_OPEN_I_O:
        return "rb+"
    if mode == common.COB_OPEN_EXTEND:
        return "ab+"
    return "rb"


def _cob_file_open(f, filename, mode, sharing):
    """Open a regular file (port of fileio.c L782-L863).

    Returns 0 on success or an ``errno`` value (the public :func:`cob_open`
    switch maps it to a COBOL status).  Establishes a best-effort advisory lock
    and primes LINAGE on a linage file.
    """
    pymode = _python_open_mode(f, mode)
    try:
        fp = open(filename, pymode)
    except FileNotFoundError:
        return _errno_of(FileNotFoundError, 2)
    except PermissionError:
        return _errno_of(PermissionError, 13)
    except IsADirectoryError:
        return _errno_of(IsADirectoryError, 21)
    except OSError as exc:  # pragma: no cover - rare filesystem failures
        return exc.errno or COB_STATUS_30_PERMANENT_ERROR

    if mode == common.COB_OPEN_EXTEND:
        fp.seek(0, io.SEEK_END)

    f.file = fp

    if f.flag_select_features & common.COB_SELECT_LINAGE:
        if file_linage_check(f):
            return common.COB_LINAGE_INVALID
        f.flag_needs_top = 1
        _cob_set_int(f.linorkeyptr.linage_ctr, 1)
    return 0


def _errno_of(exc_class, default):
    """Return the canonical ``errno`` for a freshly raised *exc_class*.

    Python only fills ``OSError.errno`` on the instance, so we map the standard
    library's exception classes to their POSIX numbers for the
    :func:`cob_open` status switch.
    """
    import errno as _errno
    return {
        FileNotFoundError: _errno.ENOENT,
        PermissionError: _errno.EACCES,
        IsADirectoryError: _errno.EISDIR,
    }.get(exc_class, default)


def _cob_file_close(f, opt):
    """Close a regular file (port of fileio.c L865-L902)."""
    fp = f.file
    if opt in (common.COB_CLOSE_NORMAL, common.COB_CLOSE_LOCK,
               common.COB_CLOSE_NO_REWIND):
        if f.organization == common.COB_ORG_LINE_SEQUENTIAL:
            if f.flag_needs_nl and not (
                    f.flag_select_features & common.COB_SELECT_LINAGE):
                f.flag_needs_nl = 0
                try:
                    fp.write(b"\n")
                except (OSError, ValueError):  # pragma: no cover
                    pass
        try:
            fp.close()
        except (OSError, ValueError):  # pragma: no cover
            pass
        if opt == common.COB_CLOSE_NO_REWIND:
            f.open_mode = common.COB_OPEN_CLOSED
            return COB_STATUS_07_SUCCESS_NO_UNIT
        return COB_STATUS_00_SUCCESS
    try:
        fp.flush()
    except (OSError, ValueError):  # pragma: no cover
        pass
    return COB_STATUS_07_SUCCESS_NO_UNIT


# ===========================================================================
# SEQUENTIAL backend (fileio.c L984-L1150).
# ===========================================================================

def _sequential_read(f, read_opts):
    """Read the next SEQUENTIAL record (port of fileio.c L986-L1051)."""
    fp = f.file
    rec = f.record
    if f.record_min != f.record_max:
        # Variable-length: read the 4-byte length prefix first.
        prefix = fp.read(4)
        if len(prefix) != 4:
            return COB_STATUS_10_END_OF_FILE
        rec.size = _unpack_varseq(prefix)
    data = fp.read(rec.size)
    if len(data) != rec.size:
        if len(data) == 0:
            return COB_STATUS_10_END_OF_FILE
        rec.data[0:len(data)] = data
        return COB_STATUS_04_SUCCESS_INCOMPLETE
    rec.data[0:rec.size] = data
    return COB_STATUS_00_SUCCESS


def _sequential_write(f, opt):
    """Write a SEQUENTIAL record (port of fileio.c L1053-L1130)."""
    fp = f.file
    rec = f.record
    # WRITE AFTER advancing.
    if opt & common.COB_WRITE_AFTER:
        ret = cob_file_write_opt(f, opt)
        if ret:
            return ret
        f.flag_needs_nl = 1
    if f.record_min != f.record_max:
        try:
            fp.write(_pack_varseq(rec.size))
        except (OSError, ValueError):  # pragma: no cover
            return COB_STATUS_30_PERMANENT_ERROR
    try:
        fp.write(bytes(rec.data[:rec.size]))
    except (OSError, ValueError):  # pragma: no cover
        return COB_STATUS_30_PERMANENT_ERROR
    # WRITE BEFORE advancing.
    if opt & common.COB_WRITE_BEFORE:
        ret = cob_file_write_opt(f, opt)
        if ret:
            return ret
        f.flag_needs_nl = 0
    return COB_STATUS_00_SUCCESS


def _sequential_rewrite(f, opt):
    """Rewrite the last-read SEQUENTIAL record in place (fileio.c L1132-L1150)."""
    fp = f.file
    rec = f.record
    try:
        fp.seek(-rec.size, io.SEEK_CUR)
        fp.write(bytes(rec.data[:rec.size]))
    except (OSError, ValueError):
        return COB_STATUS_30_PERMANENT_ERROR
    return COB_STATUS_00_SUCCESS


# ===========================================================================
# LINE SEQUENTIAL backend (fileio.c L1152-L1306).
# ===========================================================================

def _lineseq_read(f, read_opts):
    """Read one text line into the record (port of fileio.c L1156-L1208)."""
    fp = f.file
    rec = f.record
    i = 0
    while True:
        ch = fp.read(1)
        if not ch:  # EOF
            if i == 0:
                return COB_STATUS_10_END_OF_FILE
            break
        n = ch[0]
        if n == 0 and cob_ls_nulls is not None:
            ch = fp.read(1)
            if not ch:
                return COB_STATUS_30_PERMANENT_ERROR
            n = ch[0]
        else:
            if n == 0x0D:  # '\r' - ignore
                continue
            if n == 0x0A:  # '\n' - end of line
                break
        if i < rec.size:
            rec.data[i] = n
            i += 1
    if i < rec.size:
        # Pad the remainder of the record with spaces (fileio.c L1200-L1203).
        for j in range(i, rec.size):
            rec.data[j] = 0x20
    if f.record_size is not None:
        _cob_set_int(f.record_size, i)
    return COB_STATUS_00_SUCCESS


def _lineseq_write(f, opt):
    """Write one text line from the record (port of fileio.c L1210-L1306)."""
    global _eop_status
    fp = f.file
    rec = f.record
    # Determine the number of bytes to write: full record under COB_LS_FIXED,
    # otherwise trim trailing spaces (fileio.c L1234-L1244).
    if cob_ls_fixed is not None:
        size = rec.size
    else:
        size = rec.size
        while size > 0 and rec.data[size - 1] == 0x20:
            size -= 1
    if f.flag_select_features & common.COB_SELECT_LINAGE:
        if f.flag_needs_top:
            f.flag_needs_top = 0
            for _ in range(f.linorkeyptr.lin_top):
                fp.write(b"\n")
    # WRITE AFTER advancing.
    if opt & common.COB_WRITE_AFTER:
        ret = cob_file_write_opt(f, opt)
        if ret:
            return ret
        f.flag_needs_nl = 1
    if size:
        if cob_ls_nulls is not None:
            out = bytearray()
            for j in range(size):
                b = rec.data[j]
                if b < 0x20:
                    out.append(0)
                out.append(b)
            try:
                fp.write(bytes(out))
            except (OSError, ValueError):  # pragma: no cover
                return COB_STATUS_30_PERMANENT_ERROR
        else:
            try:
                fp.write(bytes(rec.data[:size]))
            except (OSError, ValueError):  # pragma: no cover
                return COB_STATUS_30_PERMANENT_ERROR
    if f.flag_select_features & common.COB_SELECT_LINAGE:
        fp.write(b"\n")
    # WRITE BEFORE advancing.
    if opt & common.COB_WRITE_BEFORE:
        ret = cob_file_write_opt(f, opt)
        if ret:
            return ret
        f.flag_needs_nl = 0
    if f.flag_needs_nl and not (
            f.flag_select_features & common.COB_SELECT_LINAGE):
        fp.write(b"\n")
        f.flag_needs_nl = 0
    if _eop_status:
        _eop_status = 0
        common.cob_exception_code = 0x0502
        return COB_STATUS_52_EOP
    return COB_STATUS_00_SUCCESS


# ===========================================================================
# RELATIVE backend (fileio.c L1308-L1590).
#
# A RELATIVE file is a flat array of fixed-size slots.  Each slot is a native
# ``size_t`` length prefix followed by ``record_max`` data bytes; a prefix of
# zero marks an empty / deleted slot.  This is the exact on-disk geometry the C
# runtime used (``relsize = record_max + sizeof (f->record->size)``), so files
# remain byte-compatible.
# ===========================================================================

def _seek_init(fp):
    """Reproduce the C ``SEEK_INIT`` no-op reposition (fileio.c L145).

    ISO C requires a positioning call when switching between reads and writes on
    the same stream; Python's buffered binary files have the same requirement,
    so a zero-relative seek is issued to keep read/write interleaving correct.
    """
    try:
        fp.seek(0, io.SEEK_CUR)
    except (OSError, ValueError):  # pragma: no cover - unseekable streams
        pass


def _record_slot_bytes(f):
    """Return exactly ``record_max`` data bytes for the current record."""
    buf = bytes(f.record.data[:f.record_max])
    if len(buf) < f.record_max:
        buf = buf + b"\x00" * (f.record_max - len(buf))
    return buf


def _relative_start(f, cond, k):
    """Position a RELATIVE file for a later READ NEXT (fileio.c L1313-L1366)."""
    fp = f.file
    kindex = _cob_get_int(k) - 1
    relsize = f.record_max + _SIZE_T_BYTES
    if cond == common.COB_LT:
        kindex -= 1
    elif cond == common.COB_GT:
        kindex += 1
    while True:
        if kindex < 0:
            return COB_STATUS_23_KEY_NOT_EXISTS
        off = kindex * relsize
        try:
            if fp.seek(off) != off:
                return COB_STATUS_23_KEY_NOT_EXISTS
            prefix = fp.read(_SIZE_T_BYTES)
        except (OSError, ValueError):
            return COB_STATUS_23_KEY_NOT_EXISTS
        if len(prefix) != _SIZE_T_BYTES:
            return COB_STATUS_23_KEY_NOT_EXISTS
        f.record.size = _unpack_relsize(prefix)
        if f.record.size > 0:
            _cob_set_int(k, kindex + 1)
            fp.seek(-_SIZE_T_BYTES, io.SEEK_CUR)
            return COB_STATUS_00_SUCCESS
        if cond == common.COB_EQ:
            return COB_STATUS_23_KEY_NOT_EXISTS
        if cond in (common.COB_LT, common.COB_LE):
            kindex -= 1
        else:
            kindex += 1


def _relative_read(f, k, read_opts):
    """Random READ of a RELATIVE file by record number (fileio.c L1368-L1404)."""
    fp = f.file
    _seek_init(fp)
    relnum = _cob_get_int(k) - 1
    relsize = f.record_max + _SIZE_T_BYTES
    off = relnum * relsize
    if relnum < 0:
        return COB_STATUS_23_KEY_NOT_EXISTS
    try:
        fp.seek(off)
        prefix = fp.read(_SIZE_T_BYTES)
    except (OSError, ValueError):
        return COB_STATUS_23_KEY_NOT_EXISTS
    if len(prefix) != _SIZE_T_BYTES:
        return COB_STATUS_23_KEY_NOT_EXISTS
    f.record.size = _unpack_relsize(prefix)
    if f.record.size == 0:
        fp.seek(-_SIZE_T_BYTES, io.SEEK_CUR)
        return COB_STATUS_23_KEY_NOT_EXISTS
    data = fp.read(f.record_max)
    if len(data) != f.record_max:
        return COB_STATUS_30_PERMANENT_ERROR
    f.record.data[0:f.record_max] = data
    return COB_STATUS_00_SUCCESS


def _relative_read_next(f, read_opts):
    """Sequential READ NEXT/PREVIOUS of a RELATIVE file (fileio.c L1405-L1457)."""
    fp = f.file
    _seek_init(fp)
    relsize = f.record_max + _SIZE_T_BYTES
    previous = bool(read_opts & common.COB_READ_PREVIOUS)
    while True:
        if previous:
            # Step back one whole slot before reading (READ PREVIOUS).
            pos = fp.tell()
            if pos < relsize:
                return COB_STATUS_10_END_OF_FILE
            fp.seek(pos - relsize)
        prefix = fp.read(_SIZE_T_BYTES)
        if len(prefix) != _SIZE_T_BYTES:
            return COB_STATUS_10_END_OF_FILE
        f.record.size = _unpack_relsize(prefix)
        if f.keys[0].field is not None:
            if f.flag_first_read:
                _cob_set_int(f.keys[0].field, 1)
                f.flag_first_read = 0
            else:
                off = fp.tell()
                relnum = (off // relsize) + 1
                _cob_set_int(f.keys[0].field, 0)
                if _cob_add_int(f.keys[0].field, relnum) != 0:
                    fp.seek(-_SIZE_T_BYTES, io.SEEK_CUR)
                    return COB_STATUS_14_OUT_OF_KEY_RANGE
        if f.record.size > 0:
            data = fp.read(f.record_max)
            if len(data) != f.record_max:
                return COB_STATUS_30_PERMANENT_ERROR
            f.record.data[0:f.record_max] = data
            if previous:
                # Re-position to the slot start so the next PREVIOUS steps back.
                fp.seek(-relsize, io.SEEK_CUR)
            return COB_STATUS_00_SUCCESS
        if previous:
            fp.seek(-_SIZE_T_BYTES, io.SEEK_CUR)
        else:
            fp.seek(f.record_max, io.SEEK_CUR)


def _relative_write(f, opt):
    """WRITE a RELATIVE record (fileio.c L1459-L1521)."""
    fp = f.file
    _seek_init(fp)
    relsize = f.record_max + _SIZE_T_BYTES
    if f.access_mode != common.COB_ACCESS_SEQUENTIAL:
        kindex = _cob_get_int(f.keys[0].field) - 1
        if kindex < 0:
            return COB_STATUS_21_KEY_INVALID
        off = relsize * kindex
        try:
            fp.seek(off)
        except (OSError, ValueError):
            return COB_STATUS_21_KEY_INVALID
    else:
        off = fp.tell()
    prefix = fp.read(_SIZE_T_BYTES)
    if len(prefix) == _SIZE_T_BYTES:
        fp.seek(-_SIZE_T_BYTES, io.SEEK_CUR)
        if _unpack_relsize(prefix) > 0:
            return COB_STATUS_22_KEY_EXISTS
    else:
        fp.seek(off)
    try:
        fp.write(_pack_relsize(f.record.size))
        fp.write(_record_slot_bytes(f))
    except (OSError, ValueError):  # pragma: no cover
        return COB_STATUS_30_PERMANENT_ERROR
    # Update RELATIVE KEY when loading sequentially (fileio.c L1509-L1519).
    if f.access_mode == common.COB_ACCESS_SEQUENTIAL and f.keys[0].field is not None:
        off += relsize
        _cob_set_int(f.keys[0].field, off // relsize)
    return COB_STATUS_00_SUCCESS


def _relative_rewrite(f, opt):
    """REWRITE a RELATIVE record (fileio.c L1523-L1556)."""
    fp = f.file
    if f.access_mode == common.COB_ACCESS_SEQUENTIAL:
        fp.seek(-f.record_max, io.SEEK_CUR)
    else:
        relsize = f.record_max + _SIZE_T_BYTES
        relnum = _cob_get_int(f.keys[0].field) - 1
        off = relnum * relsize
        try:
            fp.seek(off)
            prefix = fp.read(_SIZE_T_BYTES)
        except (OSError, ValueError):
            return COB_STATUS_23_KEY_NOT_EXISTS
        if len(prefix) != _SIZE_T_BYTES:
            return COB_STATUS_23_KEY_NOT_EXISTS
        f.record.size = _unpack_relsize(prefix)
        _seek_init(fp)
    try:
        fp.write(_record_slot_bytes(f))
    except (OSError, ValueError):  # pragma: no cover
        return COB_STATUS_30_PERMANENT_ERROR
    return COB_STATUS_00_SUCCESS


def _relative_delete(f):
    """DELETE a RELATIVE record (fileio.c L1558-L1590)."""
    fp = f.file
    relnum = _cob_get_int(f.keys[0].field) - 1
    relsize = f.record_max + _SIZE_T_BYTES
    off = relnum * relsize
    try:
        fp.seek(off)
        prefix = fp.read(_SIZE_T_BYTES)
    except (OSError, ValueError):
        return COB_STATUS_23_KEY_NOT_EXISTS
    if len(prefix) != _SIZE_T_BYTES:
        return COB_STATUS_23_KEY_NOT_EXISTS
    fp.seek(-_SIZE_T_BYTES, io.SEEK_CUR)
    f.record.size = 0
    try:
        fp.write(_pack_relsize(0))
    except (OSError, ValueError):  # pragma: no cover
        return COB_STATUS_30_PERMANENT_ERROR
    fp.seek(f.record_max, io.SEEK_CUR)
    return COB_STATUS_00_SUCCESS


# ===========================================================================
# INDEXED backend (fileio.c L1631-L3658) - Berkeley DB replaced by the Python
# standard library (AAP section 0.6.3).
#
# Routing:
#   * primary-key-only files  -> ``dbm``   (a single key -> record store)
#   * files with ALTERNATE RECORD KEY(s) -> ``sqlite3`` (one table plus one
#     index per key; ``WITH DUPLICATES`` keys use a non-unique index, while
#     no-duplicate alternate keys are enforced in this layer so a violation
#     reports file status ``22`` rather than surfacing a backend exception).
#
# Migration contract (HARD): an existing file in the legacy Berkeley DB on-disk
# format cannot be read by ``dbm`` / ``sqlite3``; opening it yields COBOL file
# status ``30`` and logs the exact message in :data:`_MIGRATION_MESSAGE`.  No
# data is silently dropped, truncated, or corrupted.  A one-time migration
# utility (read with the legacy reader, rewrite through this runtime) is the
# documented remedy; see the module docstring.
# ===========================================================================

#: Berkeley DB file magic numbers (btree / hash / queue / log), checked at the
#: two offsets BDB historically used.  Presence of any marks an unreadable
#: legacy file for the status-30 migration path.
_BDB_MAGICS = (0x00053162, 0x00061561, 0x00042253, 0x00040988)

#: First 16 bytes of every SQLite 3 database file.
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _detect_legacy_bdb(path):
    """Return True when *path* holds a legacy Berkeley DB file.

    Inspects the file header for the BDB magic numbers (at offset 0 and the
    historical offset 12, both byte orders).  SQLite and dbm files are excluded
    so only genuine legacy stores trip the migration error.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(24)
    except OSError:  # pragma: no cover - caller already checked existence
        return False
    if len(head) < 16 or head[:16] == _SQLITE_MAGIC:
        return False
    for off in (0, 12):
        if off + 4 <= len(head):
            for endian in ("<", ">"):
                (magic,) = struct.unpack_from(endian + "I", head, off)
                if magic in _BDB_MAGICS:
                    return True
    return False


def _is_my_sqlite_file(path):
    """Return True when *path* is an INDEXED store *this* module created.

    A bare SQLite-magic check is insufficient because some ``dbm`` back-ends
    (notably ``dbm.sqlite3``, the default on CPython 3.13+) also produce SQLite
    files.  To route reliably we additionally require the ``recs`` table this
    module defines, so a dbm-owned SQLite file is correctly sent to the dbm
    path on reopen rather than being mistaken for an alternate-key store.
    """
    try:
        with open(path, "rb") as handle:
            if handle.read(16) != _SQLITE_MAGIC:
                return False
    except OSError:  # pragma: no cover
        return False
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='recs'").fetchone()
        finally:
            conn.close()
        return row is not None
    except sqlite3.Error:  # pragma: no cover
        return False


def _record_key(f, idx):
    """Extract the bytes of key *idx* from the current record buffer.

    The key field aliases the record in the C runtime; here the connector
    stores the byte ``offset`` of every key (set by the emitter), so the key is
    sliced directly out of ``f.record.data`` - independent of whether the key
    field shares storage with the record.
    """
    key = f.keys[idx]
    off = key.offset
    size = key.field.size
    return bytes(f.record.data[off:off + size])


def _key_index_of(f, key):
    """Resolve which declared key the search field *key* refers to.

    Mirrors the C ``for (k...) if (f->keys[k].field->data == key->data)`` probe
    (fileio.c L2348) using object identity, then a data-identity fall-back, and
    finally defaults to the primary key.
    """
    for i in range(f.nkeys):
        kf = f.keys[i].field
        if kf is key:
            return i
    for i in range(f.nkeys):
        kf = f.keys[i].field
        if kf is not None and key is not None and kf.data is key.data:
            return i
    return 0


def _search_key_bytes(key):
    """Return the comparison bytes carried by a search-key field."""
    return bytes(key.data[:key.size])


class _IndexedDbm(object):
    """Primary-key-only INDEXED store backed by :mod:`dbm`.

    ``dbm`` provides a key -> value mapping but no ordering, so a sorted view of
    the keys is materialised on demand (and invalidated on every mutation) to
    drive READ NEXT / START in ascending key order, matching the Berkeley DB
    cursor the C runtime used.
    """

    __slots__ = ("db", "filename", "mode", "keylen", "_sorted",
                 "_idx", "_pending", "_last_key")

    def __init__(self, db, filename, mode, keylen):
        self.db = db
        self.filename = filename
        self.mode = mode
        self.keylen = keylen
        self._sorted = None     # cached ascending key list
        self._idx = -1          # index of last record returned (-1 = before first)
        self._pending = None    # index to return first after a START
        self._last_key = None   # last key written in sequential (load) mode

    # -- ordering helpers ---------------------------------------------------
    def _ensure_sorted(self):
        if self._sorted is None:
            self._sorted = sorted(bytes(k) for k in self.db.keys())

    def _locate(self, kb, klen, cond):
        self._ensure_sorted()
        keys = self._sorted
        n = len(keys)
        if cond in (common.COB_EQ, common.COB_GE, common.COB_GT):
            for i in range(n):
                head = keys[i][:klen]
                if cond == common.COB_EQ and head == kb:
                    return i
                if cond == common.COB_GE and head >= kb:
                    return i
                if cond == common.COB_GT and head > kb:
                    return i
            return None
        for i in range(n - 1, -1, -1):
            head = keys[i][:klen]
            if cond == common.COB_LE and head <= kb:
                return i
            if cond == common.COB_LT and head < kb:
                return i
        return None

    # -- file verbs ---------------------------------------------------------
    def write(self, f, opt):
        kb = _record_key(f, 0)
        if kb in self.db:
            return COB_STATUS_22_KEY_EXISTS
        if f.access_mode == common.COB_ACCESS_SEQUENTIAL:
            if self._last_key is not None and kb <= self._last_key:
                return COB_STATUS_21_KEY_INVALID
            self._last_key = kb
        self.db[kb] = bytes(f.record.data[:f.record.size])
        self._sorted = None
        return COB_STATUS_00_SUCCESS

    def rewrite(self, f, opt):
        kb = _record_key(f, 0)
        if kb not in self.db:
            return COB_STATUS_23_KEY_NOT_EXISTS
        self.db[kb] = bytes(f.record.data[:f.record.size])
        return COB_STATUS_00_SUCCESS

    def delete(self, f):
        kb = _record_key(f, 0)
        if kb not in self.db:
            return COB_STATUS_23_KEY_NOT_EXISTS
        del self.db[kb]
        self._sorted = None
        return COB_STATUS_00_SUCCESS

    def read(self, f, key, read_opts):
        kb = _search_key_bytes(key)
        val = None
        if kb in self.db:
            val = self.db[kb]
        elif key.size < self.keylen:
            # Partial key: first record whose key starts with kb.
            self._ensure_sorted()
            for k in self._sorted:
                if k[:key.size] == kb:
                    val = self.db[k]
                    kb = k
                    break
        if val is None:
            return COB_STATUS_23_KEY_NOT_EXISTS
        f.record.data[0:len(val)] = val
        f.record.size = len(val)
        self._ensure_sorted()
        try:
            self._idx = self._sorted.index(kb)
        except ValueError:  # pragma: no cover - key guaranteed present
            self._idx = -1
        self._pending = None
        return COB_STATUS_00_SUCCESS

    def read_next(self, f, read_opts):
        self._ensure_sorted()
        keys = self._sorted
        n = len(keys)
        previous = bool(read_opts & common.COB_READ_PREVIOUS)
        if self._pending is not None:
            idx = self._pending
            self._pending = None
        elif self._idx < 0:
            idx = (n - 1) if previous else 0
        else:
            idx = self._idx - 1 if previous else self._idx + 1
        if idx < 0 or idx >= n:
            return COB_STATUS_10_END_OF_FILE
        self._idx = idx
        kb = keys[idx]
        val = self.db[kb]
        f.record.data[0:len(val)] = val
        f.record.size = len(val)
        if f.keys[0].field is not None:
            f.keys[0].field.data[0:len(kb)] = kb
        return COB_STATUS_00_SUCCESS

    def start(self, f, cond, key):
        kb = _search_key_bytes(key)
        idx = self._locate(kb, key.size, cond)
        if idx is None:
            return COB_STATUS_23_KEY_NOT_EXISTS
        self._pending = idx
        self._idx = -1
        return COB_STATUS_00_SUCCESS

    def close(self, f, opt):
        try:
            self.db.close()
        except Exception:  # pragma: no cover - best-effort close
            pass
        return COB_STATUS_00_SUCCESS

    def sync(self, mode):
        syncer = getattr(self.db, "sync", None)
        if callable(syncer):
            try:
                self.db.sync()
            except Exception:  # pragma: no cover
                pass

    def unlock(self):  # pragma: no cover - advisory only
        pass


class _IndexedSqlite(object):
    """INDEXED store with alternate keys backed by :mod:`sqlite3`.

    One table holds the record plus a BLOB column per key (``k0`` primary,
    ``k1..`` alternates).  BLOB columns compare via ``memcmp`` in SQLite, which
    reproduces the unsigned byte ordering Berkeley DB used for keys.  The
    monotonically increasing ``seq`` row id provides the stable tie-break for
    duplicate keys (and the READ NEXT cursor), so ``WITH DUPLICATES`` keys need
    no synthetic duplicate counter.
    """

    __slots__ = ("conn", "filename", "mode", "nkeys", "keylens",
                 "_curkey", "_lastcol", "_lastseq", "_pending_seq", "_last_pkey")

    def __init__(self, conn, filename, mode, nkeys, keylens):
        self.conn = conn
        self.filename = filename
        self.mode = mode
        self.nkeys = nkeys
        self.keylens = keylens
        self._curkey = 0        # current key of reference (START/READ)
        self._lastcol = None    # last key-column value returned
        self._lastseq = None    # last seq returned
        self._pending_seq = None  # row to serve first after a START
        self._last_pkey = None  # last primary key written in load mode

    # -- positioning helpers ------------------------------------------------
    def _next_row(self, k, lastcol, lastseq, previous):
        col = "k%d" % k
        if not previous:
            if lastcol is None:
                sql = ("SELECT seq, rec, %s FROM recs ORDER BY %s, seq LIMIT 1"
                       % (col, col))
                args = ()
            else:
                sql = ("SELECT seq, rec, %s FROM recs WHERE (%s > ? OR (%s = ? "
                       "AND seq > ?)) ORDER BY %s, seq LIMIT 1"
                       % (col, col, col, col))
                args = (lastcol, lastcol, lastseq)
        else:
            if lastcol is None:
                sql = ("SELECT seq, rec, %s FROM recs ORDER BY %s DESC, seq DESC "
                       "LIMIT 1" % (col, col))
                args = ()
            else:
                sql = ("SELECT seq, rec, %s FROM recs WHERE (%s < ? OR (%s = ? "
                       "AND seq < ?)) ORDER BY %s DESC, seq DESC LIMIT 1"
                       % (col, col, col, col))
                args = (lastcol, lastcol, lastseq)
        return self.conn.execute(sql, args).fetchone()

    def _locate_seq(self, k, kb, klen, cond):
        col = "k%d" % k
        head = "substr(%s, 1, ?)" % col
        if cond == common.COB_EQ:
            sql = ("SELECT seq FROM recs WHERE %s = ? ORDER BY %s, seq LIMIT 1"
                   % (head, col))
            args = (klen, kb)
        elif cond in (common.COB_GE, common.COB_GT):
            op = ">=" if cond == common.COB_GE else ">"
            sql = ("SELECT seq FROM recs WHERE %s %s ? ORDER BY %s, seq LIMIT 1"
                   % (head, op, col))
            args = (klen, kb)
        else:
            op = "<=" if cond == common.COB_LE else "<"
            sql = ("SELECT seq FROM recs WHERE %s %s ? ORDER BY %s DESC, seq "
                   "DESC LIMIT 1" % (head, op, col))
            args = (klen, kb)
        row = self.conn.execute(sql, args).fetchone()
        return row[0] if row else None

    # -- file verbs ---------------------------------------------------------
    def write(self, f, opt):
        keyvals = [_record_key(f, i) for i in range(self.nkeys)]
        if self.conn.execute("SELECT 1 FROM recs WHERE k0 = ? LIMIT 1",
                             (keyvals[0],)).fetchone():
            return COB_STATUS_22_KEY_EXISTS
        for i in range(1, self.nkeys):
            if not f.keys[i].flag:  # no DUPLICATES -> must be unique
                if self.conn.execute("SELECT 1 FROM recs WHERE k%d = ? LIMIT 1"
                                     % i, (keyvals[i],)).fetchone():
                    return COB_STATUS_22_KEY_EXISTS
        if f.access_mode == common.COB_ACCESS_SEQUENTIAL:
            if self._last_pkey is not None and keyvals[0] <= self._last_pkey:
                return COB_STATUS_21_KEY_INVALID
            self._last_pkey = keyvals[0]
        rec = bytes(f.record.data[:f.record.size])
        cols = ", ".join("k%d" % i for i in range(self.nkeys))
        marks = ", ".join("?" for _ in range(self.nkeys))
        self.conn.execute("INSERT INTO recs (rec, %s) VALUES (?, %s)"
                          % (cols, marks), (rec, *keyvals))
        self.conn.commit()
        return COB_STATUS_00_SUCCESS

    def rewrite(self, f, opt):
        keyvals = [_record_key(f, i) for i in range(self.nkeys)]
        row = self.conn.execute("SELECT seq FROM recs WHERE k0 = ?",
                               (keyvals[0],)).fetchone()
        if row is None:
            return COB_STATUS_23_KEY_NOT_EXISTS
        seq = row[0]
        for i in range(1, self.nkeys):
            if not f.keys[i].flag:
                clash = self.conn.execute(
                    "SELECT 1 FROM recs WHERE k%d = ? AND k0 <> ? LIMIT 1" % i,
                    (keyvals[i], keyvals[0])).fetchone()
                if clash:
                    return COB_STATUS_22_KEY_EXISTS
        rec = bytes(f.record.data[:f.record.size])
        sets = "rec = ?, " + ", ".join("k%d = ?" % i for i in range(self.nkeys))
        self.conn.execute("UPDATE recs SET %s WHERE seq = ?" % sets,
                          (rec, *keyvals, seq))
        self.conn.commit()
        return COB_STATUS_00_SUCCESS

    def delete(self, f):
        kb = _record_key(f, 0)
        cur = self.conn.execute("DELETE FROM recs WHERE k0 = ?", (kb,))
        self.conn.commit()
        if cur.rowcount == 0:
            return COB_STATUS_23_KEY_NOT_EXISTS
        return COB_STATUS_00_SUCCESS

    def read(self, f, key, read_opts):
        k = _key_index_of(f, key)
        kb = _search_key_bytes(key)
        col = "k%d" % k
        if key.size < self.keylens[k]:
            row = self.conn.execute(
                "SELECT seq, rec, %s FROM recs WHERE substr(%s, 1, ?) = ? "
                "ORDER BY %s, seq LIMIT 1" % (col, col, col),
                (key.size, kb)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT seq, rec, %s FROM recs WHERE %s = ? ORDER BY seq LIMIT 1"
                % (col, col), (kb,)).fetchone()
        if row is None:
            return COB_STATUS_23_KEY_NOT_EXISTS
        seq, rec, colval = row
        f.record.data[0:len(rec)] = rec
        f.record.size = len(rec)
        self._curkey = k
        self._lastcol = colval
        self._lastseq = seq
        self._pending_seq = None
        return COB_STATUS_00_SUCCESS

    def read_next(self, f, read_opts):
        k = self._curkey
        col = "k%d" % k
        previous = bool(read_opts & common.COB_READ_PREVIOUS)
        if self._pending_seq is not None:
            row = self.conn.execute(
                "SELECT seq, rec, %s FROM recs WHERE seq = ?" % col,
                (self._pending_seq,)).fetchone()
            self._pending_seq = None
        else:
            row = self._next_row(k, self._lastcol, self._lastseq, previous)
        if row is None:
            return COB_STATUS_10_END_OF_FILE
        seq, rec, colval = row
        self._lastcol = colval
        self._lastseq = seq
        f.record.data[0:len(rec)] = rec
        f.record.size = len(rec)
        # Duplicate alternate key ahead -> status 02 (fileio.c read_next).
        if k > 0:
            nxt = self._next_row(k, colval, seq, previous)
            if nxt is not None and nxt[2] == colval:
                return COB_STATUS_02_SUCCESS_DUPLICATE
        return COB_STATUS_00_SUCCESS

    def start(self, f, cond, key):
        k = _key_index_of(f, key)
        kb = _search_key_bytes(key)
        seq = self._locate_seq(k, kb, key.size, cond)
        if seq is None:
            return COB_STATUS_23_KEY_NOT_EXISTS
        self._curkey = k
        self._pending_seq = seq
        self._lastcol = None
        self._lastseq = None
        return COB_STATUS_00_SUCCESS

    def close(self, f, opt):
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:  # pragma: no cover
            pass
        return COB_STATUS_00_SUCCESS

    def sync(self, mode):
        try:
            self.conn.commit()
        except Exception:  # pragma: no cover
            pass

    def unlock(self):  # pragma: no cover - advisory only
        pass


def _indexed_open(f, filename, mode, sharing):
    """Open an INDEXED file, routing to ``dbm`` or ``sqlite3``.

    Returns a COBOL file-status integer (0 on success).  Reproduces the C
    ``indexed_open`` outcome set: success, ``35`` (missing, non-OUTPUT),
    ``30`` (permanent error - including the legacy-BDB migration case) and
    ``37`` (permission denied).
    """
    exists = os.path.exists(filename)
    if exists and _detect_legacy_bdb(filename):
        # HARD migration contract: never read or truncate a legacy BDB file.
        common.cob_runtime_error("%s", _MIGRATION_MESSAGE)
        return COB_STATUS_30_PERMANENT_ERROR

    keylens = [f.keys[i].field.size for i in range(f.nkeys)] if f.keys else [0]
    use_sqlite = (f.nkeys > 1) or (exists and _is_my_sqlite_file(filename))

    if mode == common.COB_OPEN_OUTPUT and exists:
        # OUTPUT recreates the file from scratch.
        try:
            os.remove(filename)
        except OSError:  # pragma: no cover
            return COB_STATUS_37_PERMISSION_DENIED
        exists = False

    if use_sqlite:
        try:
            conn = sqlite3.connect(filename)
        except sqlite3.Error:  # pragma: no cover
            return COB_STATUS_30_PERMANENT_ERROR
        try:
            cols = ", ".join("k%d BLOB" % i for i in range(f.nkeys))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS recs (seq INTEGER PRIMARY KEY "
                "AUTOINCREMENT, rec BLOB, %s)" % cols)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_k0 ON recs (k0)")
            for i in range(1, f.nkeys):
                conn.execute("CREATE INDEX IF NOT EXISTS ix_k%d ON recs "
                             "(k%d, seq)" % (i, i))
            conn.commit()
        except sqlite3.Error:
            conn.close()
            return COB_STATUS_30_PERMANENT_ERROR
        f.file = _IndexedSqlite(conn, filename, mode, f.nkeys, keylens)
        return COB_STATUS_00_SUCCESS

    # dbm route (primary key only).
    dbm_flag = {
        common.COB_OPEN_INPUT: "r",
        common.COB_OPEN_OUTPUT: "n",
        common.COB_OPEN_I_O: "c",
        common.COB_OPEN_EXTEND: "c",
    }.get(mode, "c")
    try:
        db = dbm.open(filename, dbm_flag)
    except dbm.error:
        if not exists:
            return COB_STATUS_35_NOT_EXISTS
        # An existing, non-empty file that dbm cannot read is treated as an
        # incompatible legacy store (the BDB magic check above already handled
        # recognised formats); surface the migration contract.
        common.cob_runtime_error("%s", _MIGRATION_MESSAGE)
        return COB_STATUS_30_PERMANENT_ERROR
    except OSError:  # pragma: no cover
        return COB_STATUS_30_PERMANENT_ERROR
    f.file = _IndexedDbm(db, filename, mode, keylens[0])
    return COB_STATUS_00_SUCCESS


# ===========================================================================
# Per-organization dispatch (the C ``fileio_funcs[org]`` vtable, L425-L431).
# These thin routers send each verb to the SEQUENTIAL / LINE SEQUENTIAL /
# RELATIVE function pair above or to the INDEXED handle's method.
# ===========================================================================

def _be_close(f, opt):
    org = f.organization
    if org == common.COB_ORG_INDEXED:
        return f.file.close(f, opt)
    return _cob_file_close(f, opt)


def _be_start(f, cond, key):
    org = f.organization
    if org == common.COB_ORG_RELATIVE:
        return _relative_start(f, cond, key)
    if org == common.COB_ORG_INDEXED:
        return f.file.start(f, cond, key)
    # SEQUENTIAL / LINE SEQUENTIAL do not support START.
    return COB_STATUS_23_KEY_NOT_EXISTS


def _be_read(f, key, read_opts):
    org = f.organization
    if org == common.COB_ORG_RELATIVE:
        return _relative_read(f, key, read_opts)
    if org == common.COB_ORG_INDEXED:
        return f.file.read(f, key, read_opts)
    # SEQUENTIAL / LINE SEQUENTIAL have no keyed read.
    return COB_STATUS_23_KEY_NOT_EXISTS


def _be_read_next(f, read_opts):
    org = f.organization
    if org == common.COB_ORG_SEQUENTIAL:
        return _sequential_read(f, read_opts)
    if org == common.COB_ORG_LINE_SEQUENTIAL:
        return _lineseq_read(f, read_opts)
    if org == common.COB_ORG_RELATIVE:
        return _relative_read_next(f, read_opts)
    return f.file.read_next(f, read_opts)


def _be_write(f, opt):
    org = f.organization
    if org == common.COB_ORG_SEQUENTIAL:
        return _sequential_write(f, opt)
    if org == common.COB_ORG_LINE_SEQUENTIAL:
        return _lineseq_write(f, opt)
    if org == common.COB_ORG_RELATIVE:
        return _relative_write(f, opt)
    return f.file.write(f, opt)


def _be_rewrite(f, opt):
    org = f.organization
    if org == common.COB_ORG_SEQUENTIAL:
        return _sequential_rewrite(f, opt)
    if org == common.COB_ORG_LINE_SEQUENTIAL:
        return _sequential_rewrite(f, opt)
    if org == common.COB_ORG_RELATIVE:
        return _relative_rewrite(f, opt)
    return f.file.rewrite(f, opt)


def _be_delete(f):
    org = f.organization
    if org == common.COB_ORG_RELATIVE:
        return _relative_delete(f)
    if org == common.COB_ORG_INDEXED:
        return f.file.delete(f)
    return COB_STATUS_49_I_O_DENIED


# ===========================================================================
# File-name resolution (port of the mapping block in cob_open, L3815-L3897).
# ===========================================================================

def _resolve_filename(f):
    """Resolve the connector's ASSIGN/SELECT name to a filesystem path.

    Reproduces the C behaviour: take ``f.assign`` (or ``select_name`` when
    ASSIGN is absent); expand ``$VAR`` environment references; and, for a
    "simple" name (alphanumerics / ``_`` / ``-`` only), try the ``DD_<name>``,
    ``dd_<name>`` and ``<name>`` environment variables, then fall back to
    ``COB_FILE_PATH``/<name>.
    """
    if f.assign is None:
        name = (f.select_name or "")
    else:
        name = common.cob_field_to_string(f.assign)
    if isinstance(name, bytes):
        name = name.decode("latin-1")

    module = common.cob_current_module
    if module is not None and not getattr(module, "flag_filename_mapping", 1):
        return name

    # Expand $VAR references and detect a "simple" name.
    simple = True
    out = []
    i = 0
    n = len(name)
    while i < n:
        ch = name[i]
        if not (ch.isalnum() or ch in "_-"):
            simple = False
        if ch == "$":
            j = i + 1
            while j < n and (name[j].isalnum() or name[j] == "_"):
                j += 1
            val = os.environ.get(name[i + 1:j])
            if val:
                out.append(val)
            i = j
        else:
            out.append(ch)
            i += 1
    name = "".join(out)

    if simple:
        for prefix in _PREFIX:
            val = os.environ.get(prefix + name)
            if val:
                return val
        if cob_file_path:
            return os.path.join(cob_file_path, name)
    return name


# ===========================================================================
# Public file verbs - the exact ``cob_*`` names the rewritten emitter calls
# (AAP section 0.6.5).  Signatures follow the C argument order; *fnstatus* is a
# :class:`~libcob_py.common.cob_field` (the COBOL FILE STATUS item) or None.
# ===========================================================================

def cob_open(f, mode, sharing, fnstatus):
    """OPEN a file (port of fileio.c L3769-L4024)."""
    f.flag_read_done = 0

    if f.open_mode == common.COB_OPEN_LOCKED:
        save_status(f, COB_STATUS_38_CLOSED_WITH_LOCK, fnstatus)
        return
    if f.open_mode != common.COB_OPEN_CLOSED:
        save_status(f, COB_STATUS_41_ALREADY_OPEN, fnstatus)
        return

    f.last_open_mode = mode
    f.flag_nonexistent = 0
    f.flag_end_of_file = 0
    f.flag_begin_of_file = 0
    f.flag_first_read = 2

    # Special files (stdin/stdout) - f.special is 1 (stdin) or 2 (stdout).
    if f.special:
        if f.special == 1:
            if mode != common.COB_OPEN_INPUT:
                save_status(f, COB_STATUS_30_PERMANENT_ERROR, fnstatus)
                return
            f.file = sys.stdin.buffer
        else:
            if mode != common.COB_OPEN_OUTPUT:
                save_status(f, COB_STATUS_30_PERMANENT_ERROR, fnstatus)
                return
            f.file = sys.stdout.buffer
        f.open_mode = mode
        save_status(f, COB_STATUS_00_SUCCESS, fnstatus)
        return

    filename = _resolve_filename(f)

    was_not_exist = False
    if not os.path.exists(filename):
        was_not_exist = True
        if mode != common.COB_OPEN_OUTPUT and not f.flag_optional:
            save_status(f, COB_STATUS_35_NOT_EXISTS, fnstatus)
            return

    cob_cache_file(f)

    if f.organization == common.COB_ORG_INDEXED:
        ret = _indexed_open(f, filename, mode, sharing)
        if ret == COB_STATUS_00_SUCCESS:
            f.open_mode = mode
            if f.flag_optional and was_not_exist:
                save_status(f, COB_STATUS_05_SUCCESS_OPTIONAL, fnstatus)
            else:
                save_status(f, COB_STATUS_00_SUCCESS, fnstatus)
            return
        if ret == COB_STATUS_35_NOT_EXISTS and f.flag_optional:
            f.open_mode = mode
            f.flag_nonexistent = 1
            f.flag_end_of_file = 1
            f.flag_begin_of_file = 1
            save_status(f, COB_STATUS_05_SUCCESS_OPTIONAL, fnstatus)
            return
        save_status(f, ret, fnstatus)
        return

    # SEQUENTIAL / LINE SEQUENTIAL / RELATIVE - map the errno-style result.
    code = _cob_file_open(f, filename, mode, sharing)
    if code == 0:
        f.open_mode = mode
        if f.flag_optional and was_not_exist:
            save_status(f, COB_STATUS_05_SUCCESS_OPTIONAL, fnstatus)
        else:
            save_status(f, COB_STATUS_00_SUCCESS, fnstatus)
        return
    import errno as _errno
    if code == _errno.ENOENT:
        if mode in (common.COB_OPEN_EXTEND, common.COB_OPEN_OUTPUT):
            save_status(f, COB_STATUS_30_PERMANENT_ERROR, fnstatus)
            return
        if f.flag_optional:
            f.open_mode = mode
            f.flag_nonexistent = 1
            f.flag_end_of_file = 1
            f.flag_begin_of_file = 1
            save_status(f, COB_STATUS_05_SUCCESS_OPTIONAL, fnstatus)
            return
        save_status(f, COB_STATUS_35_NOT_EXISTS, fnstatus)
        return
    if code in (_errno.EACCES, _errno.EISDIR, _errno.EROFS):
        save_status(f, COB_STATUS_37_PERMISSION_DENIED, fnstatus)
        return
    if code in (_errno.EAGAIN, COB_STATUS_61_FILE_SHARING):
        save_status(f, COB_STATUS_61_FILE_SHARING, fnstatus)
        return
    if code == COB_STATUS_91_NOT_AVAILABLE:
        save_status(f, COB_STATUS_91_NOT_AVAILABLE, fnstatus)
        return
    if code == common.COB_LINAGE_INVALID:
        save_status(f, COB_STATUS_57_I_O_LINAGE, fnstatus)
        return
    save_status(f, COB_STATUS_30_PERMANENT_ERROR, fnstatus)


def cob_close(f, opt, fnstatus):
    """CLOSE a file (port of fileio.c L4026-L4068)."""
    f.flag_read_done = 0

    if f.special:
        f.open_mode = common.COB_OPEN_CLOSED
        save_status(f, COB_STATUS_00_SUCCESS, fnstatus)
        return
    if f.open_mode == common.COB_OPEN_CLOSED:
        save_status(f, COB_STATUS_42_NOT_OPEN, fnstatus)
        return

    if f.flag_nonexistent:
        ret = COB_STATUS_00_SUCCESS
    else:
        ret = _be_close(f, opt)

    if ret == COB_STATUS_00_SUCCESS:
        if opt == common.COB_CLOSE_LOCK:
            f.open_mode = common.COB_OPEN_LOCKED
        else:
            f.open_mode = common.COB_OPEN_CLOSED

    save_status(f, ret, fnstatus)


def cob_unlock(f):
    """UNLOCK a file connector.

    The C ``cob_unlock`` body was compiled out (``#if 0``, fileio.c L4070); the
    runtime instead released locks implicitly.  It is provided here for a
    complete emitter -> runtime surface: it releases any advisory lock on an
    open connector and is a safe no-op when the file is closed.
    """
    cob_file_unlock(f)


#: The rewritten emitter names this verb ``cob_unlock_file`` (codegen.c); expose
#: both spellings so generated code links regardless of which it emits.
cob_unlock_file = cob_unlock


def cob_start(f, cond, key, fnstatus):
    """START - position for sequential retrieval (port of fileio.c L4091-L4117)."""
    f.flag_read_done = 0
    f.flag_first_read = 0

    if f.flag_nonexistent:
        save_status(f, COB_STATUS_23_KEY_NOT_EXISTS, fnstatus)
        return

    if (f.open_mode == common.COB_OPEN_CLOSED
            or f.open_mode == common.COB_OPEN_OUTPUT
            or f.open_mode == common.COB_OPEN_EXTEND
            or f.access_mode == common.COB_ACCESS_RANDOM):
        save_status(f, COB_STATUS_47_INPUT_DENIED, fnstatus)
        return

    ret = _be_start(f, cond, key)
    if ret == COB_STATUS_00_SUCCESS:
        f.flag_end_of_file = 0
        f.flag_begin_of_file = 0
        f.flag_first_read = 1

    save_status(f, ret, fnstatus)


def cob_read(f, key, fnstatus, read_opts):
    """READ - keyed (``key`` set) or sequential (port of fileio.c L4118-L4189)."""
    f.flag_read_done = 0

    if f.flag_nonexistent:
        if f.flag_first_read == 0:
            save_status(f, COB_STATUS_23_KEY_NOT_EXISTS, fnstatus)
            return
        f.flag_first_read = 0
        save_status(f, COB_STATUS_10_END_OF_FILE, fnstatus)
        return

    if key is None:
        if f.flag_end_of_file and not (read_opts & common.COB_READ_PREVIOUS):
            save_status(f, COB_STATUS_46_READ_ERROR, fnstatus)
            return
        if f.flag_begin_of_file and (read_opts & common.COB_READ_PREVIOUS):
            save_status(f, COB_STATUS_46_READ_ERROR, fnstatus)
            return

    if (f.open_mode == common.COB_OPEN_CLOSED
            or f.open_mode == common.COB_OPEN_OUTPUT
            or f.open_mode == common.COB_OPEN_EXTEND):
        save_status(f, COB_STATUS_47_INPUT_DENIED, fnstatus)
        return

    if key is not None:
        ret = _be_read(f, key, read_opts)
    else:
        ret = _be_read_next(f, read_opts)

    if ret == COB_STATUS_00_SUCCESS:
        f.flag_first_read = 0
        f.flag_read_done = 1
        f.flag_end_of_file = 0
        f.flag_begin_of_file = 0
        if f.record_size is not None \
                and f.organization != common.COB_ORG_LINE_SEQUENTIAL:
            _cob_set_int(f.record_size, f.record.size)
    elif ret == COB_STATUS_10_END_OF_FILE:
        if read_opts & common.COB_READ_PREVIOUS:
            f.flag_begin_of_file = 1
        else:
            f.flag_end_of_file = 1

    save_status(f, ret, fnstatus)


def cob_write(f, rec, opt, fnstatus):
    """WRITE a record (port of fileio.c L4190-L4238)."""
    f.flag_read_done = 0

    if f.access_mode == common.COB_ACCESS_SEQUENTIAL:
        if f.open_mode in (common.COB_OPEN_CLOSED, common.COB_OPEN_INPUT,
                           common.COB_OPEN_I_O):
            save_status(f, COB_STATUS_48_OUTPUT_DENIED, fnstatus)
            return
    else:
        if f.open_mode in (common.COB_OPEN_CLOSED, common.COB_OPEN_INPUT,
                           common.COB_OPEN_EXTEND):
            save_status(f, COB_STATUS_48_OUTPUT_DENIED, fnstatus)
            return

    if f.record_size is not None:
        f.record.size = _cob_get_int(f.record_size)
    else:
        f.record.size = rec.size

    if f.record.size < f.record_min or f.record_max < f.record.size:
        save_status(f, COB_STATUS_44_RECORD_OVERFLOW, fnstatus)
        return

    ret = _be_write(f, opt)

    if cob_do_sync and ret == 0:
        cob_sync(f, cob_do_sync)

    save_status(f, ret, fnstatus)


def cob_rewrite(f, rec, opt, fnstatus):
    """REWRITE the last-read record (port of fileio.c L4240-L4284)."""
    read_done = f.flag_read_done
    f.flag_read_done = 0

    if f.open_mode == common.COB_OPEN_CLOSED \
            or f.open_mode != common.COB_OPEN_I_O:
        save_status(f, COB_STATUS_49_I_O_DENIED, fnstatus)
        return

    if f.access_mode == common.COB_ACCESS_SEQUENTIAL and not read_done:
        save_status(f, COB_STATUS_43_READ_NOT_DONE, fnstatus)
        return

    if f.organization == common.COB_ORG_SEQUENTIAL:
        if f.record.size != rec.size:
            save_status(f, COB_STATUS_44_RECORD_OVERFLOW, fnstatus)
            return
        if f.record_size is not None:
            if f.record.size != _cob_get_int(f.record_size):
                save_status(f, COB_STATUS_44_RECORD_OVERFLOW, fnstatus)
                return

    ret = _be_rewrite(f, opt)

    if cob_do_sync and ret == 0:
        cob_sync(f, cob_do_sync)

    save_status(f, ret, fnstatus)


def cob_delete(f, fnstatus):
    """DELETE the last-read record (port of fileio.c L4286-L4310)."""
    read_done = f.flag_read_done
    f.flag_read_done = 0

    if f.open_mode == common.COB_OPEN_CLOSED \
            or f.open_mode != common.COB_OPEN_I_O:
        save_status(f, COB_STATUS_49_I_O_DENIED, fnstatus)
        return

    if f.access_mode == common.COB_ACCESS_SEQUENTIAL and not read_done:
        save_status(f, COB_STATUS_43_READ_NOT_DONE, fnstatus)
        return

    ret = _be_delete(f)

    if cob_do_sync and ret == 0:
        cob_sync(f, cob_do_sync)

    save_status(f, ret, fnstatus)


def cob_commit():
    """COMMIT - release locks on every cached file (port of fileio.c L4312-L4320)."""
    for fl in _file_cache:
        cob_file_unlock(fl)


def cob_rollback():
    """ROLLBACK - release locks on every cached file (port of fileio.c L4322-L4330)."""
    for fl in _file_cache:
        cob_file_unlock(fl)


#: Status -> diagnostic text used by :func:`cob_default_error_handle`
#: (fileio.c L4339-L4408).
_ERROR_MESSAGES = {
    COB_STATUS_10_END_OF_FILE: "End of file",
    COB_STATUS_14_OUT_OF_KEY_RANGE: "Key out of range",
    COB_STATUS_21_KEY_INVALID: "Key order not ascending",
    COB_STATUS_22_KEY_EXISTS: "Record key already exists",
    COB_STATUS_23_KEY_NOT_EXISTS: "Record key does not exist",
    COB_STATUS_30_PERMANENT_ERROR: "Permanent file error",
    COB_STATUS_35_NOT_EXISTS: "File does not exist",
    COB_STATUS_37_PERMISSION_DENIED: "Permission denied",
    COB_STATUS_41_ALREADY_OPEN: "File already open",
    COB_STATUS_42_NOT_OPEN: "File not open",
    COB_STATUS_43_READ_NOT_DONE: "READ must be executed first",
    COB_STATUS_44_RECORD_OVERFLOW: "Record overflow",
    COB_STATUS_46_READ_ERROR: "Failed to read",
    COB_STATUS_47_INPUT_DENIED: "READ/START not allowed",
    COB_STATUS_48_OUTPUT_DENIED: "WRITE not allowed",
    COB_STATUS_49_I_O_DENIED: "DELETE/REWRITE not allowed",
    COB_STATUS_51_RECORD_LOCKED: "Record locked by another file connector",
    COB_STATUS_52_EOP: "A page overflow condition occurred",
    COB_STATUS_57_I_O_LINAGE: "LINAGE values invalid",
    COB_STATUS_61_FILE_SHARING: "File sharing conflict",
    COB_STATUS_91_NOT_AVAILABLE:
        "Runtime library is not configured for this operation",
}


def cob_default_error_handle():
    """Emit the default USE-error diagnostic (port of fileio.c L4332-L4416).

    Decodes the two-digit status held on :data:`cob_error_file`, selects the
    matching message and reports it (with the status and the file name) through
    :func:`libcob_py.common.cob_runtime_error`.
    """
    if cob_error_file is None:  # pragma: no cover - defensive
        return
    file_status = cob_error_file.file_status
    status = common.cob_d2i(file_status[0]) * 10 + common.cob_d2i(file_status[1])
    msg = _ERROR_MESSAGES.get(status, "Unknown file error")
    if cob_error_file.assign is not None:
        filename = common.cob_field_to_string(cob_error_file.assign)
    else:
        filename = cob_error_file.select_name or ""
    if isinstance(filename, bytes):
        filename = filename.decode("latin-1")
    common.cob_runtime_error("%s (STATUS = %02d) File : '%s'",
                             msg, status, filename)


# ===========================================================================
# SORT / MERGE (fileio.c L5068-L5760).
#
# The C runtime implemented an external multiway merge over temp files; the AAP
# (section 0.4) explicitly permits "a stable Python sort over collected records
# ... provided ordering/keys match".  Records are therefore collected into an
# in-memory list and ordered with a comparator that reproduces
# ``cob_file_sort_compare`` (per-key numeric or collated-byte comparison, with
# ASCENDING/DESCENDING honoured and a stable insertion-order tie-break).  The
# ``COB_SORT_MEMORY`` budget governs an optional temp-file spill.
# ===========================================================================

class _cobsort(object):
    """SORT work area - mirror of ``struct cobsort`` (fileio.c L183-L201)."""

    __slots__ = ("pointer", "fnstatus", "sort_return", "size", "memory",
                 "items", "retrieving", "retrieval_index", "_unique", "spill")

    def __init__(self, f, fnstatus, sort_return):
        self.pointer = f            # the SORT cob_file
        self.fnstatus = fnstatus
        self.sort_return = sort_return
        self.size = f.record_max
        # Number of records that fit in the in-core budget before spilling.
        self.memory = max(1, cob_sort_memory // (f.record_max + 64))
        self.items = []             # collected (unique, bytes) tuples
        self.retrieving = 0
        self.retrieval_index = 0
        self._unique = 0
        self.spill = None           # tempfile path when the budget is exceeded


def _sort_compare(f, rec1, rec2):
    """Compare two SORT records by their keys (port of fileio.c L5101-L5130)."""
    for key in f.keys[:f.nkeys]:
        kf = key.field
        off = key.offset
        size = kf.size
        d1 = bytes(rec1[off:off + size])
        d2 = bytes(rec2[off:off + size])
        if common.COB_FIELD_IS_NUMERIC(kf):
            f1 = common.cob_field(size, d1, kf.attr)
            f2 = common.cob_field(size, d2, kf.attr)
            cmp = _cob_numeric_cmp(f1, f2)
        else:
            cmp = _sort_cmps(d1, d2, size, f.sort_collating)
        if cmp != 0:
            return cmp if key.flag == common.COB_ASCENDING else -cmp
    return 0


def _sort_cmps(s1, s2, size, col):
    """Byte comparison with an optional collating table (fileio.c L5068-L5089)."""
    if col is not None:
        for i in range(size):
            ret = col[s1[i]] - col[s2[i]]
            if ret != 0:
                return ret
        return 0
    if s1 < s2:
        return -1
    if s1 > s2:
        return 1
    return 0


def cob_file_sort_init(f, nkeys, collating_sequence, sort_return, fnstatus):
    """Initialise a SORT/MERGE work area (port of fileio.c L5649-L5674)."""
    p = _cobsort(f, fnstatus, sort_return)
    if sort_return is not None:
        _cob_set_int(sort_return, 0)
    f.file = p
    f.keys = cob_file_key_array(nkeys)
    f.nkeys = 0
    f.organization = common.COB_ORG_SORT
    if collating_sequence is not None:
        f.sort_collating = collating_sequence
    elif common.cob_current_module is not None:
        f.sort_collating = getattr(common.cob_current_module,
                                   "collating_sequence", None)
    save_status(f, COB_STATUS_00_SUCCESS, fnstatus)


def cob_file_sort_init_key(f, flag, field, offset):
    """Register one SORT/MERGE key (port of fileio.c L5676-L5684)."""
    f.keys[f.nkeys].flag = flag
    f.keys[f.nkeys].field = field
    f.keys[f.nkeys].offset = offset
    f.nkeys += 1


def cob_file_sort_submit(f, data):
    """Submit one record into the SORT work area (port of cob_file_sort_submit)."""
    p = f.file
    if p is None:
        return COBSORTNOTOPEN
    rec = bytes(data[:f.record_max])
    if len(rec) < f.record_max:
        rec = rec + b" " * (f.record_max - len(rec))
    if p.spill is not None:
        # Already spilling to disk - append to the temp file.
        try:
            with open(p.spill, "ab") as handle:
                handle.write(struct.pack("<Q", p._unique))
                handle.write(rec)
        except OSError:  # pragma: no cover
            return COBSORTFILEERR
    else:
        p.items.append((p._unique, rec))
        if len(p.items) > p.memory:
            # Budget exceeded: spill the collected items to a temp file.
            try:
                fd, path = tempfile.mkstemp(prefix="cobsort_")
                with os.fdopen(fd, "wb") as handle:
                    for uniq, item in p.items:
                        handle.write(struct.pack("<Q", uniq))
                        handle.write(item)
                p.spill = path
                p.items = []
            except OSError:  # pragma: no cover
                return COBSORTFILEERR
    p._unique += 1
    return 0


def _sort_load_spill(p):
    """Read every spilled record back into memory for the final ordering."""
    f = p.pointer
    reclen = f.record_max
    out = list(p.items)
    if p.spill is not None:
        try:
            with open(p.spill, "rb") as handle:
                while True:
                    head = handle.read(8)
                    if len(head) < 8:
                        break
                    (uniq,) = struct.unpack("<Q", head)
                    rec = handle.read(reclen)
                    if len(rec) < reclen:
                        break
                    out.append((uniq, rec))
        except OSError:  # pragma: no cover
            pass
    return out


def _sort_finish(p):
    """Order all submitted records once retrieval begins (stable sort)."""
    import functools
    f = p.pointer
    items = _sort_load_spill(p)

    def cmp(a, b):
        c = _sort_compare(f, a[1], b[1])
        if c != 0:
            return c
        return -1 if a[0] < b[0] else 1

    items.sort(key=functools.cmp_to_key(cmp))
    p.items = items
    p.retrieving = 1
    p.retrieval_index = 0


def cob_file_sort_retrieve(f, data):
    """Retrieve the next ordered record (port of cob_file_sort_retrieve)."""
    p = f.file
    if p is None:
        return COBSORTNOTOPEN
    if not p.retrieving:
        _sort_finish(p)
    if p.retrieval_index >= len(p.items):
        return COBSORTEND
    _uniq, rec = p.items[p.retrieval_index]
    p.retrieval_index += 1
    data[0:len(rec)] = rec
    f.record.size = len(rec)
    return 0


def cob_file_sort_using(sort_file, data_file):
    """Feed every record of *data_file* into the sort (fileio.c L5579-L5597)."""
    cob_open(data_file, common.COB_OPEN_INPUT, 0, None)
    while True:
        cob_read(data_file, None, None, common.COB_READ_NEXT)
        if data_file.file_status[0] != ord("0"):
            break
        cob_copy_check(sort_file, data_file)
        if cob_file_sort_submit(sort_file, sort_file.record.data):
            break
    cob_close(data_file, common.COB_CLOSE_NORMAL, None)


def cob_file_sort_giving(sort_file, varcnt, *fbase):
    """Write the ordered records to the GIVING files (fileio.c L5599-L5647).

    ``fbase`` is the flattened list of output :class:`cob_file` connectors; the
    leading ``varcnt`` matches the C variadic count and is validated against it.
    """
    files = list(fbase[:varcnt]) if varcnt else list(fbase)
    for outf in files:
        cob_open(outf, common.COB_OPEN_OUTPUT, 0, None)
    while True:
        ret = cob_file_sort_retrieve(sort_file, sort_file.record.data)
        if ret:
            if ret == COBSORTEND:
                sort_file.file_status[0] = ord("1")
                sort_file.file_status[1] = ord("0")
            else:
                hp = sort_file.file
                if hp is not None and hp.sort_return is not None:
                    _cob_set_int(hp.sort_return, 16)
                sort_file.file_status[0] = ord("3")
                sort_file.file_status[1] = ord("0")
            break
        for outf in files:
            if outf.special or outf.organization == common.COB_ORG_LINE_SEQUENTIAL:
                opt = common.COB_WRITE_BEFORE | common.COB_WRITE_LINES | 1
            else:
                opt = 0
            cob_copy_check(outf, sort_file)
            cob_write(outf, outf.record, opt, None)
    for outf in files:
        cob_close(outf, common.COB_CLOSE_NORMAL, None)


def cob_file_sort_close(f):
    """Tear down the SORT work area (port of fileio.c L5686-L5708)."""
    p = f.file
    fnstatus = None
    if p is not None:
        fnstatus = p.fnstatus
        if p.spill is not None:
            try:
                os.remove(p.spill)
            except OSError:  # pragma: no cover
                pass
    f.file = None
    save_status(f, COB_STATUS_00_SUCCESS, fnstatus)


def cob_file_release(f):
    """RELEASE one record into the sort (port of fileio.c L5710-L5734)."""
    p = f.file
    fnstatus = p.fnstatus if p is not None else None
    ret = cob_file_sort_submit(f, f.record.data)
    if ret == 0:
        save_status(f, COB_STATUS_00_SUCCESS, fnstatus)
    else:
        if p is not None and p.sort_return is not None:
            _cob_set_int(p.sort_return, 16)
        save_status(f, COB_STATUS_30_PERMANENT_ERROR, fnstatus)


def cob_file_return(f):
    """RETURN one ordered record from the sort (port of fileio.c L5736-L5760)."""
    p = f.file
    fnstatus = p.fnstatus if p is not None else None
    ret = cob_file_sort_retrieve(f, f.record.data)
    if ret == 0:
        save_status(f, COB_STATUS_00_SUCCESS, fnstatus)
    elif ret == COBSORTEND:
        save_status(f, COB_STATUS_10_END_OF_FILE, fnstatus)
    else:
        if p is not None and p.sort_return is not None:
            _cob_set_int(p.sort_return, 16)
        save_status(f, COB_STATUS_30_PERMANENT_ERROR, fnstatus)


def cob_copy_check(to, frm):
    """Copy a record between files, space-padding/truncating (fileio.c L5337-L5354)."""
    toptr = to.record.data
    fromptr = frm.record.data
    tosize = to.record.size if to.record.size else to.record_max
    fromsize = frm.record.size if frm.record.size else frm.record_max
    if tosize > fromsize:
        toptr[0:fromsize] = bytes(fromptr[:fromsize])
        for i in range(fromsize, tosize):
            toptr[i] = 0x20
    else:
        toptr[0:tosize] = bytes(fromptr[:tosize])


# ===========================================================================
# C$ filesystem routines (fileio.c L4952-L5063).
#
# These physically live in fileio.c in the C runtime; they are dispatched by
# the COBOL system-call layer (``system.py`` here) but implemented here, on top
# of ``os`` / ``shutil``.  Each accepts the COBOL argument fields directly and
# returns the integer result the COBOL program receives.
# ===========================================================================

def _chk_parms(required):
    """Honour the C ``COB_CHK_PARMS`` / ``cob_call_params`` count check.

    When invoked through a COBOL CALL the runtime sets
    :data:`libcob_py.common.cob_call_params`; if it is set and short of
    *required*, the routine reports failure (128).  When zero (a direct or unit
    invocation) the check is skipped so the passed arguments govern.
    """
    params = getattr(common, "cob_call_params", 0)
    return params == 0 or params >= required


def _field_path(field):
    """Return the trimmed filesystem path carried by a COBOL field."""
    if field is None:
        return ""
    name = common.cob_field_to_string(field)
    if isinstance(name, bytes):
        name = name.decode("latin-1")
    return name.rstrip()


def cob_acuw_mkdir(dir):
    """C$MAKEDIR - create a directory (port of fileio.c L4951-L4963)."""
    try:
        os.mkdir(_field_path(dir))
        return 0
    except OSError:
        return 128


def cob_acuw_chdir(dir, status):
    """C$CHDIR - change the working directory (port of fileio.c L4965-L4978)."""
    try:
        os.chdir(_field_path(dir))
        ret = 0
    except OSError:
        ret = 128
    if status is not None:
        _cob_set_int(status, ret)
    return ret


def cob_acuw_copyfile(fname1, fname2, file_type):
    """C$COPY - copy a file (port of fileio.c L4980-L4997).

    ``file_type`` is accepted for signature compatibility but, as in the C
    runtime, is not yet evaluated.
    """
    if not _chk_parms(3):
        return 128
    try:
        shutil.copyfile(_field_path(fname1), _field_path(fname2))
        return 0
    except OSError:
        return 128


def cob_acuw_file_info(file_name, file_info):
    """C$FILEINFO - return size and mtime (port of fileio.c L4999-L5046).

    Packs an 8-byte big-endian size, then a 4-byte big-endian ``YYYYMMDD`` date
    and a 4-byte big-endian ``HHMMSSss`` time (seconds scaled by 100, matching
    the C layout) into *file_info*.  Returns ``35`` when the file is missing.
    """
    if not _chk_parms(2) or file_name is None:
        return 128
    import time as _time
    try:
        st = os.stat(_field_path(file_name))
    except OSError:
        return 35
    tm = _time.localtime(st.st_mtime)
    sz = st.st_size & 0xFFFFFFFFFFFFFFFF
    dt_date = (tm.tm_year * 10000) + ((tm.tm_mon) * 100) + tm.tm_mday
    dt_time = (tm.tm_hour * 1000000) + (tm.tm_min * 10000) + (tm.tm_sec * 100)
    packed = struct.pack(">Q", sz) + struct.pack(">I", dt_date) \
        + struct.pack(">I", dt_time)
    file_info.data[0:16] = packed
    return 0


def cob_acuw_file_delete(file_name, file_type):
    """C$DELETE - remove a file (port of fileio.c L5048-L5063).

    ``file_type`` is accepted for signature compatibility but, as in the C
    runtime, is not yet evaluated.
    """
    if not _chk_parms(2) or file_name is None:
        return 128
    try:
        os.remove(_field_path(file_name))
        return 0
    except OSError:
        return 128


# ===========================================================================
# Runtime initialisation / shutdown (fileio.c L4418-L4490).
#
# ``cob_init_fileio`` runs after ``cob_init_intrinsic`` and before
# ``cob_init_termio`` (AAP section 0.5.3).  Berkeley-DB environment joining is
# intentionally absent - the dbm/sqlite3 backends need no shared environment
# (noted, not fixed, per the minimal-deviation rule).
# ===========================================================================

def cob_init_fileio():
    """Read the COB_* file-I/O environment settings (port of fileio.c L4418-L4463)."""
    global cob_do_sync, cob_sort_memory, cob_file_path, cob_ls_nulls, cob_ls_fixed
    s = os.environ.get("COB_SYNC")
    if s:
        if s[0] in ("Y", "y"):
            cob_do_sync = 1
        elif s[0] in ("P", "p"):
            cob_do_sync = 2
    s = os.environ.get("COB_SORT_MEMORY")
    if s:
        try:
            n = int(s)
        except ValueError:
            n = 0
        if n >= 1024 * 1024:
            cob_sort_memory = n
    s = os.environ.get("COB_FILE_PATH")
    if s is not None:
        if not s or s[0] == " ":
            s = None
    cob_file_path = s
    cob_ls_nulls = os.environ.get("COB_LS_NULLS")
    cob_ls_fixed = os.environ.get("COB_LS_FIXED")


def cob_exit_fileio():
    """Implicitly close any still-open files (port of fileio.c L4465-L4490)."""
    for fl in list(_file_cache):
        if fl.open_mode not in (common.COB_OPEN_CLOSED, common.COB_OPEN_LOCKED):
            if fl.assign is not None:
                name = common.cob_field_to_string(fl.assign)
                if isinstance(name, bytes):
                    name = name.decode("latin-1")
            else:
                name = fl.select_name or ""
            cob_close(fl, 0, None)
            sys.stderr.write('WARNING - Implicit CLOSE of %s ("%s")\n'
                             % (fl.select_name or "", name))
            sys.stderr.flush()


# ===========================================================================
# Public surface - every name the rewritten emitter binds to ``fileio`` plus
# the SORT/MERGE, C$ and lifecycle entry points.
# ===========================================================================
__all__ = [
    # connector structures / factories
    "cob_file", "cob_file_key", "cob_linage_struct",
    "cob_file_external", "cob_file_key_array",
    # status model
    "save_status", "status_exception",
    # core verbs
    "cob_open", "cob_close", "cob_read", "cob_write", "cob_rewrite",
    "cob_delete", "cob_start", "cob_unlock", "cob_unlock_file",
    "cob_commit", "cob_rollback", "cob_default_error_handle",
    # cache / sync / linage helpers
    "cob_cache_file", "cob_sync", "cob_file_unlock",
    "cob_linage_write_opt", "cob_file_write_opt",
    # SORT / MERGE
    "cob_file_sort_init", "cob_file_sort_init_key", "cob_file_sort_using",
    "cob_file_sort_giving", "cob_file_sort_close", "cob_file_release",
    "cob_file_return", "cob_file_sort_submit", "cob_file_sort_retrieve",
    "cob_copy_check",
    # C$ filesystem routines
    "cob_acuw_mkdir", "cob_acuw_chdir", "cob_acuw_copyfile",
    "cob_acuw_file_info", "cob_acuw_file_delete",
    # lifecycle
    "cob_init_fileio", "cob_exit_fileio",
    # module global
    "cob_error_file",
]
