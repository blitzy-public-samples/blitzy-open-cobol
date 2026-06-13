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
    for mod in list(sys.modules.values()):
        if mod is None:
            continue
        func = getattr(mod, encoded, None)
        if callable(func):
            return func
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
    modname = _apply_case(encoded)

    try:
        importlib.invalidate_caches()
        module = importlib.import_module(modname)
    except ImportError:
        # QA FIX (Issue G1 - inter-program CALL resolution): a CALLed subprogram
        # compiled with "cobc -m" is a self-contained "<PROGRAM-ID>.pyz" archive
        # sitting in a COB_LIBRARY_PATH directory (or "."), not a module already
        # importable by name.  importlib will not import it unless the archive
        # itself is a sys.path entry (zipimport), so - mirroring the C resolver's
        # resolve_path[] scan for "<name>.so" before lt_dlopen - locate
        # "<modname>.pyz" (or a plain "<modname>.py") on the resolve paths, put
        # it on sys.path, and retry the import exactly once.
        module = None
        archive = _locate_on_resolve_paths(modname)
        if archive is not None:
            if archive.endswith("." + COB_MODULE_EXT):
                entry = archive                      # the .pyz file (zipimport)
            else:
                entry = os.path.dirname(archive) or "."   # dir holding the .py
            if entry not in sys.path:
                sys.path.insert(0, entry)
            try:
                importlib.invalidate_caches()
                module = importlib.import_module(modname)
            except ImportError:
                module = None
        if module is None:
            # QA FIX (COBOL-85 gate - sibling program CALL): a program that
            # shares its source file with the caller has NO standalone module on
            # disk to import (the code generator emits sibling programs as
            # separate entries inside the caller's own module).  Before
            # declaring the program unresolvable, mirror the C resolver locating
            # a statically-linked sibling: search the already-loaded modules for
            # the encoded entry and, when present, cache and return it.
            sibling = _locate_in_loaded_modules(encoded)
            if sibling is not None:
                insert(name, sibling, None)
                _resolve_error = None
                return sibling
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

    # COB_PRE_LOAD: import each named module now (call.c L571-L593).
    s = os.environ.get("COB_PRE_LOAD")
    if s is not None:
        for mod in s.split(os.pathsep):
            if not mod:
                continue
            try:
                importlib.import_module(_apply_case(cob_encode_program_id(mod)))
            except ImportError:
                # Mirror the C behaviour: a missing preload module is skipped.
                continue


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
