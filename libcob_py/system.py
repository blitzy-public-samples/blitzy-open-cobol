"""libcob_py.system - COBOL system service routines (CBL_*/C$*/SYSTEM).

Python standard-library port of the system-routine dispatch table defined in
``libcob/system.def`` (the authoritative ``COB_SYSTEM_GEN`` enumeration) together
with the C implementations physically located in ``libcob/common.c`` (the
bit-logic, case, ``SYSTEM`` and call-frame helpers) and ``libcob/fileio.c`` (the
low-level file-handle API and the ``C$`` ACUCOBOL filesystem wrappers).  Part of
the C->Python backend refactor (AAP 0.4.1).

Count mandate (AAP 0.6.1 rule 3 - the .def is authoritative).  ``system.def``
enumerates **exactly 43** ``COB_SYSTEM_GEN (external, param-count, internal)``
rows; the line carrying the ``/* COB_SYSTEM_GEN (external name, ...) */`` text is
a comment, not an entry.  The AAP narrative says "44"; per the .def-authority
hierarchy the authoritative enumeration is implemented exactly and the **43 vs
44 count delta is logged here** and treated as a non-authoritative aggregate.
Every external name is exposed through :data:`SYSTEM_TABLE` mapping
``external -> internal-function-name``; :mod:`libcob_py.call` registers these at
runtime init (``cob_init_call``) so that a dynamic ``CALL "CBL_..."`` /
``CALL "C$..."`` resolves to the functions below via
``getattr(system, internal_name)``.

Dependencies (AAP whitelist - standard library plus the two declared runtime
modules ``common`` and ``fileio``).  **Standard-library only** (AAP 0.5 / 0.7.1):
``os``, ``struct``, ``time``.  No third-party packages.  ``common`` is the
runtime base; the five call-frame ``C$`` helpers (``C$GETPID``/``C$NARG``/
``C$PARAMSIZE``/``C$SLEEP``/``C$JUSTIFY``) are re-exported from it (their C
bodies live in ``common.c``), and the five filesystem ``C$`` routines
(``C$CHDIR``/``C$COPY``/``C$DELETE``/``C$FILEINFO``/``C$MAKEDIR``) are
**delegated to** :mod:`libcob_py.fileio`, where they physically live in the C
source (``fileio.c``).

Calling convention (matches the emitter, ``cobc/codegen.c`` output_param):

* ``BY REFERENCE`` data operands arrive as a mutable :class:`memoryview` over the
  COBOL data item's backing bytearray - the byte-exact equivalent of the C
  ``unsigned char *``.  Routines mutate them in place (``mv[i] = b``,
  ``mv[a:b] = ...``).
* ``BY VALUE`` operands (e.g. the ``length`` of the bitwise routines) arrive as a
  plain Python ``int``.
* Several routines additionally consult
  ``common.cob_current_module.cob_procedure_parameters`` exactly where the C
  code does (filename extraction, C$NARG/C$PARAMSIZE call-frame inspection,
  SYSTEM command text).  The ``fileio`` C$ wrappers expect ``cob_field``
  operands, so the delegating wrappers below resolve each operand to a
  ``cob_field`` (preferring the procedure-parameter frame) before delegating.

Each routine returns its integer return-code (0 on success), matching the C
``int`` return contract the emitter captures via ``move.cob_set_int``.
"""

# --- Standard-library imports (rule: stdlib only) --------------------------
import os
import struct
import time

# --- Internal runtime imports (AAP whitelist: common + fileio) -------------
# ``common`` is the runtime base.  ``fileio`` owns the C$ filesystem routines
# (their C bodies live in fileio.c); the wrappers below delegate to it.  The
# numeric ``move`` accessors are reached only through ``common``'s deferred
# accessor (and a single deferred helper for the 64-bit nanosleep value),
# mirroring the package-wide ``from libcob_py import move`` idiom used by
# common.py / fileio.py / strings.py to avoid import-time coupling.
from libcob_py import common
from libcob_py import fileio

# Re-export the C$ call-frame helper routines whose C bodies live in common.c
# (common.c L2167-L2292).  These are bound as explicit module-level assignments
# (rather than a ``from ... import`` re-export) so that
# ``getattr(system, internal_name)`` resolves for the
# C$GETPID / C$NARG / C$PARAMSIZE / C$SLEEP / C$JUSTIFY rows of the dispatch
# table -- call.py uses exactly that getattr lookup.  Binding via assignment
# (not import) keeps the static-lint surface clean: these names are a
# deliberate part of this module's public API, not dead imports.
cob_acuw_getpid = common.cob_acuw_getpid
cob_return_args = common.cob_return_args
cob_parameter_size = common.cob_parameter_size
cob_acuw_sleep = common.cob_acuw_sleep
cob_acuw_justify = common.cob_acuw_justify

#: Shared ALPHANUMERIC attribute used when wrapping a raw operand buffer in a
#: transient :class:`common.cob_field` for delegation to the fileio C$ routines.
_ALNUM_ATTR = common.cob_field_attr(
    type=common.COB_TYPE_ALPHANUMERIC, digits=0, scale=0, flags=0, pic=None)


# ===========================================================================
# Argument-coercion helpers
# ===========================================================================
def _as_buf(x):
    """Return a mutable byte buffer view of *x*.

    Accepts the :class:`memoryview` the emitter passes for a BY REFERENCE
    operand, a raw ``bytearray``/``bytes``, or a :class:`common.cob_field`
    (whose ``.data`` bytearray is returned).  This makes the routines callable
    both from generated code (memoryview) and directly from unit tests
    (cob_field / bytearray).
    """
    if isinstance(x, common.cob_field):
        return x.data
    return x


def _as_int(x):
    """Return the integer value of a BY VALUE operand.

    A plain ``int`` is returned unchanged; a :class:`common.cob_field` is
    decoded through the runtime integer accessor (so tests may pass a numeric
    field where generated code passes the pre-evaluated int).  The decode is
    routed through ``common._lazy_get_int`` - the canonical, whitelisted
    field->int accessor (it performs the deferred ``move.cob_get_int`` call),
    so this module needs no direct ``move`` import.
    """
    if isinstance(x, common.cob_field):
        return common._lazy_get_int(x)
    return int(x)


def _get_long_long(f):
    """Decode :class:`common.cob_field` *f* as a C ``long long`` (64-bit).

    CBL_OC_NANOSLEEP reads a nanosecond count that the C runtime decodes with
    ``cob_get_long_long`` (common.c L2137) - a 64-bit value that ``cob_get_int``
    would truncate.  ``common`` exposes no 64-bit lazy accessor, so this mirrors
    the package-wide deferred-``move`` idiom (the same pattern as
    ``common._lazy_get_int``) for the one place that genuinely needs it,
    preserving byte-for-byte numeric fidelity without a module-level ``move``
    dependency.
    """
    from libcob_py import move  # deferred: preserve cob_get_long_long fidelity
    return move.cob_get_long_long(f)


def _fld_str(x):
    """Extract a filesystem path string from a COBOL data operand.

    Faithful reproduction of ``cob_str_from_fld`` (fileio.c): trailing spaces
    and NULs are trimmed, surrounding double quotes are stripped, and an
    unquoted embedded blank terminates the name.  Accepts a memoryview,
    bytes/bytearray, or cob_field.
    """
    buf = _as_buf(x)
    raw = bytes(buf)

    # Trim trailing spaces / NULs (cob_str_from_fld: scan from the end).
    i = len(raw)
    while i > 0 and raw[i - 1] in (0x20, 0x00):
        i -= 1

    out = bytearray()
    quote_switch = False
    for n in range(i):
        ch = raw[n]
        if ch == 0x22:  # '"' toggles quoting and is itself dropped
            quote_switch = not quote_switch
            continue
        if quote_switch:
            out.append(ch)
            continue
        if ch in (0x20, 0x00):  # unquoted blank/NUL terminates the name
            break
        out.append(ch)
    return out.decode("latin-1")


def _proc_param(n):
    """Return the n-th current-module procedure parameter (cob_field) or None.

    Mirrors ``cob_current_module->cob_procedure_parameters[n]``; returns None
    when no module is active or the slot is unset, so callers can replicate the
    C ``if (!...parameters[0]) return -1`` guards.
    """
    module = common.cob_current_module
    if module is None:
        return None
    params = getattr(module, "cob_procedure_parameters", None)
    if not params or n >= len(params):
        return None
    return params[n]


def _filename_arg(direct, idx=0):
    """Resolve a filename, preferring the call-frame parameter (C behaviour).

    The C routines extract the name from ``cob_procedure_parameters[idx]`` via
    ``cob_str_from_fld``; when no frame is present (direct unit-test calls) the
    passed operand is used instead.
    """
    param = _proc_param(idx)
    if param is not None:
        return _fld_str(param)
    return _fld_str(direct)


def _as_field(operand, idx):
    """Resolve a filesystem operand to a :class:`common.cob_field` for fileio.

    The fileio ``C$`` wrappers consume ``cob_field`` operands (they call
    ``common.cob_field_to_string``), whereas the emitter passes BY REFERENCE
    operands as memoryviews.  This bridges the two conventions, exactly as the C
    code does: prefer the procedure-parameter frame entry
    (``cob_procedure_parameters[idx]``); otherwise wrap the raw positional bytes
    (memoryview/bytes/bytearray) in a transient ALPHANUMERIC field; ``None`` (no
    operand) propagates as ``None`` so fileio's parameter-count guard fires.
    """
    param = _proc_param(idx)
    if isinstance(param, common.cob_field):
        return param
    if isinstance(operand, common.cob_field):
        return operand
    if operand is None:
        return None
    raw = bytes(_as_buf(operand))
    return common.cob_field(size=len(raw), data=bytearray(raw), attr=_ALNUM_ATTR)


def _status_field(status, idx):
    """Resolve the OUTPUT status operand of C$CHDIR to a cob_field (or None).

    The C ``cob_acuw_chdir`` stores its result into the call-frame field
    ``cob_procedure_parameters[idx]``; prefer that, falling back to a directly
    supplied ``cob_field``.  A bare memoryview cannot receive an integer MOVE,
    so it maps to ``None`` (status simply not written, matching the C path when
    no usable field is present).
    """
    param = _proc_param(idx)
    if isinstance(param, common.cob_field):
        return param
    if isinstance(status, common.cob_field):
        return status
    return None


# ===========================================================================
# Authoritative dispatch table - libcob/system.def (43 COB_SYSTEM_GEN rows)
# ===========================================================================
# external-name -> (param-count, internal-function-name).  The param counts are
# reproduced verbatim from system.def for diagnostics/validation; call.py keys
# off the internal name via getattr(system, internal).  The two high-bit
# external names use the exact octal escapes from the .def (\221, \364, \365 =
# bytes 0x91, 0xF4, 0xF5).  NOTE (43 vs 44): the AAP narrative cites 44 system
# routines; the authoritative system.def contains exactly 43 COB_SYSTEM_GEN
# entries (the 44th "row" is the format comment).  Per AAP 0.6.1 rule 3 the .def
# wins; all 43 are implemented and the delta is logged (see module docstring).
SYSTEM_DEF = (
    ("SYSTEM", 1, "SYSTEM"),
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
    ("C$TOUPPER", 2, "CBL_TOUPPER"),
    ("C$TOLOWER", 2, "CBL_TOLOWER"),
    ("\221", 2, "CBL_X91"),
    ("\364", 2, "CBL_XF4"),
    ("\365", 2, "CBL_XF5"),
)

#: external-name -> internal-function-name (the form call.py consumes).
SYSTEM_TABLE = {ext: internal for (ext, _n, internal) in SYSTEM_DEF}

#: external-name -> declared parameter count (exposed for validation/tests).
SYSTEM_PARAM_COUNT = {ext: n for (ext, n, _i) in SYSTEM_DEF}


# ===========================================================================
# SYSTEM - shell command execution (common.c L1855-L1891)
# ===========================================================================
def SYSTEM(cmd):
    """Execute a shell command, returning its exit status (common.c SYSTEM).

    The command text is taken from the current-module procedure parameter
    (matching the C, which reads ``cob_procedure_parameters[0]``); trailing
    blanks/NULs are trimmed.  When the curses screen is active it is suspended
    around the command (``cob_screen_set_mode(0)`` / ``(1)``) and resumed
    afterwards, exactly as the C runtime does.  Returns 1 when the command is
    empty (all blanks).
    """
    param = _proc_param(0)
    if param is None:
        # No procedure-parameter frame: fall back to the direct operand.
        param = cmd
    raw = bytes(_as_buf(param))
    if len(raw) > common.COB_MEDIUM_MAX:
        common.cob_runtime_error(
            "Parameter to SYSTEM call is larger than 8192 characters")
        common.cob_stop_run(1)

    # Trim trailing blanks/NULs (common.c L1864-L1869).
    i = len(raw)
    while i > 0 and raw[i - 1] in (0x20, 0x00):
        i -= 1
    if i <= 0:
        return 1
    command = raw[:i].decode("latin-1")

    # Suspend the managed screen around the external command (common.c L1879).
    screen_active = False
    try:  # pragma: no cover - exercised only with a live curses screen
        from libcob_py import screenio
        if screenio.cob_screen_initialized:
            screen_active = True
            screenio.cob_screen_set_mode(0)
    except Exception:  # pragma: no cover
        screen_active = False

    rc = os.system(command)
    # os.system returns a wait-status; mirror the C ``return system()`` value by
    # extracting the child exit code on POSIX (WEXITSTATUS) where available.
    if os.name == "posix" and os.WIFEXITED(rc):
        rc = os.WEXITSTATUS(rc)

    if screen_active:  # pragma: no cover - live screen only
        from libcob_py import screenio
        screenio.cob_screen_set_mode(1)
    return rc


# ===========================================================================
# Bitwise logical routines (common.c L1892-L2019)
# ===========================================================================
# Each operates byte-for-byte over ``length`` bytes; the AND/OR/NOR/XOR/IMP/NIMP
# /EQ family writes the result into data_2, CBL_NOT complements data_1.  A
# non-positive length is a no-op (the C ``if (length <= 0) return 0`` guard).

def CBL_AND(data_1, data_2, length):
    """Bitwise AND: data_2[n] &= data_1[n] (common.c L1892-L1907)."""
    d1, d2 = _as_buf(data_1), _as_buf(data_2)
    n = _as_int(length)
    for i in range(max(n, 0)):
        d2[i] = d2[i] & d1[i]
    return 0


def CBL_OR(data_1, data_2, length):
    """Bitwise OR: data_2[n] |= data_1[n] (common.c L1908-L1923)."""
    d1, d2 = _as_buf(data_1), _as_buf(data_2)
    n = _as_int(length)
    for i in range(max(n, 0)):
        d2[i] = d2[i] | d1[i]
    return 0


def CBL_NOR(data_1, data_2, length):
    """Bitwise NOR: data_2[n] = ~(data_1[n] | data_2[n]) (common.c L1924-L1939)."""
    d1, d2 = _as_buf(data_1), _as_buf(data_2)
    n = _as_int(length)
    for i in range(max(n, 0)):
        d2[i] = (~(d1[i] | d2[i])) & 0xFF
    return 0


def CBL_XOR(data_1, data_2, length):
    """Bitwise XOR: data_2[n] ^= data_1[n] (common.c L1940-L1955)."""
    d1, d2 = _as_buf(data_1), _as_buf(data_2)
    n = _as_int(length)
    for i in range(max(n, 0)):
        d2[i] = d2[i] ^ d1[i]
    return 0


def CBL_IMP(data_1, data_2, length):
    """Bitwise IMP: data_2[n] = (~data_1[n]) | data_2[n] (common.c L1956-L1971)."""
    d1, d2 = _as_buf(data_1), _as_buf(data_2)
    n = _as_int(length)
    for i in range(max(n, 0)):
        d2[i] = ((~d1[i]) | d2[i]) & 0xFF
    return 0


def CBL_NIMP(data_1, data_2, length):
    """Bitwise NIMP: data_2[n] = data_1[n] & (~data_2[n]) (common.c L1972-L1987)."""
    d1, d2 = _as_buf(data_1), _as_buf(data_2)
    n = _as_int(length)
    for i in range(max(n, 0)):
        d2[i] = (d1[i] & (~d2[i])) & 0xFF
    return 0


def CBL_EQ(data_1, data_2, length):
    """Bitwise EQ: data_2[n] = ~(data_1[n] ^ data_2[n]) (common.c L1988-L2003)."""
    d1, d2 = _as_buf(data_1), _as_buf(data_2)
    n = _as_int(length)
    for i in range(max(n, 0)):
        d2[i] = (~(d1[i] ^ d2[i])) & 0xFF
    return 0


def CBL_NOT(data_1, length):
    """Bitwise NOT: data_1[n] = ~data_1[n] (common.c L2004-L2019)."""
    d1 = _as_buf(data_1)
    n = _as_int(length)
    for i in range(max(n, 0)):
        d1[i] = (~d1[i]) & 0xFF
    return 0


def CBL_XF4(data_1, data_2):
    """Pack 8 low-bits of data_2[0..7] into the single byte data_1 (common.c L2020-L2033).

    ``data_1 = OR over n in 0..7 of (data_2[n] & 1) << (7 - n)``.
    """
    d1, d2 = _as_buf(data_1), _as_buf(data_2)
    val = 0
    for n in range(8):
        val |= (d2[n] & 1) << (7 - n)
    d1[0] = val & 0xFF
    return 0


def CBL_XF5(data_1, data_2):
    """Unpack the byte data_1 into 8 bytes data_2[0..7] (common.c L2034-L2046).

    ``data_2[n] = 1 if (data_1 & (1 << (7 - n))) else 0``.
    """
    d1, d2 = _as_buf(data_1), _as_buf(data_2)
    src = d1[0]
    for n in range(8):
        d2[n] = 1 if (src & (1 << (7 - n))) else 0
    return 0


def CBL_X91(result, func, parm):
    """Program switch / parameter-count service (common.c L2047-L2085).

    ``func`` selects the operation:

    * 11 - set the 8 program switches from ``parm`` (0 -> off, 1 -> on).
    * 12 - read the 8 program switches into ``parm``.
    * 16 - store the saved CALL parameter count into ``parm[0]``.

    ``result[0]`` is set to 0 on success, 1 for an unknown function.
    """
    res, fn, pm = _as_buf(result), _as_buf(func), _as_buf(parm)
    op = fn[0]
    if op == 11:
        for i in range(8):
            v = pm[i]
            if v == 0:
                common.cob_set_switch(i, 0)
            elif v == 1:
                common.cob_set_switch(i, 1)
        res[0] = 0
    elif op == 12:
        for i in range(8):
            pm[i] = 1 if common.cob_get_switch(i) else 0
        res[0] = 0
    elif op == 16:
        pm[0] = common.cob_save_call_params & 0xFF
        res[0] = 0
    else:
        res[0] = 1
    return 0


# ===========================================================================
# Case conversion (common.c L2086-L2119)
# ===========================================================================
def CBL_TOUPPER(data, length):
    """Upper-case ``length`` bytes in place (common.c CBL_TOUPPER).

    Only lower-case ASCII letters are converted, matching the C ``islower``
    guard so non-alphabetic bytes are left untouched byte-for-byte.
    """
    d = _as_buf(data)
    n = _as_int(length)
    for i in range(max(n, 0)):
        if 0x61 <= d[i] <= 0x7A:  # 'a'..'z'
            d[i] -= 0x20
    return 0


def CBL_TOLOWER(data, length):
    """Lower-case ``length`` bytes in place (common.c CBL_TOLOWER).

    Only upper-case ASCII letters are converted (the C ``isupper`` guard).
    """
    d = _as_buf(data)
    n = _as_int(length)
    for i in range(max(n, 0)):
        if 0x41 <= d[i] <= 0x5A:  # 'A'..'Z'
            d[i] += 0x20
    return 0


# ===========================================================================
# CBL_OC_NANOSLEEP (common.c L2120-L2165)
# ===========================================================================
def CBL_OC_NANOSLEEP(data):
    """Sleep for the nanoseconds given by procedure parameter 0 (common.c).

    Reads a long-long nanosecond count from the call frame (the C uses
    ``cob_get_long_long`` - decoded here via :func:`_get_long_long` to preserve
    64-bit fidelity) and sleeps that long when positive; a no-op otherwise.
    """
    param = _proc_param(0)
    if param is None:
        param = data if isinstance(data, common.cob_field) else None
    if param is not None:
        nsecs = _get_long_long(param)
        if nsecs > 0:
            time.sleep(nsecs / 1_000_000_000.0)
    return 0



# ===========================================================================
# Exit / error procedure handler registration (common.c L1777-L1853)
# ===========================================================================
# The C runtime keeps singly-linked lists of registered handlers; the Python
# port keeps equivalent lists of callables.  ``x`` selects install vs remove.
_exit_handlers = []
_error_handlers = []


def _selector_byte(x):
    """Decode the install/remove selector byte ``*x`` of a handler routine.

    The C parameter is ``unsigned char *x`` and the code tests ``*x`` (its first
    byte).  Accepts a memoryview/bytes/bytearray (first byte) or a plain int.
    """
    if isinstance(x, (bytearray, bytes, memoryview)):
        return x[0]
    if isinstance(x, common.cob_field):
        return x.data[0]
    return int(x) & 0xFF


def CBL_EXIT_PROC(x, pptr):
    """Install/remove a program exit handler (common.c CBL_EXIT_PROC).

    *pptr* is the handler - a Python callable (PROGRAM-POINTER value).  Any
    already-registered copy is removed first; then when the selector
    ``*x in {0, 2, 3}`` the handler is (re)installed at the head of the list.
    Returns -1 when no usable handler is supplied (the C ``!p || !*p`` guard).
    """
    if not callable(pptr):
        return -1
    if pptr in _exit_handlers:
        _exit_handlers.remove(pptr)
    if _selector_byte(x) in (0, 2, 3):
        _exit_handlers.insert(0, pptr)
    return 0


def CBL_ERROR_PROC(x, pptr):
    """Install/remove a runtime error handler (common.c CBL_ERROR_PROC).

    Mirrors :func:`CBL_EXIT_PROC` but for the error-handler list; the handler
    is (re)installed only when the selector ``*x == 0``.  Returns -1 when no
    usable handler is supplied.
    """
    if not callable(pptr):
        return -1
    if pptr in _error_handlers:
        _error_handlers.remove(pptr)
    if _selector_byte(x) == 0:
        _error_handlers.insert(0, pptr)
    return 0


# ===========================================================================
# Low-level file-handle routines (fileio.c L4537-L4722)
# ===========================================================================
# These map onto the OS file-descriptor API.  The 4-byte file_handle stores the
# native OS descriptor (C ``memcpy(file_handle, &fd, 4)`` - native byte order).
# file_offset (8 bytes) and file_len (4 bytes) are big-endian on the wire (the C
# code byte-swaps them on little-endian hosts), so struct '>' formats are used.

def _open_cbl_file(file_name, file_access, file_handle, extra_flags):
    """Shared open path for CBL_OPEN_FILE / CBL_CREATE_FILE (fileio.c open_cbl_file).

    Decodes the low 6 bits of ``file_access`` (1=read, 2=create/truncate/write,
    3=read-write), opens the file, and stores the descriptor into the 4-byte
    ``file_handle``.  Returns 0 on success, -1 on a bad access mode / missing
    name, 35 when the open itself fails.
    """
    handle = _as_buf(file_handle)
    if _proc_param(0) is None and file_name is None:
        handle[0:4] = struct.pack("=i", -1)
        return -1

    access = _as_buf(file_access)[0] & 0x3F
    if access == 1:
        flags = os.O_RDONLY
    elif access == 2:
        flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY
    elif access == 3:
        flags = os.O_RDWR
    else:
        handle[0:4] = struct.pack("=i", -1)
        return -1
    flags |= extra_flags
    if hasattr(os, "O_BINARY"):  # pragma: no cover - Windows only
        flags |= os.O_BINARY

    name = _filename_arg(file_name, 0)
    try:
        fd = os.open(name, flags, 0o660)
    except OSError:
        handle[0:4] = struct.pack("=i", -1)
        return 35
    handle[0:4] = struct.pack("=i", fd)
    return 0


def CBL_OPEN_FILE(file_name, file_access, file_lock, file_dev, file_handle):
    """Open an existing file (fileio.c CBL_OPEN_FILE).  file_lock/file_dev unused."""
    return _open_cbl_file(file_name, file_access, file_handle, 0)


def CBL_CREATE_FILE(file_name, file_access, file_lock, file_dev, file_handle):
    """Create/truncate a file (fileio.c CBL_CREATE_FILE)."""
    return _open_cbl_file(file_name, file_access, file_handle,
                          os.O_CREAT | os.O_TRUNC)


def CBL_READ_FILE(file_handle, file_offset, file_len, flags, buf):
    """Read ``file_len`` bytes at ``file_offset`` into ``buf`` (fileio.c CBL_READ_FILE).

    Returns 0 on a successful read, 10 at end-of-file (0 bytes read), -1 on a
    seek/IO error.  When ``flags & 0x80`` the current file size is written back
    into ``file_offset`` (big-endian).
    """
    fd = struct.unpack("=i", bytes(_as_buf(file_handle)[0:4]))[0]
    off = struct.unpack(">q", bytes(_as_buf(file_offset)[0:8]))[0]
    length = struct.unpack(">i", bytes(_as_buf(file_len)[0:4]))[0]
    dst = _as_buf(buf)
    rc = 0
    try:
        os.lseek(fd, off, os.SEEK_SET)
    except OSError:
        return -1
    if length > 0:
        chunk = os.read(fd, length)
        if len(chunk) == 0:
            rc = 10
        else:
            dst[0:len(chunk)] = chunk
            rc = 0
    if _as_buf(flags)[0] & 0x80:
        try:
            size = os.fstat(fd).st_size
        except OSError:
            return -1
        _as_buf(file_offset)[0:8] = struct.pack(">q", size)
    return rc


def CBL_WRITE_FILE(file_handle, file_offset, file_len, flags, buf):
    """Write ``file_len`` bytes from ``buf`` at ``file_offset`` (fileio.c CBL_WRITE_FILE).

    Returns 0 on success, -1 on a seek failure, 30 on a write error.
    """
    fd = struct.unpack("=i", bytes(_as_buf(file_handle)[0:4]))[0]
    off = struct.unpack(">q", bytes(_as_buf(file_offset)[0:8]))[0]
    length = struct.unpack(">i", bytes(_as_buf(file_len)[0:4]))[0]
    src = bytes(_as_buf(buf)[0:max(length, 0)])
    try:
        os.lseek(fd, off, os.SEEK_SET)
    except OSError:
        return -1
    try:
        os.write(fd, src)
    except OSError:
        return 30
    return 0


def CBL_CLOSE_FILE(file_handle):
    """Close the descriptor in ``file_handle`` (fileio.c CBL_CLOSE_FILE)."""
    fd = struct.unpack("=i", bytes(_as_buf(file_handle)[0:4]))[0]
    try:
        os.close(fd)
    except OSError:
        return -1
    return 0


def CBL_FLUSH_FILE(file_handle):
    """No-op flush, always succeeds (fileio.c CBL_FLUSH_FILE returns 0)."""
    return 0


def CBL_DELETE_FILE(file_name):
    """Delete a file by name (fileio.c CBL_DELETE_FILE).

    Returns -1 when no name is supplied, 128 on failure, 0 on success.
    """
    if _proc_param(0) is None and file_name is None:
        return -1
    try:
        os.unlink(_filename_arg(file_name, 0))
    except OSError:
        return 128
    return 0


def CBL_COPY_FILE(fname1, fname2):
    """Copy ``fname1`` to ``fname2`` (fileio.c CBL_COPY_FILE).

    Returns -1 when a name is missing or a file cannot be opened, 0 on success.
    """
    if (_proc_param(0) is None and fname1 is None) or \
       (_proc_param(1) is None and fname2 is None):
        return -1
    src = _filename_arg(fname1, 0)
    dst = _filename_arg(fname2, 1)
    try:
        fd1 = os.open(src, os.O_RDONLY)
    except OSError:
        return -1
    try:
        fd2 = os.open(dst, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o660)
    except OSError:
        os.close(fd1)
        return -1
    ret = 0
    try:
        while True:
            chunk = os.read(fd1, 4096)
            if not chunk:
                break
            os.write(fd2, chunk)
    except OSError:
        ret = -1
    finally:
        os.close(fd1)
        os.close(fd2)
    return ret


def CBL_CHECK_FILE_EXIST(file_name, file_info):
    """Stat a file and fill a 16-byte info block (fileio.c CBL_CHECK_FILE_EXIST).

    Layout: size (big-endian 8) | day | month | year (big-endian 2) | hour |
    minute | second | 0.  Returns 35 when the file does not exist, 0 on success.
    """
    if _proc_param(0) is None and file_name is None:
        return -1
    try:
        st = os.stat(_filename_arg(file_name, 0))
    except OSError:
        return 35
    tm = time.localtime(st.st_mtime)
    info = _as_buf(file_info)
    info[0:8] = struct.pack(">q", st.st_size)
    # MIGRATION (C -> Python): Python's time.struct_time already reports tm_mon
    # as 1-12 and tm_year as the full 4-digit year, whereas C's struct tm uses
    # 0-11 / years-since-1900.  The C source's "tm_mon + 1" and "tm_year + 1900"
    # adjustments are therefore intentionally dropped so the stored values stay
    # byte-for-byte identical (month 1-12, full year).
    info[8] = tm.tm_mday & 0xFF
    info[9] = tm.tm_mon & 0xFF
    info[10:12] = struct.pack(">h", tm.tm_year)
    info[12] = tm.tm_hour & 0xFF
    info[13] = tm.tm_min & 0xFF
    info[14] = tm.tm_sec & 0xFF
    info[15] = 0
    return 0


def CBL_RENAME_FILE(fname1, fname2):
    """Rename ``fname1`` to ``fname2`` (fileio.c CBL_RENAME_FILE).

    Returns -1 when a name is missing, 128 on failure, 0 on success.
    """
    if (_proc_param(0) is None and fname1 is None) or \
       (_proc_param(1) is None and fname2 is None):
        return -1
    try:
        os.rename(_filename_arg(fname1, 0), _filename_arg(fname2, 1))
    except OSError:
        return 128
    return 0


# ===========================================================================
# Directory routines (fileio.c L4724-L4830)
# ===========================================================================
def CBL_GET_CURRENT_DIR(flags, dir_length, directory):
    """Store the current working directory into ``directory`` (fileio.c).

    Returns 128 when ``dir_length < 1`` or the path will not fit, 129 when
    ``flags`` is non-zero, 0 on success.  A path containing a space is wrapped
    in double quotes (matching the C ``has_space`` handling).
    """
    n = _as_int(dir_length)
    if n < 1:
        return 128
    if _as_int(flags):
        return 129
    out = _as_buf(directory)
    for i in range(n):
        out[i] = 0x20  # blank-fill
    cwd = os.getcwd()
    dir_size = len(cwd)
    has_space = 2 if (" " in cwd) else 0
    if dir_size + has_space > n:
        return 128
    enc = cwd.encode("latin-1")
    if has_space:
        out[0] = 0x22  # '"'
        out[1:1 + dir_size] = enc
        out[dir_size + 1] = 0x22
    else:
        out[0:dir_size] = enc
    return 0


def CBL_CREATE_DIR(directory):
    """Create a directory (fileio.c CBL_CREATE_DIR).  -1 no name, 128 fail, 0 ok."""
    if _proc_param(0) is None and directory is None:
        return -1
    try:
        os.mkdir(_filename_arg(directory, 0), 0o770)
    except OSError:
        return 128
    return 0


def CBL_CHANGE_DIR(directory):
    """Change the current directory (fileio.c CBL_CHANGE_DIR)."""
    if _proc_param(0) is None and directory is None:
        return -1
    try:
        os.chdir(_filename_arg(directory, 0))
    except OSError:
        return 128
    return 0


def CBL_DELETE_DIR(directory):
    """Remove a directory (fileio.c CBL_DELETE_DIR)."""
    if _proc_param(0) is None and directory is None:
        return -1
    try:
        os.rmdir(_filename_arg(directory, 0))
    except OSError:
        return 128
    return 0


# ===========================================================================
# ACUCOBOL-style C$ filesystem wrappers - DELEGATED to libcob_py.fileio
# ===========================================================================
# AAP 0.4.1 / agent-prompt: "delegate filesystem C$ routines to libcob_py.fileio
# where they physically live" (their C bodies are in fileio.c L4951-L5063).  The
# emitter passes BY REFERENCE operands as memoryviews and sets up the
# procedure-parameter frame, while the fileio cob_acuw_* routines consume
# cob_field operands; the thin wrappers below bridge the two conventions via
# :func:`_as_field` / :func:`_status_field` and then call into fileio, so there
# is a single authoritative filesystem implementation (DRY) and system.py is the
# CALL-dispatch entry layer.  C$JUSTIFY / C$GETPID / C$NARG / C$PARAMSIZE /
# C$SLEEP are re-exported from common (their C bodies live in common.c).

def cob_acuw_mkdir(directory):
    """C$MAKEDIR - create a directory (delegates to fileio.cob_acuw_mkdir)."""
    return fileio.cob_acuw_mkdir(_as_field(directory, 0))


def cob_acuw_chdir(directory, status):
    """C$CHDIR - change directory, store the result into the status field.

    Delegates to fileio.cob_acuw_chdir; the status code is written into the
    call-frame field ``cob_procedure_parameters[1]`` (the C behaviour),
    resolved by :func:`_status_field`.
    """
    return fileio.cob_acuw_chdir(_as_field(directory, 0),
                                 _status_field(status, 1))


def cob_acuw_copyfile(fname1, fname2, file_type):
    """C$COPY - copy a file (delegates to fileio.cob_acuw_copyfile).

    ``file_type`` is forwarded unchanged; as in the C runtime it is not yet
    evaluated.  fileio enforces the 3-parameter requirement (returns 128 when
    ``cob_call_params`` < 3).
    """
    return fileio.cob_acuw_copyfile(_as_field(fname1, 0),
                                    _as_field(fname2, 1), file_type)


def cob_acuw_file_info(file_name, file_info):
    """C$FILEINFO - stat a file into a 16-byte block (delegates to fileio).

    fileio.cob_acuw_file_info writes the 16-byte ``size | YYYYMMDD | HHMMSSss``
    block into a cob_field's ``.data``; a transient field receives it and the
    bytes are copied back into the caller's OUTPUT buffer (slice-safe for the
    emitter's ``memoryview(b_N)[offset:]`` operands).  Returns fileio's status
    (0 success, 35 missing, 128 bad parameters).
    """
    temp = common.cob_field(size=16, data=bytearray(16), attr=_ALNUM_ATTR)
    rc = fileio.cob_acuw_file_info(_as_field(file_name, 0), temp)
    if rc == 0:
        out = _as_buf(file_info)
        out[0:16] = bytes(temp.data[0:16])
    return rc


def cob_acuw_file_delete(file_name, file_type):
    """C$DELETE - delete a file (delegates to fileio.cob_acuw_file_delete).

    ``file_type`` is forwarded unchanged (not yet evaluated, as in the C).
    """
    return fileio.cob_acuw_file_delete(_as_field(file_name, 0), file_type)


# ===========================================================================
# Subsystem initialisation
# ===========================================================================
def cob_init_system():
    """Initialise the system-routine subsystem.

    The dispatch table is static data, so initialisation only resets the
    handler lists; :mod:`libcob_py.call` performs the actual name registration
    from :data:`SYSTEM_TABLE` during ``cob_init_call``.
    """
    _exit_handlers.clear()
    _error_handlers.clear()

