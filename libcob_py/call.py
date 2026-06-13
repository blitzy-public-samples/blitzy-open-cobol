"""``libcob_py.call`` - dynamic program loading / CALL / CANCEL runtime.

Pure-Python port of ``libcob/call.c``.  The C runtime resolved called programs
through a 131-bucket name hash plus ``lt_dlopen``/``lt_dlsym`` (libltdl over
``dlopen``/``LoadLibrary``); under the Python backend that whole machinery
collapses onto the standard library (AAP sections 0.3.2 and 0.5):

* :func:`importlib.import_module` + ``sys.path`` (seeded from
  ``COB_LIBRARY_PATH``) replace ``lt_dlopen`` and the resolve-path search.
* A program's entry symbol is the module attribute whose name is the
  *encoded* program-id (the exact ``cb_encode_program_id`` transformation the
  code generator applied when it emitted the ``.py`` module), so
  ``CALL "PROG"`` resolves to ``import PROG; PROG.PROG``.
* ``CANCEL`` evicts the module from ``sys.modules`` and calls
  :func:`importlib.invalidate_caches`, reproducing the C cancel/uncache path.
* ``COB_PRE_LOAD`` preloads modules at start-up and ``COB_LOAD_CASE``
  (``UPPER``/``LOWER``) folds resolved names, exactly as the C did.

Standard-library only (Rule R3 / AAP sections 0.5 and 0.7.1): the whole module
is built from ``importlib``, ``sys`` and ``os`` -- zero third-party packages.
``importlib`` supplies the dynamic loader, ``sys`` supplies ``sys.path`` and
``sys.modules`` (search path + CANCEL eviction) and ``os`` supplies the
environment reads (``COB_LIBRARY_PATH`` / ``COB_PRE_LOAD`` / ``COB_LOAD_CASE``)
and the OS path separator.
"""
import importlib
import ctypes
import os
import sys

from libcob_py import common

# ---------------------------------------------------------------------------
# Module-scope resolver state (mirrors the C file-scope statics).
# ---------------------------------------------------------------------------
# Ordered resolve paths (call.c ``resolve_path[]``); also pushed onto sys.path.
_resolve_paths = []
# name -> callable cache (call.c ``call_table`` / ``lookup``/``insert``).
#
# LIFECYCLE (REVIEW FIX, MAJOR #7): this cache is intentionally *unbounded* -
# no LRU/size eviction.  COBOL programs are *resident* once CALLed: a program's
# WORKING-STORAGE persists between CALLs and is only reset by an explicit
# CANCEL (ISO 1989 / the C ``call_table``, which likewise never auto-evicts).
# A size-bounded cache would silently drop a resident program's state and
# violate that contract, so residency is by design.  Entries leave the cache
# only on CANCEL (:func:`cobcancel`) or at STOP RUN/tidy (:func:`cob_exit_call`),
# both of which evict ``_call_cache`` and ``_cancel_handlers`` *in lockstep*.
_call_cache = {}
# name -> cancel callable (call.c ``struct call_hash.cancel``).  Kept strictly
# in step with ``_call_cache`` (the C runtime stored func + cancel in one
# ``struct call_hash`` entry); see the lockstep eviction in cobcancel /
# cob_exit_call (REVIEW FIX, MAJOR #7).
_cancel_handlers = {}
# Last resolve error message (call.c ``resolve_error``).
_resolve_error = None
# COB_LOAD_CASE: 0 = preserve, 1 = lower, 2 = upper (call.c ``name_convert``).
name_convert = 0
# Reusable scratch buffer backing :func:`cob_get_buff` (call.c statics
# ``call_buffer`` / ``call_lastsize``).  Grown on demand, never shrunk, so the
# same storage is handed back to repeated callers exactly like the C runtime.
_call_buffer = bytearray()

# Default built-in library path baked into the C build (defaults.h
# COB_LIBRARY_PATH); under Python the module search uses sys.path, so the
# built-in default is simply the current directory.
COB_LIBRARY_PATH = "."
# Generated modules are packaged as ``.pyz`` (cobc -m), but they are imported
# by module name through importlib; the extension mirrors COB_MODULE_EXT and is
# used only for COB_PRE_LOAD file probing.
COB_MODULE_EXT = "pyz"

# MIGRATION (C->Python): NATIVE C subprogram support.  A COBOL program may
# ``CALL`` a subprogram written in C (the test oracle's ``CALL "dump"`` helpers
# compile a ``dump.c`` into a shared object with ``cc -shared -fPIC``; this is a
# legitimate COBOL feature - calling an external native routine).  The original
# C backend resolved such a CALL through ``lt_dlopen``/``lt_dlsym``; the
# faithful Python-backend equivalent (stdlib only - ctypes is part of the
# standard library, NOT a third-party package, so AAP 0.5/0.7.1 still holds) is
# :mod:`ctypes`, which dlopen's the shared object and exposes its C entry point.
# ``_NATIVE_EXT`` is the platform native shared-object extension probed in
# addition to the COBOL ``.pyz`` module extension (Linux ``.so``; the test
# harness builds ``dump.${SHREXT}`` and we set SHREXT=so in tests/atlocal).
# A located candidate is treated as native ONLY when its first bytes are the
# ELF magic, so a Python ``.pyz`` zip archive is never mistaken for native code.
_NATIVE_EXT = "so"
# ELF magic - leading bytes of a Linux shared object (``\x7fELF``).
_ELF_MAGIC = b"\x7fELF"
# Read-past-end pad for an immutable BY CONTENT literal: the original C backend
# placed literals in zeroed static storage, so a callee that reads slightly
# past the declared length saw zero bytes (e.g. the ``dump`` helper that prints
# 4 bytes of a 3-byte X"000102" literal expects a trailing 00).  Copying the
# literal into a NUL-padded ctypes buffer reproduces that exactly.
_NATIVE_PAD = 8


# MIGRATION (C->Python): ctypes mirrors of the libcob C runtime structs
# ``cob_field_attr``/``cob_field``/``cob_file`` (libcob/common.h).  A native
# subprogram CALL'd USING a COBOL *file-name* (e.g. ``CALL "setfilename" USING
# TEST-FILE``) receives a ``cob_file *`` and dereferences members at fixed ABI
# offsets - notably ``f->assign->data`` (the ASSIGN/external-filename buffer).
# To let such a routine read AND write the COBOL file's storage exactly as the C
# backend allowed, we hand it a ctypes ``cob_file`` whose ``assign`` (and
# ``record``) point at ctypes ``cob_field``s whose ``data`` ALIASES the Python
# field's bytearray (via ``ctypes.from_buffer``), so the callee's memcpy/memset
# writes propagate straight back into the COBOL data item.  The field order,
# integer widths and pointer placement below were verified to reproduce the
# installed header's offsets byte-for-byte (cob_field 0/8/16, total 24;
# cob_file.assign at offset 16, total 120 on LP64).  These are stdlib ctypes
# Structures only - no third-party dependency, so AAP 0.5/0.7.1 still holds.
class _CtCobFieldAttr(ctypes.Structure):
    """ctypes mirror of C ``cob_field_attr`` (common.h)."""

    _fields_ = [
        ("type", ctypes.c_ubyte),       # field type
        ("digits", ctypes.c_ubyte),     # digit count
        ("scale", ctypes.c_byte),       # signed scale
        ("flags", ctypes.c_ubyte),      # field flags
        ("pic", ctypes.c_char_p),       # picture string pointer
    ]


class _CtCobField(ctypes.Structure):
    """ctypes mirror of C ``cob_field`` (common.h): {size, data, attr}."""

    _fields_ = [
        ("size", ctypes.c_size_t),                  # field size
        ("data", ctypes.c_void_p),                  # pointer to field storage
        ("attr", ctypes.POINTER(_CtCobFieldAttr)),  # pointer to attribute
    ]


class _CtCobFile(ctypes.Structure):
    """ctypes mirror of C ``cob_file`` (common.h).

    Only the leading pointer block, the ``size_t`` block and the trailing
    ``char`` flag block are needed to place ``assign`` (offset 16) and the other
    members a native file routine might read at the exact C offsets; the actual
    payloads we populate are ``assign`` and ``record`` (the two ``cob_field *``
    members backed by COBOL storage).  All other members are left NULL/zero -
    no in-scope native routine dereferences them.
    """

    _fields_ = [
        ("select_name", ctypes.c_char_p),           # name in SELECT
        ("file_status", ctypes.c_void_p),           # FILE STATUS buffer
        ("assign", ctypes.POINTER(_CtCobField)),    # ASSIGN TO  (offset 16)
        ("record", ctypes.POINTER(_CtCobField)),    # record area
        ("record_size", ctypes.c_void_p),           # record size field
        ("keys", ctypes.c_void_p),                  # key descriptors
        ("file", ctypes.c_void_p),                  # backend file pointer
        ("linorkeyptr", ctypes.c_void_p),           # LINAGE / split-key ptr
        ("sort_collating", ctypes.c_void_p),        # SORT collating seq
        ("extfh_ptr", ctypes.c_void_p),             # EXTFH usage
        ("record_min", ctypes.c_size_t),            # record min size
        ("record_max", ctypes.c_size_t),            # record max size
        ("nkeys", ctypes.c_size_t),                 # number of keys
        ("organization", ctypes.c_char),            # ORGANIZATION
        ("access_mode", ctypes.c_char),             # ACCESS MODE
        ("lock_mode", ctypes.c_char),               # LOCK MODE
        ("open_mode", ctypes.c_char),               # OPEN MODE
        ("flag_optional", ctypes.c_char),           # OPTIONAL
        ("last_open_mode", ctypes.c_char),          # last OPEN mode
        ("special", ctypes.c_char),                 # special file
        ("flag_nonexistent", ctypes.c_char),        # nonexistent file
        ("flag_end_of_file", ctypes.c_char),        # at EOF
        ("flag_begin_of_file", ctypes.c_char),      # at BOF
        ("flag_first_read", ctypes.c_char),         # first READ after OPEN
        ("flag_read_done", ctypes.c_char),          # last READ ok
        ("flag_select_features", ctypes.c_char),    # SELECT features
        ("flag_needs_nl", ctypes.c_char),           # LS needs NL at close
        ("flag_needs_top", ctypes.c_char),          # LINAGE needs top
        ("file_version", ctypes.c_char),            # file I/O version
    ]


def cob_encode_program_id(name):
    """Encode a COBOL program name to its Python identifier (typeck.c L621-L646).

    Reproduces ``cb_encode_program_id`` byte-for-byte: a leading digit and any
    character that is not alphanumeric/underscore becomes ``_HH`` (uppercase
    hex), while ``-`` becomes ``__``.  This is the name under which the code
    generator emitted both the ``.py`` module and its entry function.
    """
    if not name:
        return name
    out = []
    s = name
    i = 0
    if s[0].isdigit():
        out.append("_%02X" % ord(s[0]))
        i = 1
    for ch in s[i:]:
        o = ord(ch)
        if ch.isalnum() or ch == "_":
            out.append(ch)
        elif ch == "-":
            out.append("__")
        else:
            out.append("_%02X" % o)
    return "".join(out)


def _apply_case(name):
    """Apply the COB_LOAD_CASE folding (call.c name_convert, L450-L460)."""
    if name_convert == 1:
        return name.lower()
    if name_convert == 2:
        return name.upper()
    return name


def cob_strdup(stptr):
    """Return an independent copy of *stptr* (call.c L165-L175).

    The C ``cob_strdup`` did ``cob_malloc(strlen+1)`` + ``memcpy`` so the caller
    owned a private, mutable duplicate it could hand to ``strtok`` without
    clobbering the source.  Under Python the analogue is a fresh, independent
    object: ``str`` (immutable) is returned unchanged because no caller can
    mutate it, while ``bytes``/``bytearray``/``memoryview`` yield a brand-new
    ``bytearray`` the caller may freely mutate (the ``strtok`` use case).
    """
    if stptr is None:
        return None
    if isinstance(stptr, str):
        # Immutable: a copy is indistinguishable from the original, and every
        # call.c use of cob_strdup(str) only ever read or tokenised the result.
        return stptr
    if isinstance(stptr, (bytes, bytearray, memoryview)):
        return bytearray(bytes(stptr))
    # Any other text-like object: normalise to str (its immutable duplicate).
    return str(stptr)


def cob_set_library_path(path):
    """Set the resolver search path from a PATHSEP-delimited string (call.c L178).

    Splits *path*, records the ordered resolve paths, and prepends them to
    ``sys.path`` so :func:`importlib.import_module` can find generated modules
    placed in ``COB_LIBRARY_PATH`` directories.
    """
    global _resolve_paths
    _resolve_paths = [p for p in path.split(os.pathsep) if p]
    # Prepend (in order) to sys.path without duplicating existing entries.
    for entry in reversed(_resolve_paths):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def cob_get_buff(buffsize):
    """Return a reusable scratch buffer of at least *buffsize* bytes (call.c L204).

    Mirrors the C ``cob_get_buff``: a single module-level buffer
    (:data:`_call_buffer`) is grown (never shrunk) whenever a larger size is
    requested and the same storage is handed back to every caller, so repeated
    name/field conversions reuse one allocation just as the C runtime reused its
    ``call_buffer`` static.  The returned :class:`bytearray` is zero-filled to
    the requested width and is writable by the caller (the C out-parameter
    idiom, e.g. ``cob_field_to_string(f, buff)``).
    """
    global _call_buffer
    n = int(buffsize)
    if n < 0:
        n = 0
    if n > len(_call_buffer):
        _call_buffer = bytearray(n)
    else:
        # Reuse the existing allocation, re-zeroing the active window.
        for i in range(n):
            _call_buffer[i] = 0
    # Hand back a view of exactly the requested width over the shared buffer.
    return memoryview(_call_buffer)[:n]


def lookup(name):
    """Return the cached callable for *name*, or ``None`` (call.c ``lookup``)."""
    return _call_cache.get(name)


def insert(name, func, cancel=None):
    """Cache *func* (and optional *cancel*) under *name* (call.c ``insert``)."""
    _call_cache[name] = func
    if cancel is not None:
        _cancel_handlers[name] = cancel


def cob_resolve_error():
    """Return and clear the last resolve error message (call.c L281-L287)."""
    global _resolve_error
    p = _resolve_error
    _resolve_error = None
    return p


def cob_call_error():
    """Report the pending resolve error and stop the run unit (call.c L289-L300)."""
    s = cob_resolve_error() or "Unknown error"
    common.cob_runtime_error("%s", s)
    common.cob_stop_run(1)


def cob_set_cancel(name, entry, cancel):
    """Register/refresh the CANCEL handler for *name* (call.c L302-L320)."""
    if name in _call_cache:
        if cancel is not None:
            _cancel_handlers[name] = cancel
        return
    insert(name, entry, cancel)


def _locate_on_resolve_paths(modname):
    """Find a generated module file for *modname* on the resolver search paths.

    QA FIX (Issue G1 - inter-program CALL resolution).  Mirrors the C resolver's
    ``resolve_path[]`` scan for a ``<name>.so`` before ``lt_dlopen`` (call.c).
    ``cobc -m`` packages each compiled COBOL unit as a self-contained
    ``<PROGRAM-ID>.pyz`` zip archive whose *internal* module is named after the
    PROGRAM-ID; such an archive is NOT importable merely because its containing
    directory is on ``sys.path`` - the archive file itself must be a ``sys.path``
    entry for zipimport to expose the module inside it.  This helper scans the
    ordered resolve paths (and the current directory, which the C resolver also
    searches) for ``<modname>.pyz`` (``COB_MODULE_EXT``) first, then a plain
    ``<modname>.py``, returning the path that must be placed on ``sys.path`` to
    make ``import <modname>`` succeed - or ``None`` when no candidate exists.
    """
    # Search order mirrors the C resolve_path[]: the configured COB_LIBRARY_PATH
    # entries (already captured in _resolve_paths) followed by the current
    # directory (the C runtime's implicit "." search and the default
    # COB_LIBRARY_PATH = ".").
    search_dirs = list(_resolve_paths)
    if "." not in search_dirs:
        search_dirs.append(".")
    for ext in (COB_MODULE_EXT, "py"):
        for d in search_dirs:
            cand = os.path.join(d, modname + "." + ext)
            if os.path.isfile(cand):
                return cand
    return None


def _locate_in_loaded_modules(encoded):
    """Find an entry callable named *encoded* among already-loaded modules.

    QA FIX (COBOL-85 gate - sibling / same-source-file program CALL).  When a
    single COBOL source file declares several sequential programs - e.g. the
    NIST ``IC222A`` unit, whose source contains ``PROGRAM-ID. IC222A`` ...
    ``END PROGRAM IC222A`` immediately followed by ``PROGRAM-ID. IC222A-1`` -
    the code generator emits ALL of them as separate ``def`` entries inside ONE
    generated module (here ``IC222A`` and the encoded ``IC222A__1``).  This
    mirrors the C backend, which links every program of a compilation unit into
    a single object file.  A ``CALL "IC222A-1"`` therefore has NO standalone
    ``IC222A__1`` module on disk to import; the C resolver instead satisfies it
    from the statically-linked sibling's registered entry point.  This helper
    reproduces that resolution path by scanning the modules already loaded in
    this run for a callable attribute named after the encoded program-id and
    returning it (or ``None``).  Encoded COBOL program-ids are unique within a
    run, so a match is unambiguous; the per-resolve scan happens at most once
    per sibling because the result is then cached by :func:`cob_resolve`.
    """
    for mod_name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        # QA FIX (test 3 "Hexadecimal literal" - spurious stdlib match): restrict
        # the scan to COBOL-GENERATED units only.  The previous unrestricted scan
        # matched ANY loaded module exposing a callable of the encoded name, so a
        # CALL to a program whose name collides with a standard-library function
        # (CALL "dump" vs json/pickle/marshal ``dump``) resolved to that stdlib
        # function and was then invoked with the COBOL argument list, raising
        # "TypeError: dump() takes at least 2 positional arguments (1 given)".
        # Two disqualifiers make the scan precise:
        #   (a) skip the libcob_py runtime package and its sub-modules - they are
        #       not COBOL programs (and a sub-module like ``move``/``system`` could
        #       otherwise shadow a like-named program-id); and
        #   (b) require the module to BE a generated COBOL unit, identified by its
        #       ``common`` attribute being THIS libcob_py.common (every emitted
        #       module does ``from libcob_py import common`` to call cob_init /
        #       cob_field / ...; stdlib and site modules do not).
        # Encoded COBOL program-ids are unique within a run, so among the
        # generated units that remain a match is still unambiguous.
        if mod_name == "libcob_py" or mod_name.startswith("libcob_py."):
            continue
        if getattr(mod, "common", None) is not common:
            continue
        func = getattr(mod, encoded, None)
        if callable(func):
            return func
    return None


def _load_module(modname):
    """Import the generated module *modname*, seeding ``sys.path`` from the
    resolve paths when a direct import fails.

    Mirrors the C resolver/preloader, which probe ``resolve_path[i]/<name>.<ext>``
    and ``lt_dlopen`` the first hit (call.c ``cob_resolve`` L321 and the
    ``COB_PRE_LOAD`` scan L571-593).  ``cobc -m`` packages each compiled unit as
    a self-contained ``<name>.pyz`` zip archive; such an archive is importable
    only once the archive file itself is on ``sys.path`` (zipimport), so a bare
    :func:`importlib.import_module` is retried after locating ``<modname>.pyz``
    (``COB_MODULE_EXT``) or a plain ``<modname>.py`` on the resolve paths and
    inserting it.  Returns the imported module, or ``None`` when no importable
    candidate exists (the caller decides whether that is fatal).
    """
    try:
        importlib.invalidate_caches()
        return importlib.import_module(modname)
    except ImportError:
        archive = _locate_on_resolve_paths(modname)
        if archive is None:
            return None
        if archive.endswith("." + COB_MODULE_EXT):
            entry = archive                          # the .pyz file (zipimport)
        else:
            entry = os.path.dirname(archive) or "."  # dir holding the .py
        if entry not in sys.path:
            sys.path.insert(0, entry)
        try:
            importlib.invalidate_caches()
            return importlib.import_module(modname)
        except ImportError:
            return None


def _is_native_object(path):
    """Return True when *path* is a native shared object (ELF), not a ``.pyz``.

    MIGRATION (C->Python): the resolver must distinguish a NATIVE C subprogram
    (loaded via ctypes) from a Python ``.pyz`` zip archive (loaded via
    zipimport).  Both can share a directory on the resolve path, and - because
    the test harness builds ``dump.${SHREXT}`` - can even share an extension, so
    the discriminator is the file CONTENT: a shared object begins with the ELF
    magic ``\\x7fELF`` whereas a ``.pyz`` begins with the ZIP magic ``PK``.
    """
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == _ELF_MAGIC
    except OSError:
        return False


def _locate_native_on_resolve_paths(modname):
    """Find a native shared object for *modname* on the resolver search paths.

    MIGRATION (C->Python): mirrors the C resolver's ``resolve_path[]`` scan for a
    ``<name>.<shlibext>`` before ``lt_dlopen`` (call.c L405-L420), restricted to
    files whose content is actually native (ELF - see :func:`_is_native_object`).
    Probes, in each resolve directory (and the current directory, which the C
    resolver also searched), the platform native extension ``<modname>.so``
    first, then the COBOL module extension ``<modname>.pyz`` and the doubled-dot
    ``<modname>..pyz`` form that ``${CC} -o dump.${SHREXT}`` produces when
    ``SHREXT`` carries a leading dot, and finally the bare ``<modname>``.
    Returns the first candidate that exists AND is an ELF object, else ``None``.
    """
    search_dirs = list(_resolve_paths)
    if "." not in search_dirs:
        search_dirs.append(".")
    suffixes = (
        "." + _NATIVE_EXT,                 # dump.so   (preferred native name)
        "." + COB_MODULE_EXT,              # dump.pyz  (native built with .pyz ext)
        ".." + COB_MODULE_EXT,             # dump..pyz (SHREXT carried leading dot)
        "",                                # dump      (bare)
    )
    for suf in suffixes:
        for d in search_dirs:
            cand = os.path.join(d, modname + suf)
            if os.path.isfile(cand) and _is_native_object(cand):
                return cand
    return None


def _is_cob_file_like(arg):
    """True if *arg* duck-types as a COBOL file object (fileio.cob_file).

    MIGRATION (C->Python): a native subprogram CALL'd USING a file-name expects
    a ``cob_file *``.  We detect the Python file object structurally (it carries
    the SELECT name, the ASSIGN field and an OPEN MODE) rather than importing
    :mod:`libcob_py.fileio` (which would create an import cycle), and so that a
    plain :class:`~libcob_py.common.cob_field` (size/data/attr - no ``assign``)
    is never mistaken for a file.
    """
    return (hasattr(arg, "assign")
            and hasattr(arg, "select_name")
            and hasattr(arg, "open_mode"))


def _alias_cob_field(pyfield, keepalive):
    """Build a ctypes ``cob_field *`` aliasing a Python cob_field's storage.

    MIGRATION (C->Python): mirrors a single :class:`~libcob_py.common.cob_field`
    into a :class:`_CtCobField` whose ``data`` pointer aliases the SAME Python
    ``bytearray``/``memoryview`` storage (via ``ctypes.from_buffer``), so a
    native routine that writes through ``field->data`` mutates the COBOL data
    item in place - exactly as the C pointer did.  Returns ``None`` when the
    field (or its storage) is absent, leaving the parent pointer NULL.  Read-only
    storage (an immutable literal) cannot be aliased, so it is copied (the callee
    then cannot write back, which is correct for a read-only argument).  All
    ctypes objects are appended to *keepalive* so the aliasing survives the call.
    """
    if pyfield is None:
        return None
    data = getattr(pyfield, "data", None)
    if data is None:
        return None
    try:
        size = int(getattr(pyfield, "size", 0) or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        try:
            size = len(data)
        except TypeError:
            return None
    if size <= 0:
        return None
    try:
        cbuf = (ctypes.c_char * size).from_buffer(data)
    except (TypeError, ValueError):
        # Read-only / non-contiguous storage: copy (no write-back possible).
        cbuf = ctypes.create_string_buffer(bytes(data)[:size], size)
    keepalive.append(cbuf)
    ctf = _CtCobField()
    ctf.size = size
    ctf.data = ctypes.cast(cbuf, ctypes.c_void_p)
    ctf.attr = None
    keepalive.append(ctf)
    ptr = ctypes.pointer(ctf)
    keepalive.append(ptr)
    return ptr


def _marshal_cob_file(fobj, keepalive):
    """Marshal a Python cob_file into a ctypes ``cob_file *`` for a native call.

    MIGRATION (C->Python): constructs a :class:`_CtCobFile` whose ``assign`` and
    ``record`` members point at ctypes ``cob_field``s aliasing the Python file's
    ASSIGN/record storage, so a native routine (e.g. ``setfilename`` doing
    ``memcpy(f->assign->data, name, ...)``) sees the COBOL members at the right
    ABI offsets and writes straight back into the COBOL data items.  Members not
    backed by COBOL storage are left NULL.  Returns a ``byref`` to the struct,
    which is retained in *keepalive* for the duration of the call.
    """
    ctf = _CtCobFile()
    keepalive.append(ctf)
    assign_ptr = _alias_cob_field(getattr(fobj, "assign", None), keepalive)
    if assign_ptr is not None:
        ctf.assign = assign_ptr
    record_ptr = _alias_cob_field(getattr(fobj, "record", None), keepalive)
    if record_ptr is not None:
        ctf.record = record_ptr
    return ctypes.byref(ctf)


def _marshal_native_arg(arg, keepalive):
    """Convert one COBOL CALL argument into a ctypes argument for a native call.

    MIGRATION (C->Python): reproduces the raw-pointer argument passing the C
    backend used when calling a native subprogram.  Each emitted CALL argument
    arrives as one of: ``None`` (OMITTED -> NULL pointer); an ``int`` (BY VALUE
    numeric -> passed by value); ``bytes`` (immutable BY CONTENT literal -> a
    NUL-padded copy so the callee may read slightly past the end and see zero,
    matching zeroed static literal storage); or a writable buffer
    (``bytearray`` / ``memoryview`` for BY REFERENCE or a BY CONTENT temp ->
    aliased in place via ``ctypes.from_buffer`` so the callee's writes propagate
    back to the COBOL data item, exactly like the C pointer did).  Objects that
    must outlive the call are appended to *keepalive*.
    """
    if arg is None:
        return ctypes.c_void_p(None)
    if isinstance(arg, bool):
        # Guard before int (bool is a subclass of int); treat as small integer.
        return ctypes.c_longlong(int(arg))
    if isinstance(arg, int):
        # BY VALUE numeric: passed by value (rare for the native helpers, which
        # take pointers, but supported for completeness / faithfulness).
        return ctypes.c_longlong(arg)
    if isinstance(arg, (bytes, bytearray, memoryview)):
        mv = arg if isinstance(arg, memoryview) else memoryview(arg)
        if mv.readonly:
            # Immutable (BY CONTENT literal): copy into a NUL-padded buffer.
            buf = ctypes.create_string_buffer(bytes(mv), mv.nbytes + _NATIVE_PAD)
            keepalive.append(buf)
            return ctypes.cast(buf, ctypes.c_void_p)
        # Writable: alias the SAME storage so C writes propagate (BY REFERENCE).
        cbuf = (ctypes.c_char * mv.nbytes).from_buffer(mv)
        keepalive.append(cbuf)
        return ctypes.cast(cbuf, ctypes.c_void_p)
    if _is_cob_file_like(arg):
        # A COBOL file-name argument: hand the native routine a ctypes cob_file
        # whose assign/record members alias the COBOL storage (see
        # _marshal_cob_file), so f->assign->data writes propagate back.
        return _marshal_cob_file(arg, keepalive)
    # Anything else (an unrecognised Python object a native routine expects as a
    # struct pointer): unsupported here -> NULL.  The native call then no-ops on
    # that argument rather than crashing the interpreter.
    return ctypes.c_void_p(None)


def _make_native_wrapper(cfunc):
    """Wrap a ctypes C function so it presents the COBOL entry-callable contract.

    MIGRATION (C->Python): the emitted code invokes a resolved entry as
    ``ret = _unifunc(arg0, arg1, ...)`` and stores ``ret`` via
    ``move.cob_set_int``.  This wrapper marshals each argument
    (:func:`_marshal_native_arg`), invokes the native function (whose
    ``restype`` is set to ``c_int`` so the COBOL RETURN-CODE comes back as a
    Python ``int``), keeps the aliasing buffers alive across the call so any
    write-through has completed, and returns the integer result (0 for a void
    return).
    """
    def _native_entry(*args):
        keepalive = []
        cargs = [_marshal_native_arg(a, keepalive) for a in args]
        result = cfunc(*cargs)
        # keepalive intentionally retained until here: the ctypes buffers alias
        # the COBOL storage, so the callee's writes are already visible.
        del keepalive
        return 0 if result is None else int(result)
    return _native_entry


def _resolve_native(name, encoded):
    """Resolve a CALL target to a NATIVE C subprogram loaded through ctypes.

    MIGRATION (C->Python): faithful stdlib replacement for the C resolver's
    ``lt_dlopen``/``lt_dlsym`` path (call.c L405-L426) for subprograms that are
    genuinely native code (a ``.c`` compiled to a shared object), which cannot
    be expressed as Python.  Locates the shared object on the resolve paths
    (:func:`_locate_native_on_resolve_paths`), ``dlopen``'s it via
    :class:`ctypes.CDLL`, and looks up the C entry symbol - the symbol name is
    the program name exactly as written (the C function name), with the
    case-folded and encoded forms tried as fallbacks.  Returns a Python callable
    wrapping the native function, or ``None`` when no native object/symbol is
    found (the caller then reports PROGRAM-NOT-FOUND, exactly as before).
    """
    modname = _apply_case(name)
    path = _locate_native_on_resolve_paths(modname)
    if path is None:
        return None
    try:
        lib = ctypes.CDLL(path)
    except OSError:
        return None
    for sym in (name, modname, encoded):
        if not sym:
            continue
        try:
            cfunc = getattr(lib, sym)
        except AttributeError:
            continue
        cfunc.restype = ctypes.c_int
        return _make_native_wrapper(cfunc)
    return None


def cob_resolve(name):
    """Resolve a COBOL program *name* to its Python entry callable (call.c L321).

    Searches the call cache, then imports the module named after the encoded
    program-id and returns its entry function.  On failure sets
    ``EC-PROGRAM-NOT-FOUND`` and records the error (returning ``None``), exactly
    like the C resolver's not-found path.
    """
    global _resolve_error
    common.cob_exception_code = 0

    func = lookup(name)
    if func is not None:
        return func

    encoded = cob_encode_program_id(name)
    # MIGRATION (C->Python): the C resolver (call.c L393-L426) builds the module
    # FILE name from the (case-folded) ORIGINAL program name -- "<name>.<ext>" --
    # and uses the ENCODED name ONLY for the dlsym ENTRY-POINT lookup inside the
    # loaded handle.  cobc likewise names the generated module file and its
    # internal ".py" after the source/PROGRAM-ID base, NOT the encoded id, so an
    # unusual PROGRAM-ID like "A@B" produces "A@B.pyz" containing module "A@B"
    # whose entry def is the encoded "A_40B".  Locate/import by the original
    # (case-folded) base; fetch the entry below by the encoded name.  For
    # ordinary names (no leading digit / non-alnum char) the encoded and original
    # forms are identical, so this is a no-op for the common case.
    modname = _apply_case(name)

    # QA FIX (Issue G1 - inter-program CALL resolution): a CALLed subprogram
    # compiled with "cobc -m" is a self-contained "<PROGRAM-ID>.pyz" archive
    # sitting in a COB_LIBRARY_PATH directory (or "."), not a module already
    # importable by name.  _load_module() mirrors the C resolver's resolve_path[]
    # scan for "<name>.so" before lt_dlopen by locating "<modname>.pyz" (or a
    # plain "<modname>.py") on the resolve paths, seeding sys.path (zipimport),
    # and importing it.
    module = _load_module(modname)
    if module is None and encoded != modname:
        # MIGRATION (C->Python): a non-identifier PROGRAM-ID (e.g. "MY-PROG",
        # "A@B") cannot be imported by its original name -- the dash/"@" is
        # invalid in a Python module name -- whereas the C resolver dlopen'd
        # "<name>.so" by PATH, which works for any name.  The faithful Python
        # equivalent is the ENCODED id (cob_encode_program_id), which is always a
        # valid identifier and is exactly the symbol the code generator emits for
        # the entry def; a standalone subprogram for such a program is therefore
        # importable as "<encoded>.pyz"/"<encoded>.py" (e.g. "cobc -m -o
        # MY__PROG.pyz ...").  This is purely additive: it runs only when the
        # original-name load failed and the encoded form differs, so ordinary
        # identifier names (encoded == modname) and the source-base / preload
        # resolution paths below are unaffected.
        module = _load_module(encoded)
    if module is None:
        # QA FIX (COBOL-85 gate - sibling program CALL): a program that
        # shares its source file with the caller has NO standalone module on
        # disk to import (the code generator emits sibling programs as
        # separate entries inside the caller's own module).  This ALSO covers
        # a COB_PRE_LOAD'd module whose file name differs from its PROGRAM-ID
        # (e.g. "cobc -m callee.cob" with "PROGRAM-ID. callee2"): the preload
        # imported "callee" - registering "callee2" inside it - so CALL
        # "callee2" finds the entry here even though no "callee2.pyz" exists.
        # Before declaring the program unresolvable, mirror the C resolver
        # locating a statically-linked sibling: search the already-loaded
        # modules for the encoded entry and, when present, cache and return it.
        sibling = _locate_in_loaded_modules(encoded)
        if sibling is not None:
            insert(name, sibling, None)
            _resolve_error = None
            return sibling
        # MIGRATION (C->Python): no Python module resolved the name; before
        # declaring it unresolvable, try a NATIVE C subprogram (a ".c" compiled
        # to a shared object) loaded through ctypes - the faithful stdlib
        # equivalent of the C resolver's lt_dlopen/lt_dlsym path for native
        # CALL targets (e.g. CALL "dump" to a gcc-built dump.so).
        native = _resolve_native(name, encoded)
        if native is not None:
            insert(name, native, None)
            _resolve_error = None
            return native
        _resolve_error = "Cannot find module '%s'" % name
        common.cob_set_exception(common.COB_EC_PROGRAM_NOT_FOUND)
        return None

    # The entry point is the attribute named after the encoded program-id
    # (the def the code generator emitted); fall back to a conventional
    # ``main`` only if that attribute is absent.
    func = getattr(module, encoded, None)
    if func is None:
        func = getattr(module, modname, None)
    if func is None:
        func = getattr(module, "main", None)
    if func is None or not callable(func):
        _resolve_error = "Cannot find entry point in module '%s'" % name
        common.cob_set_exception(common.COB_EC_PROGRAM_NOT_FOUND)
        return None

    insert(name, func, None)
    _resolve_error = None
    return func


def cob_resolve_1(name):
    """:func:`cob_resolve` that aborts on failure (call.c L448-L459)."""
    p = cob_resolve(name)
    if p is None:
        cob_call_error()
    return p


def cob_call_resolve(f):
    """Resolve a program named by the contents of field *f* (call.c L460-L470)."""
    return cob_resolve(common.cob_field_to_string(f))


def cob_call_resolve_1(f):
    """:func:`cob_call_resolve` that aborts on failure (call.c L470-L480)."""
    p = cob_call_resolve(f)
    if p is None:
        cob_call_error()
    return p


def cobcancel(name):
    """CANCEL *name*: run its cancel handler and evict it (call.c L481-L510).

    Invokes any registered cancel routine, then drops the program from the
    call cache, its cancel handler, and ``sys.modules`` and invalidates the
    import caches so a subsequent CALL re-imports a fresh module instance - the
    Python analogue of the C uncache/``lt_dlclose`` behaviour.

    REVIEW FIX (MAJOR #7): in the C runtime the entry function and its cancel
    routine live in a *single* ``struct call_hash`` (call.c L138-L145), so
    uncaching an entry frees both at once.  The Python port splits them across
    the two parallel dicts ``_call_cache`` and ``_cancel_handlers``; this used
    to pop only ``_call_cache``, leaving a *stale* cancel handler behind that a
    later re-CALL/re-CANCEL of the same name would wrongly re-run.  Both dicts
    are now evicted together to restore the one-entry C semantics.
    """
    if name is None:
        common.cob_runtime_error("NULL name parameter passed to 'cobcancel'")
        common.cob_stop_run(1)
        return
    cancel = _cancel_handlers.get(name)
    if cancel is not None and callable(cancel):
        # QA FIX (emitter<->runtime CANCEL contract, surfaced by the COBOL-85
        # acceptance gate; same contract family as the STRING fix in
        # strings.py).  The code generator emits every program entry with a
        # leading control selector and a "if _entry < 0:" CANCEL-cleanup branch
        # (codegen.c output_entry), and registers that entry as its OWN cancel
        # handler via ``call.cob_set_cancel(name, entry, entry_)``.  The C
        # runtime fires the cancel handler through a varargs prototype
        # ``int (*cancel_func)(int, ...)`` and pads the call with eight NULLs
        # (call.c L501-L504) -- C varargs silently discards the surplus
        # arguments, so only the leading ``-1`` control selector is observed by
        # the cleanup branch.  The generated *Python* entry, however, has a
        # FIXED arity (``def P_(_entry, <using>=None, ...)``), so replaying the
        # C nine-argument idiom raised ``TypeError: P_() takes from 1 to N
        # positional arguments but 9 were given``.  Invoke the handler with only
        # the control selector ``-1`` (the cleanup branch ignores the USING
        # operands, which default to ``None``); a residual arity mismatch is
        # non-fatal because the authoritative Python CANCEL is the module
        # eviction performed immediately below (del sys.modules + invalidate).
        try:
            cancel(-1)
        except TypeError:
            pass
    # Evict from the call cache, the cancel-handler table, and the import system
    # together (the C ``struct call_hash`` held func + cancel as one entry).
    _call_cache.pop(name, None)
    _cancel_handlers.pop(name, None)
    modname = _apply_case(cob_encode_program_id(name))
    sys.modules.pop(modname, None)
    importlib.invalidate_caches()


def cob_field_cancel(f):
    """CANCEL the program named by the contents of field *f* (call.c L510-L520)."""
    cobcancel(common.cob_field_to_string(f))


def cob_exit_call():
    """Tear the call subsystem down at STOP RUN / tidy (call.c ``cob_exit_call``).

    REVIEW FIX (MAJOR #7, cleanup-on-stop-run).  The dynamic-loader caches are
    deliberately unbounded for the lifetime of a run (resident-program
    semantics - see the ``_call_cache`` note), but they MUST be released when
    the run ends so a fresh ``cob_init`` starts from an empty table (and so no
    cancel handler outlives the run).  This mirrors the C ``cob_exit_call``
    freeing ``call_table``; it is invoked from
    :func:`libcob_py.common._shutdown_runtime` (the shared STOP RUN / cobtidy
    teardown path).  Both parallel dicts are cleared in lockstep and the import
    caches are invalidated; ``_resolve_paths`` is left to ``cob_init_call`` to
    re-seed.  It is idempotent and safe to call more than once.
    """
    _call_cache.clear()
    _cancel_handlers.clear()
    importlib.invalidate_caches()


def cob_init_call():
    """Initialise the call subsystem (call.c L520-L607).

    Reads ``COB_LOAD_CASE`` (name folding), seeds ``sys.path`` from
    ``COB_LIBRARY_PATH`` (current directory first, built-in default last),
    preloads any ``COB_PRE_LOAD`` modules, and registers the CBL_/C$ system
    routines so a dynamic ``CALL "CBL_..."`` resolves to :mod:`libcob_py.system`.
    """
    global name_convert
    s = os.environ.get("COB_LOAD_CASE")
    if s is not None:
        if s.upper() == "LOWER":
            name_convert = 1
        elif s.upper() == "UPPER":
            name_convert = 2

    s = os.environ.get("COB_LIBRARY_PATH")
    if s is None:
        path = "." + os.pathsep + COB_LIBRARY_PATH
    else:
        path = s + os.pathsep + "." + os.pathsep + COB_LIBRARY_PATH
    cob_set_library_path(path)

    # Register the system routines so dynamic CALLs to CBL_/C$ names resolve.
    try:
        from libcob_py import system as _system
        for ext_name, internal in _system.SYSTEM_TABLE.items():
            fn = getattr(_system, internal, None)
            if fn is not None:
                insert(ext_name, fn, None)
    except Exception:                       # pragma: no cover - system optional
        pass

    # COB_PRE_LOAD: import each named module now (call.c L571-L593).  The C
    # preloader stat()s "resolve_path[i]/<name>.<COB_MODULE_EXT>" and lt_dlopen()s
    # the first hit; _load_module() is the faithful Python equivalent - it
    # locates "<name>.pyz" (or "<name>.py") on the resolve paths, seeds sys.path
    # (zipimport), and imports it.  A bare importlib.import_module() would miss a
    # ".pyz" that is not yet a sys.path entry, silently dropping the preload (the
    # COB_PRE_LOAD test calls a PROGRAM-ID whose entry lives only in the
    # preloaded archive, so the preload MUST actually load it).  A missing
    # preload module returns None and is skipped, exactly as the C code breaks
    # out without error.
    s = os.environ.get("COB_PRE_LOAD")
    if s is not None:
        for mod in s.split(os.pathsep):
            if not mod:
                continue
            _load_module(_apply_case(cob_encode_program_id(mod)))


# ===========================================================================
# Programmatic call entry points (call.c cobcall / cobfunc).
#
# These are the public "call a program by name with an argument vector"
# helpers used by the C API and by ``cobcrun``.  Under the Python backend the
# resolved entry point is an ordinary callable, so invocation collapses to
# ``func(*args)``; the resolver/cancel machinery above does the heavy lifting.
# ===========================================================================

# COB_MAX_COBCALL_PARMS (call.c L103): the fixed upper bound on CALL arguments.
COB_MAX_COBCALL_PARMS = 16


def cobcall(name, argc, argv):
    """Resolve *name* and invoke it with *argc* arguments from *argv* (call.c L602).

    Reproduces the C ``cobcall`` contract exactly: the runtime must be
    initialised, ``argc`` must lie in ``0..COB_MAX_COBCALL_PARMS`` (16) and
    *name* must be non-``None``; otherwise a runtime error is raised and the run
    unit stops.  ``cob_resolve_1`` performs the lookup (aborting if the program
    cannot be found), :data:`common.cob_call_params` is set to *argc* (so the
    callee's ``C$NARG`` / ``cob_get_int`` sees the right count) and the entry
    callable is invoked with the first *argc* items of *argv*.  Returns whatever
    the called program returns (its RETURN-CODE).
    """
    if not common.cob_initialized:
        common.cob_runtime_error("'cobcall' - Runtime has not been initialized")
        common.cob_stop_run(1)
    if argc < 0 or argc > COB_MAX_COBCALL_PARMS:
        common.cob_runtime_error("Invalid number of arguments to 'cobcall'")
        common.cob_stop_run(1)
    if name is None:
        common.cob_runtime_error("NULL name parameter passed to 'cobcall'")
        common.cob_stop_run(1)
    func = cob_resolve_1(name)
    # Publish the parameter count exactly like the C did (call.c L628).
    common.cob_call_params = argc
    # Marshal the first argc arguments, padding short vectors with None so the
    # callee always receives argc positional arguments (the C runtime padded
    # the unused pargv[] slots with NULL).
    seq = list(argv) if argv is not None else []
    args = [seq[i] if i < len(seq) else None for i in range(argc)]
    return func(*args)


def cobfunc(name, argc, argv):
    """Call *name* then immediately CANCEL it (call.c L638).

    The C ``cobfunc`` is ``cobcall`` followed by ``cobcancel`` so a one-shot
    invocation leaves no cached/loaded state behind (a fresh module is loaded on
    any later CALL).  The runtime must already be initialised.
    """
    if not common.cob_initialized:
        common.cob_runtime_error("'cobfunc' - Runtime has not been initialized")
        common.cob_stop_run(1)
    ret = cobcall(name, argc, argv)
    cobcancel(name)
    return ret


# ===========================================================================
# CALL argument-passing wrappers (BY CONTENT / BY VALUE).
#
# The emitter (codegen.c output_call) lowers CALL argument construction to
# these helpers (replacing the C union/cast idioms the original backend wrote
# inline):
#   * BY CONTENT / BY REFERENCE of a numeric literal or expression  ->
#       content_N = call.cob_content_int(value, fits_int)   (codegen.c L2795)
#   * BY CONTENT of a data item                                     ->
#       content_N = call.cob_content_buffer(data, size)     (codegen.c L2849)
#   * BY VALUE numeric argument (caller side)                       ->
#       call.cob_value_int(value, nbytes, unsigned_flag)    (codegen.c L2750)
#   * BY VALUE numeric argument (callee re-wrap, &i_N)              ->
#       call.cob_value_buffer(value, nbytes, unsigned_flag) (codegen.c L5187)
#
# "content"/"value buffer" results are writable byte buffers (a callee may
# mutate a BY CONTENT copy without touching the caller's storage); "value int"
# returns the integer the callee receives BY VALUE.
# ===========================================================================

def cob_content_int(value, fits_int):
    """Materialise integer *value* into a fresh writable native-endian buffer.

    Mirrors the C BY CONTENT / BY REFERENCE union temporary: *fits_int* truthy
    selects a 4-byte ``int`` storage, otherwise an 8-byte ``long long``.  The
    value is stored two's-complement (negative values wrap into the width), so
    the callee reads back exactly the bits the C runtime produced.  Returns a
    :class:`bytearray` so the callee may write through it (BY REFERENCE).
    """
    width = 4 if fits_int else 8
    mask = (1 << (width * 8)) - 1
    return bytearray((int(value) & mask).to_bytes(width, sys.byteorder))


def cob_content_buffer(data, size):
    """Return a fresh writable copy of the first *size* bytes of *data*.

    Implements BY CONTENT semantics: the callee receives an independent buffer,
    so any mutation cannot reach the caller's storage (codegen.c L2843-L2851).
    *data* may be a ``memoryview``/``bytes``/``bytearray``; short data is
    right-padded with NULs to *size* so the copy always has the declared width.
    """
    n = int(size)
    mv = data if isinstance(data, memoryview) else memoryview(data)
    chunk = bytes(mv[:n])
    if len(chunk) < n:
        chunk = chunk + b"\x00" * (n - len(chunk))
    return bytearray(chunk)


def cob_value_int(value, nbytes, unsigned_flag):
    """Return integer *value* reduced to a *nbytes*-wide BY VALUE argument.

    Reproduces the C cast ``(unsigned short)(...)`` / ``(int)(...)`` etc. the
    original backend emitted for BY VALUE numerics (codegen.c L2748-L2752):
    the value is masked to ``nbytes`` bytes and then interpreted as unsigned
    (*unsigned_flag* truthy) or two's-complement signed.  The callee receives
    this Python ``int`` directly (BY VALUE numerics arrive as ints).
    """
    bits = int(nbytes) * 8
    mask = (1 << bits) - 1
    v = int(value) & mask
    if not unsigned_flag and v >= (1 << (bits - 1)):
        v -= (1 << bits)        # sign-extend into a negative Python int
    return v


def cob_value_buffer(value, nbytes, unsigned_flag):
    """Wrap BY VALUE integer *value* into a *nbytes*-wide writable buffer.

    On the callee side a BY VALUE numeric argument arrives as a Python ``int``;
    the internal function expects to read it like the C runtime read ``&i_N``,
    so the value is encoded into a native-endian :class:`bytearray` of the
    declared width (codegen.c L5187).  Signedness only affects how an
    out-of-range value wraps; the stored bits are two's-complement either way.
    """
    bits = int(nbytes) * 8
    mask = (1 << bits) - 1
    return bytearray((int(value) & mask).to_bytes(int(nbytes), sys.byteorder))
