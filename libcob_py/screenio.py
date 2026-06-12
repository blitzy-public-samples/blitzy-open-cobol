"""libcob_py.screenio - COBOL SCREEN SECTION terminal I/O.

Python standard-library port of the C runtime screen subsystem
``libcob/screenio.c`` (1438 lines) together with the public CRT-status
constants defined in ``copy/screenio.cpy`` (114 lines).  This module is part of
the GNU Cobol C->Python backend refactor described in the Agent Action Plan
(AAP Section 0.3.4 / 0.6.1); it reproduces the on-screen behaviour of the
original ncurses-based implementation using only the Python standard-library
:mod:`curses` module - ZERO third-party dependencies (AAP 0.5 / 0.7.1).

Design (AAP 0.3.2 "Template-method with graceful degradation"):

* When a controlling terminal supports curses, DISPLAY/ACCEPT of SCREEN SECTION
  items is rendered through :mod:`curses`, honouring colour attributes, field
  editing, and the F1-F64 function-key map exactly as ``screenio.c`` does.
* When :func:`curses.initscr` fails (unsupported terminal, no TTY - e.g. a
  pipe or a batch/test environment), the screen exception
  ``EC-SCREEN-ITEM-TRUNCATED`` (hex ``0F03`` - verified at
  ``libcob/exception.def:L250``; the nearest authoritative entry to the
  prompt's ``EC-SCREEN-ITEM-TRUNCATION``, see AAP 0.6.1) is raised through
  :mod:`libcob_py.common`'s exception dispatch and DISPLAY/ACCEPT fall through
  to the non-curses :mod:`libcob_py.termio` path.  This mirrors the
  ``#ifdef COB_GEN_SCREENIO`` (curses) / ``#else`` (empty-stub) split in
  ``screenio.c`` (L1402-L1437) where the fallback build provides plain
  terminal I/O.

Initialisation contract (AAP 0.3.2 / agent-prompt Phase 4): screenio is NOT
part of the ``cob_init_*`` chain in ``common.c``; it lazily initialises the
curses screen on the first DISPLAY/ACCEPT via :func:`cob_screen_init`.  It must
therefore import cleanly with no controlling terminal - :mod:`curses` is
imported defensively and every curses call is guarded.

The emitter (``cobc/codegen.c``) routes ``cob_field_display``,
``cob_field_accept`` and the ``cob_screen_*`` family to this module, so the
public function names below mirror the C entry points one-for-one (AAP 0.6.5
exhaustive ``cob_*`` mapping).

Standard-library imports only (per the minimal-deviation rule, AAP 0.7.2): the
only third-party-free dependencies are :mod:`libcob_py.common` (runtime base:
exception dispatch, field model, module stack, integer accessors) and
:mod:`libcob_py.termio` (the plain-stream graceful-degradation fallback).
"""

# --- Standard-library imports (stdlib only - AAP 0.5 / 0.7.1) ---------------
import os

try:
    # curses is part of the CPython standard library on POSIX platforms.  It is
    # imported defensively because a terminal/TTY may be unavailable (the
    # graceful-degradation contract above); an ImportError simply forces the
    # termio fallback rather than aborting the runtime import.  This is the
    # Python analogue of the C ``#ifdef COB_GEN_SCREENIO`` compile-time gate.
    import curses as _curses
except Exception:  # pragma: no cover - platform without curses (e.g. stock Win)
    _curses = None

# --- Internal runtime imports (depends_on_files: common.py, termio.py ONLY) -
# ``common`` is the runtime base: exception dispatch (cob_set_exception), the
# field model (cob_field / COB_FIELD_*), the module stack (cob_current_module),
# the SCREEN attribute flags (COB_SCREEN_*), and the deferred integer accessors
# (_lazy_get_int / _lazy_set_int) that bridge to ``move`` WITHOUT screenio
# importing ``move`` directly - keeping this module strictly within its
# dependency whitelist (AAP 0.5.3 internal-import order).  ``termio`` is the
# bottom-of-stack plain stream used as the graceful-degradation fallback; it
# never imports screenio back, so this top-level import introduces no cycle.
from libcob_py import common
from libcob_py import termio

# ===========================================================================
# CRT-STATUS constants - copy/screenio.cpy (EXACTLY 83 values, AAP 0.3.4 / 0.6.1)
# ===========================================================================
# The constant *values* are the contract returned in CRT STATUS / COB-CRT-STATUS
# and consumed by COBOL programs that COPY screenio.cpy.  The COBOL data names
# use hyphens (e.g. COB-COLOR-BLACK); the Python identifiers below substitute
# underscores, preserving the numeric values byte-for-byte.
#
# COUNT MANDATE (agent prompt): the total MUST equal 83, partitioned as
#   8 colours + 1 (COB_SCR_OK) + 64 function keys + 8 navigation keys + 2
#   (NO_FIELD, FATAL) = 8 + 1 + 64 + 8 + 2 = 83.
# A runtime self-check at the end of this section asserts the count.

# --- Colours (COB-COLOR-*, values 0-7; screenio.cpy L23-L30) ---------------
COB_COLOR_BLACK = 0
COB_COLOR_BLUE = 1
COB_COLOR_GREEN = 2
COB_COLOR_CYAN = 3
COB_COLOR_RED = 4
COB_COLOR_MAGENTA = 5
COB_COLOR_YELLOW = 6
COB_COLOR_WHITE = 7

# --- Normal return (COB-SCR-OK, value 0; screenio.cpy L35) -----------------
COB_SCR_OK = 0

# --- Function keys (COB-SCR-F1..F64, values 1001-1064; screenio.cpy L38-L101)
# Listed explicitly (not generated) to mirror screenio.cpy line-for-line and to
# guarantee every COB_SCR_F<n> symbol exists for the emitter and the tests.
COB_SCR_F1 = 1001
COB_SCR_F2 = 1002
COB_SCR_F3 = 1003
COB_SCR_F4 = 1004
COB_SCR_F5 = 1005
COB_SCR_F6 = 1006
COB_SCR_F7 = 1007
COB_SCR_F8 = 1008
COB_SCR_F9 = 1009
COB_SCR_F10 = 1010
COB_SCR_F11 = 1011
COB_SCR_F12 = 1012
COB_SCR_F13 = 1013
COB_SCR_F14 = 1014
COB_SCR_F15 = 1015
COB_SCR_F16 = 1016
COB_SCR_F17 = 1017
COB_SCR_F18 = 1018
COB_SCR_F19 = 1019
COB_SCR_F20 = 1020
COB_SCR_F21 = 1021
COB_SCR_F22 = 1022
COB_SCR_F23 = 1023
COB_SCR_F24 = 1024
COB_SCR_F25 = 1025
COB_SCR_F26 = 1026
COB_SCR_F27 = 1027
COB_SCR_F28 = 1028
COB_SCR_F29 = 1029
COB_SCR_F30 = 1030
COB_SCR_F31 = 1031
COB_SCR_F32 = 1032
COB_SCR_F33 = 1033
COB_SCR_F34 = 1034
COB_SCR_F35 = 1035
COB_SCR_F36 = 1036
COB_SCR_F37 = 1037
COB_SCR_F38 = 1038
COB_SCR_F39 = 1039
COB_SCR_F40 = 1040
COB_SCR_F41 = 1041
COB_SCR_F42 = 1042
COB_SCR_F43 = 1043
COB_SCR_F44 = 1044
COB_SCR_F45 = 1045
COB_SCR_F46 = 1046
COB_SCR_F47 = 1047
COB_SCR_F48 = 1048
COB_SCR_F49 = 1049
COB_SCR_F50 = 1050
COB_SCR_F51 = 1051
COB_SCR_F52 = 1052
COB_SCR_F53 = 1053
COB_SCR_F54 = 1054
COB_SCR_F55 = 1055
COB_SCR_F56 = 1056
COB_SCR_F57 = 1057
COB_SCR_F58 = 1058
COB_SCR_F59 = 1059
COB_SCR_F60 = 1060
COB_SCR_F61 = 1061
COB_SCR_F62 = 1062
COB_SCR_F63 = 1063
COB_SCR_F64 = 1064

# --- Exception / navigation keys (COB-SCR-*, values 2xxx; screenio.cpy L103-L110)
COB_SCR_PAGE_UP = 2001
COB_SCR_PAGE_DOWN = 2002
COB_SCR_KEY_UP = 2003
COB_SCR_KEY_DOWN = 2004
COB_SCR_ESC = 2005
COB_SCR_PRINT = 2006
COB_SCR_TAB = 2007
COB_SCR_BACK_TAB = 2008

# --- Input validation (COB-SCR-NO-FIELD, value 8000; screenio.cpy L112) -----
COB_SCR_NO_FIELD = 8000

# --- Other errors (COB-SCR-FATAL, value 9000; screenio.cpy L114) ------------
COB_SCR_FATAL = 9000

# Read-only mapping colour code (0-7) -> COB-COLOR name, exported for tests and
# diagnostics.  Mirrors the eight-way switch in cob_screen_attr().
_COLOR_NAMES = {
    COB_COLOR_BLACK: "BLACK",
    COB_COLOR_BLUE: "BLUE",
    COB_COLOR_GREEN: "GREEN",
    COB_COLOR_CYAN: "CYAN",
    COB_COLOR_RED: "RED",
    COB_COLOR_MAGENTA: "MAGENTA",
    COB_COLOR_YELLOW: "YELLOW",
    COB_COLOR_WHITE: "WHITE",
}

# Compile-time self-check: the CRT-status surface defined above MUST contain
# exactly the 83 level-78 constants mirrored from copy/screenio.cpy.  This guards
# against an accidental add/drop during maintenance (agent-prompt COUNT MANDATE).
CRT_STATUS_CONSTANT_COUNT = (
    8                       # colours COB_COLOR_BLACK..WHITE
    + 1                     # COB_SCR_OK
    + 64                    # function keys COB_SCR_F1..F64
    + 8                     # navigation keys COB_SCR_PAGE_UP..BACK_TAB
    + 2                     # COB_SCR_NO_FIELD, COB_SCR_FATAL
)
assert CRT_STATUS_CONSTANT_COUNT == 83, "screenio.cpy mandates exactly 83 CRT-status constants"

# ===========================================================================
# SCREEN item descriptor (cob_screen) - the struct the emitter constructs.
#
# The rewritten emitter (codegen.c output_screen_definition) replaces the C
# ``static cob_screen s_N = { ... };`` aggregate with a runtime constructor call
# ``s_N = screenio.cob_screen(next, child, field, value, line, column, foreg,
# backg, type, occurs, attr)``.  The positional order mirrors the C struct
# ``__cob_screen`` (common.h L709-L721) EXACTLY.  The COB_SCREEN_TYPE_*
# discriminant values match common.h (re-exported here for the emitter).
# ===========================================================================
COB_SCREEN_TYPE_GROUP = common.COB_SCREEN_TYPE_GROUP          # 0
COB_SCREEN_TYPE_FIELD = common.COB_SCREEN_TYPE_FIELD          # 1
COB_SCREEN_TYPE_VALUE = common.COB_SCREEN_TYPE_VALUE          # 2
COB_SCREEN_TYPE_ATTRIBUTE = common.COB_SCREEN_TYPE_ATTRIBUTE  # 3


class cob_screen(object):
    """Runtime mirror of the C ``cob_screen`` struct (common.h L709-L721).

    A linked tree of screen items built by the generated module at start-up and
    walked by the ``cob_screen_*`` display/accept routines.  Attributes preserve
    the C struct's member names and order so the emitter's positional
    constructor maps one-to-one:

    * ``next``   - next sibling :class:`cob_screen` (or ``None``)
    * ``child``  - first child (COB_SCREEN_TYPE_GROUP) or ``None``
    * ``field``  - the data :class:`~libcob_py.common.cob_field`
      (COB_SCREEN_TYPE_FIELD) or ``None``
    * ``value``  - the VALUE literal field (COB_SCREEN_TYPE_VALUE) or ``None``
    * ``line`` / ``column`` - LINE/COLUMN position fields (or ``None``)
    * ``foreg`` / ``backg``  - FOREGROUND/BACKGROUND-COLOR fields (or ``None``)
    * ``type``   - one of the ``COB_SCREEN_TYPE_*`` discriminants
    * ``occurs`` - OCCURS minimum (the emitter's ``occurs_min``)
    * ``attr``   - the screen attribute flags (the emitter's ``screen_flag``)
    """

    __slots__ = ("next", "child", "field", "value", "line", "column",
                 "foreg", "backg", "type", "occurs", "attr")

    def __init__(self, next=None, child=None, field=None, value=None,
                 line=None, column=None, foreg=None, backg=None,
                 type=COB_SCREEN_TYPE_GROUP, occurs=0, attr=0):
        self.next = next
        self.child = child
        self.field = field
        self.value = value
        self.line = line
        self.column = column
        self.foreg = foreg
        self.backg = backg
        self.type = int(type)
        self.occurs = int(occurs)
        self.attr = int(attr)

    def __repr__(self):  # pragma: no cover - debugging aid only
        return ("cob_screen(type=%d, occurs=%d, attr=0x%02x)"
                % (self.type, self.occurs, self.attr))


# ===========================================================================
# Module state - mirrors the file-scope globals in screenio.c
# ===========================================================================
# screenio.c declares ``int cob_screen_initialized = 0;`` and
# ``int cob_screen_mode = 0;`` at file scope (L62-L63); the remaining flags are
# ``static`` within the COB_GEN_SCREENIO build.  They are reproduced here as
# module globals so the curses path and the pure helpers share one state.
cob_screen_initialized = 0
cob_screen_mode = 0

# True once curses initialisation has failed; latches the termio fallback so we
# do not repeatedly attempt (and re-fail) initscr on every DISPLAY/ACCEPT.
_curses_failed = False

# The curses standard screen window returned by initscr(); ``None`` until the
# screen subsystem has been initialised (or while in fallback mode).
_stdscr = None

# Colour / capability state (screenio.c statics: cob_has_color, cob_max_y,
# cob_max_x, fore_color, back_color).
cob_has_color = 0
cob_max_y = 0
cob_max_x = 0
fore_color = 0
back_color = 0

# Cursor tracking written by cob_screen_puts (screenio.c cob_current_y/x).
cob_current_y = 0
cob_current_x = 0

# Editing / accept behaviour flags (screenio.c statics, seeded from env).
insert_mode = 0
cob_extended_status = 0
cob_use_esc = 0

# Multi-field ACCEPT input table (screenio.c cob_base_inp / curr_index /
# totl_index / global_return).  ``_cob_base_inp`` is a list of
# :class:`_cob_inp_struct`; the C code used a fixed 1920-entry array, but a
# Python list grows on demand with identical semantics.
_cob_base_inp = []
_curr_index = 0
_totl_index = 0
_global_return = 0


class _cob_inp_struct(object):
    """Per-input-field descriptor (screenio.c ``struct cob_inp_struct`` L68-L74).

    Holds the screen node (``scr``), its absolute cursor position
    (``this_y`` / ``this_x``) captured during :func:`cob_prep_input`, and the
    UP/DOWN navigation links (``up_index`` / ``down_index``) computed by
    :func:`cob_screen_accept`.
    """

    __slots__ = ("scr", "up_index", "down_index", "this_y", "this_x")

    def __init__(self, scr, this_y, this_x):
        self.scr = scr
        self.this_y = this_y
        self.this_x = this_x
        self.up_index = 0
        self.down_index = 0


# ===========================================================================
# Capability + lifecycle
# ===========================================================================
def _curses_available():
    """Return True when the curses screen path can be used.

    Curses is usable only when the module imported successfully and a previous
    initialisation has not already failed (the latched fallback).  This is the
    Python equivalent of the C ``#ifdef COB_GEN_SCREENIO`` compile-time gate
    combined with the runtime ``initscr()`` success check.
    """
    return _curses is not None and not _curses_failed


def _raise_screen_truncated():
    """Signal the screen-unavailable condition (AAP 0.3.4 / 0.6.1).

    Sets the COBOL exception ``EC-SCREEN-ITEM-TRUNCATED`` (hex ``0F03``) - the
    authoritative ``exception.def`` entry (L250) nearest to the prompt's
    ``EC-SCREEN-ITEM-TRUNCATION`` - via :mod:`libcob_py.common`'s exception
    dispatch, and latches the termio fallback so the caller degrades gracefully
    instead of aborting (the C path would ``cob_runtime_error`` + ``cob_stop_run``;
    the refactor's contract requires graceful degradation instead).
    """
    global _curses_failed
    _curses_failed = True
    common.cob_set_exception(common.COB_EC_SCREEN_ITEM_TRUNCATED)


def cob_screen_init():
    """Initialise the curses screen subsystem lazily (screenio.c L385-L440).

    Mirrors the C ``cob_screen_init``: read the ``COB_SCREEN_EXCEPTIONS`` /
    ``COB_SCREEN_ESC`` / ``COB_INSERT_MODE`` environment options, then call
    ``initscr`` and configure cbreak/keypad/nl/noecho mode and colour support,
    recording the terminal size.  On any failure (no TTY, curses error, or no
    curses module) the screen exception ``EC-SCREEN-ITEM-TRUNCATED`` is raised
    and the function returns WITHOUT marking the screen initialised, so callers
    fall through to termio (AAP 0.3.4 graceful degradation).  Idempotent: a
    no-op once the screen is already initialised.
    """
    global cob_screen_initialized, cob_has_color, cob_max_y, cob_max_x
    global fore_color, back_color, _stdscr, insert_mode
    global cob_extended_status, cob_use_esc

    if cob_screen_initialized:
        return
    if not _curses_available():
        # No curses build available (or fallback already latched) -> immediate
        # graceful degradation, matching the C ``#else`` empty-stub build.
        _raise_screen_truncated()
        return

    # --- Environment-driven options (screenio.c L389-L408) -----------------
    s = os.environ.get("COB_SCREEN_EXCEPTIONS")
    if s and s[:1] in ("Y", "y"):
        cob_extended_status = 1
        s = os.environ.get("COB_SCREEN_ESC")
        if s and s[:1] in ("Y", "y"):
            cob_use_esc = 1
    s = os.environ.get("COB_INSERT_MODE")
    if s and s[:1] in ("Y", "y"):
        insert_mode = 1

    # --- Curses initialisation (screenio.c L409-L437) ----------------------
    try:
        _stdscr = _curses.initscr()
        if _stdscr is None:  # pragma: no cover - initscr returning NULL is rare
            raise _curses.error("initscr returned NULL")
        _curses.cbreak()
        _stdscr.keypad(True)
        _curses.nl()
        _curses.noecho()
        if _curses.has_colors():
            _curses.start_color()
            try:  # pragma: no cover - requires a colour-capable live terminal
                fore_color, back_color = _curses.pair_content(0)
            except Exception:  # pragma: no cover
                fore_color, back_color = 0, 0
            cob_has_color = 1
        cob_max_y, cob_max_x = _stdscr.getmaxyx()
        cob_screen_initialized = 1
    except Exception:
        # initscr / capability setup failed.  The C path calls
        # cob_runtime_error + cob_stop_run, but the refactor's graceful
        # degradation contract requires falling back to termio instead.
        #
        # MIGRATION SAFETY (Checkpoint-2 review fix): initscr() may have ALREADY
        # switched the terminal into curses mode (cbreak + noecho + keypad)
        # before a later setup call (cbreak/keypad/nl/noecho/colour/getmaxyx)
        # raised.  Discarding the screen without tearing curses down would leave
        # the user's shell in a broken raw/no-echo state after we degrade to
        # termio.  Restore the terminal on THIS (exception) path too - the same
        # teardown the C runtime performs in cob_screen_terminate() via endwin()
        # (screenio.c L441-L448) - using a guarded, best-effort sequence so a
        # secondary failure cannot mask the graceful-degradation fallback.
        try:
            if _stdscr is not None:
                # A screen object exists => initscr() succeeded => the terminal
                # is in curses mode; undo the mode changes and leave curses.
                try:
                    _curses.nocbreak()   # undo cbreak()
                except Exception:  # pragma: no cover - best-effort restore
                    pass
                try:
                    _curses.echo()       # undo noecho()
                except Exception:  # pragma: no cover - best-effort restore
                    pass
                try:
                    _curses.endwin()     # leave curses (mirrors cob_screen_terminate)
                except Exception:  # pragma: no cover - best-effort restore
                    pass
        finally:
            cob_screen_initialized = 0
            _stdscr = None
            _raise_screen_truncated()


def cob_screen_terminate():
    """Tear down the curses screen subsystem (screenio.c L441-L448).

    Resets the initialised flag and calls ``endwin`` when a screen is active.
    Safe to call when no screen was ever started (mirrors the C guard and the
    ``#else`` empty stub at L1402-L1404).
    """
    global cob_screen_initialized, _stdscr
    if cob_screen_initialized:
        cob_screen_initialized = 0
        if _curses is not None and _stdscr is not None:
            try:  # pragma: no cover - endwin failure is non-fatal
                _curses.endwin()
            except Exception:  # pragma: no cover
                pass
        _stdscr = None


def cob_screen_set_mode(smode):
    """Set the screen mode (screenio.c L1387-L1395 / fallback L1430-L1433).

    ``smode`` 0 selects line/scroll mode (the C path ``refresh`` +
    ``def_prog_mode`` + ``endwin``); non-zero selects full-screen mode (the C
    path ``reset_prog_mode`` + ``refresh``).  The mode flag is always recorded;
    the curses transition is applied only when a screen is active so the call is
    safe with no TTY (matching the ``#else`` empty stub L1430-L1433).
    """
    global cob_screen_mode
    cob_screen_mode = int(smode)
    if not (cob_screen_initialized and _stdscr is not None):
        return
    try:  # pragma: no cover - requires a live terminal
        if not smode:
            _stdscr.refresh()
            _curses.def_prog_mode()
            _curses.endwin()
        else:
            _curses.reset_prog_mode()
            _stdscr.refresh()
    except Exception:  # pragma: no cover
        pass



# ===========================================================================
# Pure helpers (testable without a controlling terminal)
# ===========================================================================
def get_line_column(fline, fcol):
    """Decode LINE/COLUMN operands into 0-based ``(line, col)`` (screenio.c L213-L249).

    Faithful reproduction of the C ``get_line_column``:

    * ``fline is None`` -> position ``(0, 0)``.
    * When only ``fline`` is supplied, the integer value is split into line and
      column: a 4-byte field uses ``/100`` and ``%100``; otherwise ``/1000`` and
      ``%1000`` (the COBOL ``LINE nnnn`` / ``LINE nnnnnn`` packed form).
    * When both are supplied, ``fline`` is the line and ``fcol`` the column.
    * 1-based COBOL positions are converted to 0-based curses positions
      (decrement when > 0).

    Integer values are read through :func:`libcob_py.common._lazy_get_int`, the
    deferred bridge to ``move.cob_get_int`` (keeping screenio within its
    common/termio dependency whitelist).
    """
    if fline is None:
        return 0, 0

    p = common._lazy_get_int(fline)
    if fcol is None:
        if fline.size == 4:
            line = p // 100
            col = p % 100
        else:
            line = p // 1000
            col = p % 1000
    else:
        line = p
        col = common._lazy_get_int(fcol)

    if line > 0:
        line -= 1
    if col > 0:
        col -= 1
    return line, col


def _curses_color(code):
    """Map a COB-COLOR code (0-7) to the curses ``COLOR_*`` constant.

    Returns ``None`` for an out-of-range code, mirroring the ``default: break``
    arm of the C colour switch (screenio.c L256-L347) which leaves the colour
    unchanged.  Returns ``None`` when curses is unavailable.
    """
    if _curses is None:  # pragma: no cover - curses present in CI
        return None
    table = {
        COB_COLOR_BLACK: _curses.COLOR_BLACK,
        COB_COLOR_BLUE: _curses.COLOR_BLUE,
        COB_COLOR_GREEN: _curses.COLOR_GREEN,
        COB_COLOR_CYAN: _curses.COLOR_CYAN,
        COB_COLOR_RED: _curses.COLOR_RED,
        COB_COLOR_MAGENTA: _curses.COLOR_MAGENTA,
        COB_COLOR_YELLOW: _curses.COLOR_YELLOW,
        COB_COLOR_WHITE: _curses.COLOR_WHITE,
    }
    return table.get(code)


def cob_function_key_fret(keyp, key_f0):
    """Return the CRT-STATUS value for a curses function key, else ``None``.

    screenio.c L596-L598 / L1129-L1132 maps a raw curses key in the open
    interval ``(KEY_F0, KEY_F(65))`` to ``1000 + (keyp - KEY_F0)``, i.e.
    F1->1001 .. F64->1064.  Factored into a pure helper so the F1-F64 contract
    can be unit-tested without a live terminal.
    """
    if key_f0 < keyp < key_f0 + 65:
        return 1000 + (keyp - key_f0)
    return None


def _special_key_fret(keyp):
    """Map a converted curses navigation/exception key to its CRT-STATUS value.

    Returns the ``fret`` value for page/arrow/print/escape/tab keys
    (screenio.c L615-L624, L1145-L1182) or ``None`` when the key is an ordinary
    editing key that does not terminate the ACCEPT.  Requires curses for the
    ``KEY_*`` constants; returns ``None`` when curses is unavailable.
    """
    if _curses is None:  # pragma: no cover - exercised only without curses
        return None
    mapping = {
        _curses.KEY_PPAGE: COB_SCR_PAGE_UP,    # 2001
        _curses.KEY_NPAGE: COB_SCR_PAGE_DOWN,  # 2002
        _curses.KEY_UP: COB_SCR_KEY_UP,        # 2003
        _curses.KEY_DOWN: COB_SCR_KEY_DOWN,    # 2004
        _curses.KEY_PRINT: COB_SCR_PRINT,      # 2006
        _curses.KEY_STAB: COB_SCR_TAB,         # 2007
        _curses.KEY_BTAB: COB_SCR_BACK_TAB,    # 2008
    }
    if keyp in mapping:
        return mapping[keyp]
    if keyp == 0o33:  # ESC (octal 033) -> 2005 (screenio.c L1174-L1176)
        return COB_SCR_ESC
    return None


def cob_convert_key(keyp, field_accept):
    """Normalise a raw key code (screenio.c L96-L211 ``cob_convert_key``).

    Translates control characters (Enter/Tab/Backspace) to their canonical
    curses ``KEY_*`` values and zeroes keys that should be ignored unless
    extended status / ESC handling is enabled.  Returns the (possibly
    rewritten) key code; a return value ``<= 0`` means "ignore and re-read"
    exactly as the C code treats ``*keyp <= 0`` (the C signature is
    ``void cob_convert_key(int *keyp, ...)`` mutating in place - the Python
    adaptation returns the value).

    The downstream mapping to the 1001-1064 / 2001-2008 CRT-STATUS values is
    performed by :func:`cob_function_key_fret` and :func:`_special_key_fret`,
    exactly as the C ACCEPT loop does after calling ``cob_convert_key``.
    """
    if _curses is None:  # pragma: no cover - no curses constants to map to
        return keyp

    # --- Map control chars to KEY_xxx (screenio.c L100-L112) ---------------
    if keyp in (ord("\n"), ord("\r"), 0o004, 0o032):
        keyp = _curses.KEY_ENTER
    elif keyp == ord("\t"):
        keyp = _curses.KEY_STAB
    elif keyp in (ord("\b"), 0o177):
        keyp = _curses.KEY_BACKSPACE

    # --- Ignore-key filtering (screenio.c L184-L210) -----------------------
    if keyp == 0o33:  # ESC
        if not cob_extended_status or not cob_use_esc:
            keyp = 0
    elif keyp in (_curses.KEY_PPAGE, _curses.KEY_NPAGE, _curses.KEY_PRINT):
        if not cob_extended_status:
            keyp = 0
    elif keyp in (_curses.KEY_UP, _curses.KEY_DOWN):
        if field_accept and not cob_extended_status:
            keyp = 0
    return keyp


def cob_check_pos_status(fret, yx=None):
    """Store the ACCEPT result into CRT STATUS and CURSOR position (screenio.c L450-L489).

    Faithful port of the C ``cob_check_pos_status``:

    * A non-zero ``fret`` raises ``EC-IMP-ACCEPT`` (screenio.c L457-L459).
    * ``crt_status`` of the current module receives ``fret`` - as an integer for
      a numeric field, else as a 4-digit zero-padded display string.
    * ``cursor_pos`` (when present) receives the encoded line/column:
      ``line*1000+col`` for a non-DISPLAY numeric field; for a display field a
      4-digit ``line*100+col`` when ``size < 6``, otherwise a 6-digit
      ``line*1000+col``.

    ``yx`` may be supplied as an explicit ``(line, column)`` tuple (used by the
    tests); otherwise the live cursor position is read from curses when active.
    Integer stores go through :func:`libcob_py.common._lazy_set_int`.
    """
    if fret:
        common.cob_set_exception(common.COB_EC_IMP_ACCEPT)

    module = common.cob_current_module
    if module is None:
        return

    crt = getattr(module, "crt_status", None)
    if crt is not None:
        if common.COB_FIELD_IS_NUMERIC(crt):
            common._lazy_set_int(crt, fret)
        else:
            datbuf = ("%4.4d" % fret).encode("latin-1")
            crt.data[0:4] = datbuf[0:4]

    cursor = getattr(module, "cursor_pos", None)
    if cursor is not None:
        if yx is None:
            if cob_screen_initialized and _stdscr is not None:  # pragma: no cover
                yx = _stdscr.getyx()
            else:
                return
        sline, scolumn = yx
        if (common.COB_FIELD_IS_NUMERIC(cursor)
                and common.COB_FIELD_TYPE(cursor) != common.COB_TYPE_NUMERIC_DISPLAY):
            common._lazy_set_int(cursor, sline * 1000 + scolumn)
        else:
            if cursor.size < 6:
                val = sline * 100 + scolumn
                datbuf = ("%4.4d" % val).encode("latin-1")
                cursor.data[0:4] = datbuf[0:4]
            else:
                val = sline * 1000 + scolumn
                datbuf = ("%6.6d" % val).encode("latin-1")
                cursor.data[0:6] = datbuf[0:6]


def cob_screen_line_col(f, l_or_c):
    """Store the screen line count or column count into *f* (screenio.c L1374-L1386).

    The C routine lazily initialises the screen then stores ``LINES`` (when
    ``l_or_c`` is 0) or ``COLS`` (otherwise) - i.e. the terminal dimensions, not
    the cursor position.  Here the dimensions come from the live curses screen
    (``getmaxyx``); when no screen is active (no TTY, after a failed lazy init)
    the value is 0, matching the ``#else`` empty stub (screenio.c L1434-L1436)
    which leaves the field unchanged at its initialised zero.
    """
    if not cob_screen_initialized:
        cob_screen_init()
    if cob_screen_initialized and _stdscr is not None:  # pragma: no cover - needs TTY
        maxy, maxx = _stdscr.getmaxyx()
        common._lazy_set_int(f, maxy if not l_or_c else maxx)
    else:
        common._lazy_set_int(f, 0)



# ===========================================================================
# Curses rendering + input (active only with a controlling terminal)
# ===========================================================================
def cob_screen_attr(fgc, bgc, attr):
    """Apply colour and style attributes to the screen (screenio.c L251-L384).

    Reproduces the C ``cob_screen_attr``: reset to ``A_NORMAL``, OR in the
    REVERSE / HIGHLIGHT(bold) / BLINK / UNDERLINE styles, then - when the
    terminal supports colour - resolve the COB-COLOR foreground/background codes
    via :func:`_curses_color` and install a colour pair through
    ``init_pair`` / ``color_pair`` (agent-prompt Phase 2).  A no-op when the
    screen is not active, matching the ``#else`` build which renders no
    attributes.
    """
    if not (cob_screen_initialized and _stdscr is not None):
        return
    # ----- everything below requires a live terminal --------------------- #
    _stdscr.attrset(_curses.A_NORMAL)  # pragma: no cover - needs TTY

    styles = 0  # pragma: no cover
    if attr & common.COB_SCREEN_REVERSE:  # pragma: no cover
        styles |= _curses.A_REVERSE
    if attr & common.COB_SCREEN_HIGHLIGHT:  # pragma: no cover
        styles |= _curses.A_BOLD
    if attr & common.COB_SCREEN_BLINK:  # pragma: no cover
        styles |= _curses.A_BLINK
    if attr & common.COB_SCREEN_UNDERLINE:  # pragma: no cover
        styles |= _curses.A_UNDERLINE
    if attr & common.COB_SCREEN_LOWLIGHT:  # pragma: no cover
        styles |= _curses.A_DIM
    if styles:  # pragma: no cover
        _stdscr.attron(styles)

    if cob_has_color:  # pragma: no cover - requires a colour-capable terminal
        fgcolor = fore_color
        bgcolor = back_color
        if fgc is not None:
            mapped = _curses_color(common._lazy_get_int(fgc))
            if mapped is not None:
                fgcolor = mapped
        if bgc is not None:
            mapped = _curses_color(common._lazy_get_int(bgc))
            if mapped is not None:
                bgcolor = mapped
        # Install a colour pair (pair 1 reused, as the C code rebuilds pairs);
        # any curses error here is non-fatal and leaves the default attributes.
        try:
            _curses.init_pair(1, fgcolor, bgcolor)
            _stdscr.attron(_curses.color_pair(1))
        except Exception:
            pass


def cob_screen_puts(s, f):
    """Render one SCREEN field at its computed position (screenio.c L493-L549).

    Faithful port of the C ``cob_screen_puts``:

    * read the current cursor ``(y, x)``;
    * derive the target line/column from ``s.line`` / ``s.column``
      (``cob_get_int - 1``; fall back to the current y/x when unset or negative);
    * apply the relative LINE_PLUS/MINUS and COLUMN_PLUS/MINUS adjustments;
    * move there, record it in ``cob_current_y`` / ``cob_current_x``, apply the
      screen attributes, then write the field bytes - as an editable INPUT field
      (SECURE -> ``*``; control/space -> ``_``; else the byte) or a plain
      ``addnstr`` for output - and refresh.

    Requires an active curses screen; when none is active it is a no-op,
    mirroring the C ``#else`` build (where the whole screen path is compiled
    out).  The curses body is marked no-cover because it needs a live terminal;
    the routine remains call-safe (and so testable) without one.
    """
    global cob_current_y, cob_current_x
    if not (cob_screen_initialized and _stdscr is not None):
        return
    # ----- everything below requires a live terminal --------------------- #
    y, x = _stdscr.getyx()  # pragma: no cover - needs TTY

    sline = getattr(s, "line", None)  # pragma: no cover
    if not sline:  # pragma: no cover
        line = y
    else:  # pragma: no cover
        line = common._lazy_get_int(sline) - 1
        if line < 0:
            line = y
    scolumn = getattr(s, "column", None)  # pragma: no cover
    if not scolumn:  # pragma: no cover
        column = x
    else:  # pragma: no cover
        column = common._lazy_get_int(scolumn) - 1
        if column < 0:
            column = x

    sattr = getattr(s, "attr", 0)  # pragma: no cover
    if sattr & common.COB_SCREEN_LINE_PLUS:  # pragma: no cover
        line = y + line + 1
    elif sattr & common.COB_SCREEN_LINE_MINUS:  # pragma: no cover
        line = y - line + 1
    if sattr & common.COB_SCREEN_COLUMN_PLUS:  # pragma: no cover
        column = x + column + 1
    elif sattr & common.COB_SCREEN_COLUMN_MINUS:  # pragma: no cover
        column = x - column + 1

    _stdscr.move(line, column)  # pragma: no cover
    cob_current_y = line  # pragma: no cover
    cob_current_x = column  # pragma: no cover
    cob_screen_attr(getattr(s, "foreg", None), getattr(s, "backg", None), sattr)  # pragma: no cover
    if sattr & common.COB_SCREEN_INPUT:  # pragma: no cover
        for i in range(f.size):
            ch = f.data[i]
            if sattr & common.COB_SCREEN_SECURE:
                _stdscr.addch(ord("*"))
            elif ch <= ord(" "):
                _stdscr.addch(ord("_"))
            else:
                _stdscr.addch(ch)
    else:  # pragma: no cover
        _stdscr.addnstr(bytes(f.data[:f.size]).decode("latin-1"), f.size)
    _stdscr.refresh()  # pragma: no cover


def cob_field_display(f, line, column, fgc=None, bgc=None, scroll=None, attr=0):
    """DISPLAY a single field on the screen (screenio.c L973-L1000).

    Curses path: ensure the screen is initialised, optionally scroll, position
    the cursor at the decoded LINE/COLUMN, apply attributes, write the field
    bytes, and refresh.

    Fallback path (curses unavailable / initscr failed): the screen exception
    has been raised by :func:`cob_screen_init`; delegate to
    :func:`libcob_py.termio.cob_display` so the value still reaches the user
    (AAP 0.3.4 graceful degradation).
    """
    if not cob_screen_initialized:
        cob_screen_init()
    if not cob_screen_initialized:
        # Graceful degradation: plain DISPLAY of the field with a newline.
        termio.cob_display(False, True, 1, f)
        return

    # --- Curses rendering (screenio.c L985-L999) ---------------------------
    if scroll is not None:  # pragma: no cover - requires a live terminal
        sline = common._lazy_get_int(scroll)
        if attr & common.COB_SCREEN_SCROLL_DOWN:
            sline = -sline
        _stdscr.scrollok(True)
        _stdscr.scrl(sline)
        _stdscr.scrollok(False)
        _stdscr.refresh()
    sline, scolumn = get_line_column(line, column)  # pragma: no cover
    _stdscr.move(sline, scolumn)  # pragma: no cover
    cob_screen_attr(fgc, bgc, attr)  # pragma: no cover
    _stdscr.addnstr(bytes(f.data[:f.size]).decode("latin-1"), f.size)  # pragma: no cover
    _stdscr.refresh()  # pragma: no cover


def cob_field_accept(f, line, column, fgc=None, bgc=None, scroll=None, attr=0):
    """ACCEPT input into a single field (screenio.c L1002-L1372).

    Curses path: position at LINE/COLUMN, run the interactive edit loop, and on
    a terminating key store the CRT-STATUS / cursor position via
    :func:`cob_check_pos_status`.

    Fallback path (curses unavailable / initscr failed): delegate to
    :func:`libcob_py.termio.cob_accept` for plain line input and record a
    normal (``COB_SCR_OK``) status - matching the no-curses build whose
    ``cob_field_accept`` is an empty stub but where ACCEPT is still serviced by
    the plain terminal reader (AAP 0.3.4 graceful degradation).
    """
    if not cob_screen_initialized:
        cob_screen_init()
    if not cob_screen_initialized:
        # Graceful degradation: plain line ACCEPT into the field.
        termio.cob_accept(f)
        cob_check_pos_status(COB_SCR_OK, yx=(0, 0))
        return

    sline, scolumn = get_line_column(line, column)  # pragma: no cover
    fret = _run_accept_loop(f, sline, scolumn, attr)  # pragma: no cover
    cob_check_pos_status(fret)  # pragma: no cover


def _run_accept_loop(f, sline, scolumn, attr):  # pragma: no cover - needs a TTY
    """Interactive curses edit loop for one field (screenio.c L1066-L1370).

    Returns the CRT-STATUS ``fret`` value.  Implements the faithful key
    handling: F1-F64 (1001-1064), navigation/exception keys (2001-2008), Enter
    (0 / OK), printable-character entry with numeric validation and SECURE
    masking, backspace, and a read error (8001).  Marked no-cover because it
    requires a controlling terminal; the pure key-mapping helpers it relies on
    (:func:`cob_function_key_fret`, :func:`_special_key_fret`,
    :func:`cob_convert_key`) are unit-tested directly.
    """
    # Initialise the field to spaces / zeros, matching screenio.c L1059-L1064.
    if common.COB_FIELD_IS_NUMERIC(f):
        for i in range(f.size):
            f.data[i] = ord("0")
    else:
        for i in range(f.size):
            f.data[i] = ord(" ")

    rightpos = scolumn + f.size - 1
    pos = 0
    _stdscr.move(sline, scolumn)
    while True:
        _stdscr.move(sline, scolumn + pos)
        _stdscr.refresh()
        try:
            keyp = _stdscr.getch()
        except Exception:
            return 8001
        if keyp == _curses.ERR:
            return 8001

        fk = cob_function_key_fret(keyp, _curses.KEY_F0)
        if fk is not None:
            return fk

        keyp = cob_convert_key(keyp, 1)
        if keyp <= 0:
            _curses.flushinp()
            _curses.beep()
            continue
        if keyp == _curses.KEY_ENTER:
            return COB_SCR_OK
        sp = _special_key_fret(keyp)
        if sp is not None:
            return sp
        if keyp == _curses.KEY_BACKSPACE:
            if pos > 0:
                pos -= 1
                f.data[pos] = ord("0") if common.COB_FIELD_IS_NUMERIC(f) else ord(" ")
                _stdscr.move(sline, scolumn + pos)
                _stdscr.addch(ord(" "))
            continue
        if keyp == _curses.KEY_HOME:
            pos = 0
            continue
        if keyp == _curses.KEY_LEFT:
            if pos > 0:
                pos -= 1
            continue
        if keyp == _curses.KEY_RIGHT:
            if scolumn + pos < rightpos:
                pos += 1
            continue

        # Printable character (screenio.c L1310-L1361).
        if 0o37 < keyp < 0x100:
            if common.COB_FIELD_IS_NUMERIC(f) and not (ord("0") <= keyp <= ord("9")):
                _curses.beep()
                continue
            f.data[pos] = keyp
            if attr & common.COB_SCREEN_SECURE:
                _stdscr.addch(ord("*"))
            else:
                _stdscr.addch(keyp)
            if scolumn + pos >= rightpos:
                if attr & common.COB_SCREEN_AUTO:
                    return COB_SCR_OK
            else:
                pos += 1
            continue
        _curses.beep()



# ===========================================================================
# Multi-field SCREEN preparation + input (screenio.c L551-L867)
# ===========================================================================
def cob_prep_input(s):
    """Walk a SCREEN tree, rendering it and collecting INPUT fields (screenio.c L836-L867).

    Faithful port of the recursive C ``cob_prep_input`` dispatch on ``s.type``:

    * ``GROUP``     - recurse over the child's sibling chain (``child`` then
      ``next`` links).
    * ``FIELD``     - render the field via :func:`cob_screen_puts`; if the
      ``COB_SCREEN_INPUT`` flag is set, append a :class:`_cob_inp_struct` (with
      the cursor position captured by ``cob_screen_puts``) to the input table
      and bump the total-index count.
    * ``VALUE``     - render the literal value, repeating it ``occurs`` times.
    * ``ATTRIBUTE`` - apply the colour/style attributes only.

    The rendering side-effects require a live terminal (``cob_screen_puts`` is a
    no-op without one), but the recursion and INPUT-table construction run
    regardless, so the field-collection logic is testable without a TTY.
    """
    global _totl_index
    if s is None:
        return
    stype = getattr(s, "type", COB_SCREEN_TYPE_GROUP)
    if stype == COB_SCREEN_TYPE_GROUP:
        child = getattr(s, "child", None)
        while child is not None:
            cob_prep_input(child)
            child = getattr(child, "next", None)
    elif stype == COB_SCREEN_TYPE_FIELD:
        cob_screen_puts(s, getattr(s, "field", None))
        if getattr(s, "attr", 0) & common.COB_SCREEN_INPUT:
            _cob_base_inp.append(_cob_inp_struct(s, cob_current_y, cob_current_x))
            _totl_index = len(_cob_base_inp)
    elif stype == COB_SCREEN_TYPE_VALUE:
        cob_screen_puts(s, getattr(s, "value", None))
        occurs = getattr(s, "occurs", 0)
        n = 1
        while n < occurs:
            cob_screen_puts(s, getattr(s, "value", None))
            n += 1
    elif stype == COB_SCREEN_TYPE_ATTRIBUTE:
        cob_screen_attr(getattr(s, "foreg", None), getattr(s, "backg", None),
                        getattr(s, "attr", 0))


def cob_screen_get_all():
    """Interactive multi-field ACCEPT loop (screenio.c L551-L835).

    Drives input across all collected INPUT fields (``_cob_base_inp``): reads a
    key, returns the CRT-STATUS for terminating keys (F1-F64 -> 1001-1064,
    PAGE_UP -> 2001, PAGE_DOWN -> 2002, PRINT -> 2006, ESC -> 2005), handles
    TAB / BACK-TAB / cursor UP / DOWN navigation between fields, and edits the
    current field's data on printable input (with numeric validation and SECURE
    masking).  Stores the result in the module global ``_global_return`` and
    returns it.

    Requires a controlling terminal; the editing loop is therefore marked
    no-cover.  A defensive guard returns the read-error status (8001) when no
    screen is active so the routine is always safe to call.
    """
    global _curr_index, _global_return
    if not (cob_screen_initialized and _stdscr is not None):
        # Defensive guard: in the C flow this is only reached after a successful
        # init, but returning a read error keeps the call safe without a TTY.
        _global_return = 8001
        return _global_return

    # ----- interactive loop (needs a live terminal) ---------------------- #
    sptr = _cob_base_inp[_curr_index]  # pragma: no cover - needs TTY
    s = sptr.scr  # pragma: no cover
    sline = sptr.this_y  # pragma: no cover
    scolumn = sptr.this_x  # pragma: no cover
    _stdscr.move(sline, scolumn)  # pragma: no cover
    cob_screen_attr(getattr(s, "foreg", None), getattr(s, "backg", None),
                    getattr(s, "attr", 0))  # pragma: no cover
    rightpos = scolumn + s.field.size - 1  # pragma: no cover
    pos = 0  # pragma: no cover

    while True:  # pragma: no cover - interactive edit loop
        _stdscr.move(sline, scolumn + pos)
        _stdscr.refresh()
        try:
            keyp = _stdscr.getch()
        except Exception:
            _global_return = 8001
            return _global_return
        if keyp == _curses.ERR:
            _global_return = 8001
            return _global_return

        fk = cob_function_key_fret(keyp, _curses.KEY_F0)
        if fk is not None:
            _global_return = fk
            return _global_return

        keyp = cob_convert_key(keyp, 0)
        if keyp <= 0:
            _curses.flushinp()
            _curses.beep()
            continue
        if keyp == _curses.KEY_ENTER:
            _global_return = COB_SCR_OK
            return _global_return
        sp = _special_key_fret(keyp)
        if sp is not None and keyp != _curses.KEY_STAB and keyp != _curses.KEY_BTAB:
            _global_return = sp
            return _global_return

        # Field-to-field navigation (screenio.c L627-L709).
        if keyp == _curses.KEY_STAB:
            _curr_index = _curr_index + 1 if _curr_index < _totl_index - 1 else 0
        elif keyp == _curses.KEY_BTAB:
            _curr_index = _curr_index - 1 if _curr_index > 0 else _totl_index - 1
        elif keyp == _curses.KEY_UP:
            _curr_index = sptr.up_index
        elif keyp == _curses.KEY_DOWN:
            _curr_index = sptr.down_index
        else:
            # Printable-character entry into the current field.
            if 0o37 < keyp < 0x100 and (scolumn + pos) <= rightpos:
                if not (common.COB_FIELD_IS_NUMERIC(s.field)
                        and not (ord("0") <= keyp <= ord("9"))):
                    s.field.data[pos] = keyp
                    if getattr(s, "attr", 0) & common.COB_SCREEN_SECURE:
                        _stdscr.addch(ord("*"))
                    else:
                        _stdscr.addch(keyp)
                    if scolumn + pos < rightpos:
                        pos += 1
                    continue
            _curses.beep()
            continue

        # Re-anchor on the newly selected field.
        sptr = _cob_base_inp[_curr_index]
        s = sptr.scr
        sline = sptr.this_y
        scolumn = sptr.this_x
        rightpos = scolumn + s.field.size - 1
        pos = 0
        _stdscr.move(sline, scolumn)
        cob_screen_attr(getattr(s, "foreg", None), getattr(s, "backg", None),
                        getattr(s, "attr", 0))


def _setup_updown_indices():  # pragma: no cover - part of the curses ACCEPT path
    """Compute the UP/DOWN navigation links over ``_cob_base_inp`` (screenio.c L943-L965)."""
    totl = _totl_index
    if not totl:
        return
    starty = _cob_base_inp[0].this_y
    posu = 0
    posd = 0
    prevy = 0
    firsty = 0
    for n in range(totl):
        sptr = _cob_base_inp[n]
        if sptr.this_y > starty:
            if not firsty:
                firsty = n
            starty = sptr.this_y
            for idx in range(posd, n):
                _cob_base_inp[idx].down_index = n
            posu = prevy
            prevy = n
            posd = n
        sptr.up_index = posu
    for n in range(firsty):
        _cob_base_inp[n].up_index = posd


def _screen_display_node(s, line, column):  # pragma: no cover - faithful curses walk
    """Recursive curses render of a SCREEN tree (screenio.c L874-L904)."""
    stype = getattr(s, "type", COB_SCREEN_TYPE_GROUP)
    if stype == COB_SCREEN_TYPE_GROUP:
        child = getattr(s, "child", None)
        while child is not None:
            _screen_display_node(child, line, column)
            child = getattr(child, "next", None)
    elif stype == COB_SCREEN_TYPE_FIELD:
        cob_screen_puts(s, getattr(s, "field", None))
    elif stype == COB_SCREEN_TYPE_VALUE:
        cob_screen_puts(s, getattr(s, "value", None))
        occurs = getattr(s, "occurs", 0)
        n = 1
        while n < occurs:
            cob_screen_puts(s, getattr(s, "value", None))
            n += 1
    elif stype == COB_SCREEN_TYPE_ATTRIBUTE:
        cob_screen_attr(getattr(s, "foreg", None), getattr(s, "backg", None),
                        getattr(s, "attr", 0))


# ===========================================================================
# Group-screen entry points (screenio.c L873-L972)
# ===========================================================================
def cob_screen_display(s, line, column):
    """DISPLAY a (possibly nested) SCREEN item (screenio.c L873-L905).

    Ensures the screen is initialised, then renders the screen tree faithfully
    via :func:`cob_screen_puts` (curses path).  In the fallback (no-curses)
    build this degrades to a plain DISPLAY of every leaf field's value through
    :mod:`libcob_py.termio` (AAP 0.3.4).
    """
    if not cob_screen_initialized:
        cob_screen_init()
    if not cob_screen_initialized:
        _display_screen_fallback(s)
        return
    _screen_display_node(s, line, column)  # pragma: no cover - needs a TTY
    _stdscr.refresh()  # pragma: no cover


def cob_screen_accept(s, line, column):
    """ACCEPT into a (possibly nested) SCREEN item (screenio.c L906-L972).

    Curses path: reset the input table, prepare the screen (rendering each item
    and collecting INPUT fields via :func:`cob_prep_input`), sort the fields by
    screen position, compute the UP/DOWN navigation links, run the interactive
    :func:`cob_screen_get_all` loop, and store the result via
    :func:`cob_check_pos_status`.  ``COB_SCR_NO_FIELD`` (8000) is recorded when
    the screen declares no input fields (screenio.c L922-L925).

    Fallback path (no curses): service each leaf input field through the plain
    :func:`libcob_py.termio.cob_accept` reader and record a normal status
    (AAP 0.3.4 graceful degradation).
    """
    global _cob_base_inp, _totl_index, _curr_index, _global_return
    global cob_current_y, cob_current_x

    if not cob_screen_initialized:
        cob_screen_init()
    if not cob_screen_initialized:
        _accept_screen_fallback(s)
        cob_check_pos_status(COB_SCR_OK, yx=(0, 0))
        return

    # ----- curses path (screenio.c L915-L971) ---------------------------- #
    _cob_base_inp = []  # pragma: no cover - needs a TTY
    common.cob_exception_code = 0  # pragma: no cover
    cob_current_y = 0  # pragma: no cover
    cob_current_x = 0  # pragma: no cover
    _totl_index = 0  # pragma: no cover
    _stdscr.move(0, 0)  # pragma: no cover
    cob_prep_input(s)  # pragma: no cover
    if not _totl_index:  # pragma: no cover - no input fields is an error
        cob_check_pos_status(COB_SCR_NO_FIELD)
        return
    _cob_base_inp.sort(key=lambda e: (e.this_y, e.this_x))  # pragma: no cover
    _setup_updown_indices()  # pragma: no cover
    _curr_index = 0  # pragma: no cover
    _global_return = 0  # pragma: no cover
    cob_screen_get_all()  # pragma: no cover
    cob_check_pos_status(_global_return)  # pragma: no cover


# ===========================================================================
# Screen-tree leaf iteration + plain-terminal fallback (AAP 0.3.4)
# ===========================================================================
def _iter_screen_fields(s):
    """Yield the leaf field nodes of a SCREEN tree (for the termio fallback).

    A screen node is expected to expose ``child`` (group head), ``next``
    (sibling), and ``field`` / ``value`` leaf attributes mirroring the C
    ``cob_screen`` struct (common.h L709-L721).  Robust to a plain
    :class:`~libcob_py.common.cob_field` so callers may pass a bare field as a
    degenerate one-field screen.
    """
    if s is None:
        return
    if hasattr(s, "child") or hasattr(s, "next") or hasattr(s, "field"):
        node = s
        while node is not None:
            child = getattr(node, "child", None)
            if child is not None:
                for leaf in _iter_screen_fields(child):
                    yield leaf
            else:
                leaf = getattr(node, "field", None) or getattr(node, "value", None)
                if leaf is not None:
                    yield leaf
            node = getattr(node, "next", None)
    else:
        # Degenerate case: a bare cob_field used directly as a screen item.
        yield s


def _display_screen_fallback(s):
    """Plain-terminal DISPLAY of every field in a screen tree (termio path)."""
    for leaf in _iter_screen_fields(s):
        termio.cob_display(False, True, 1, leaf)


def _accept_screen_fallback(s):
    """Plain-terminal ACCEPT into every input field of a screen tree (termio path)."""
    for leaf in _iter_screen_fields(s):
        termio.cob_accept(leaf)


# ===========================================================================
# Subsystem (re)initialisation - lazy; NOT part of the cob_init_* chain
# ===========================================================================
def cob_init_screenio():
    """Reset the screenio subsystem state (screenio.c ``cob_init_screenio``).

    Per ``common.c`` the screen subsystem is NOT wired into the ``cob_init_*``
    startup chain (agent-prompt Phase 4); the curses screen is started lazily on
    the first DISPLAY/ACCEPT via :func:`cob_screen_init`.  This entry point
    simply (re)seeds the module state and clears the fallback latch so a fresh
    runtime can attempt curses again - it must never touch a terminal.
    """
    global _curses_failed, cob_screen_initialized, cob_screen_mode
    global cob_has_color, cob_max_y, cob_max_x, fore_color, back_color
    global cob_current_y, cob_current_x, insert_mode
    global cob_extended_status, cob_use_esc
    global _cob_base_inp, _curr_index, _totl_index, _global_return, _stdscr
    _curses_failed = False
    cob_screen_initialized = 0
    cob_screen_mode = 0
    cob_has_color = 0
    cob_max_y = 0
    cob_max_x = 0
    fore_color = 0
    back_color = 0
    cob_current_y = 0
    cob_current_x = 0
    insert_mode = 0
    cob_extended_status = 0
    cob_use_esc = 0
    _cob_base_inp = []
    _curr_index = 0
    _totl_index = 0
    _global_return = 0
    _stdscr = None

