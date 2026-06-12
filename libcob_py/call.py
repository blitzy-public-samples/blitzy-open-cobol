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

Standard-library only (Rule R3): ``importlib``, ``sys``, ``os``, ``struct``.
"""
import importlib
import os
import struct
import sys

from libcob_py import common

# ---------------------------------------------------------------------------
# Module-scope resolver state (mirrors the C file-scope statics).
# ---------------------------------------------------------------------------
# Ordered resolve paths (call.c ``resolve_path[]``); also pushed onto sys.path.
_resolve_paths = []
# name -> callable cache (call.c ``call_table`` / ``lookup``/``insert``).
_call_cache = {}
# name -> cancel callable (call.c ``struct call_hash.cancel``).
_cancel_handlers = {}
# Last resolve error message (call.c ``resolve_error``).
_resolve_error = None
# COB_LOAD_CASE: 0 = preserve, 1 = lower, 2 = upper (call.c ``name_convert``).
name_convert = 0

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
    call cache and from ``sys.modules`` and invalidates the import caches so a
    subsequent CALL re-imports a fresh module instance - the Python analogue of
    the C uncache/``lt_dlclose`` behaviour.
    """
    if name is None:
        common.cob_runtime_error("NULL name parameter passed to 'cobcancel'")
        common.cob_stop_run(1)
        return
    cancel = _cancel_handlers.get(name)
    if cancel is not None and callable(cancel):
        cancel(-1, None, None, None, None, None, None, None, None)
    # Evict from the call cache and the import system.
    _call_cache.pop(name, None)
    modname = _apply_case(cob_encode_program_id(name))
    sys.modules.pop(modname, None)
    importlib.invalidate_caches()


def cob_field_cancel(f):
    """CANCEL the program named by the contents of field *f* (call.c L510-L520)."""
    cobcancel(common.cob_field_to_string(f))


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
