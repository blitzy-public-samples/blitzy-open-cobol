"""libcob_py.screenio - COBOL SCREEN SECTION terminal I/O.

Python standard-library port of the C runtime screen subsystem
``libcob/screenio.c`` together with the public CRT-status constants defined in
``copy/screenio.cpy``.  This module is part of the C->Python backend refactor
described in the Agent Action Plan (Section 0.3.4); it reproduces the on-screen
behaviour of the original ncurses-based implementation using only the Python
standard-library :mod:`curses` module (no third-party dependency, per rule R3).

Design (AAP 0.3.2 "Template-method with graceful degradation"):

* When a controlling terminal supports curses, ACCEPT/DISPLAY of SCREEN SECTION
  items is rendered through :mod:`curses`, honouring colour attributes, field
  editing, and the F1-F64 function-key map exactly as ``screenio.c`` does.
* When :func:`curses.initscr` fails (unsupported terminal, no TTY, e.g. a pipe
  or a batch/test environment), the screen exception
  ``EC-SCREEN-ITEM-TRUNCATED`` (hex ``0F03`` - see
  ``libcob/exception.def`` and AAP 0.6.1) is raised and ACCEPT/DISPLAY fall
  through to the non-curses :mod:`libcob_py.termio` path.  This mirrors the
  ``#ifdef COB_GEN_SCREENIO`` / ``#else`` split in ``screenio.c`` where the
  fallback build provides plain terminal I/O.

The emitter (``cobc/codegen.c``) routes ``cob_field_display``,
``cob_field_accept`` and the ``cob_screen_*`` family to this module, so the
public function names below mirror the C entry points one-for-one.
"""

# --- Standard-library imports (rule R3: stdlib only) ------------------------
import os

try:
    # curses is part of the CPython standard library on POSIX platforms.  It is
    # imported defensively because a terminal/TTY may be unavailable (the
    # graceful-degradation contract above); an ImportError simply forces the
    # termio fallback rather than aborting the runtime.
    import curses as _curses
except Exception:  # pragma: no cover - platform without curses (e.g. stock Win)
    _curses = None

# --- Internal runtime imports ----------------------------------------------
# ``common`` is the runtime base (exception dispatch, field model, module
# stack).  ``move`` supplies the integer accessors used for LINE/COL extraction
# and CRT-STATUS storage.  ``termio`` is the bottom-of-stack plain stream used
# as the graceful-degradation fallback; it never imports screenio back, so this
# top-level import introduces no cycle.
from libcob_py import common
from libcob_py import move
from libcob_py import termio

# ===========================================================================
# CRT-STATUS constants - copy/screenio.cpy (83 values, AAP 0.3.4 / 0.6.1)
# ===========================================================================
# The constant *values* are the contract returned in CRT STATUS / COB-CRT-STATUS
# and consumed by COBOL programs that COPY screenio.cpy.  The COBOL data names
# use hyphens (e.g. COB-COLOR-BLACK); the Python identifiers below substitute
# underscores, preserving the numeric values byte-for-byte.

# --- Colours (COB-COLOR-*, values 0-7) -------------------------------------
COB_COLOR_BLACK = 0
COB_COLOR_BLUE = 1
COB_COLOR_GREEN = 2
COB_COLOR_CYAN = 3
COB_COLOR_RED = 4
COB_COLOR_MAGENTA = 5
COB_COLOR_YELLOW = 6
COB_COLOR_WHITE = 7

# --- Normal return (COB-SCR-OK, value 0) -----------------------------------
COB_SCR_OK = 0

# --- Function keys (COB-SCR-F1..F64, values 1001-1064) ---------------------
# Generated programmatically to guarantee the exact sequential mapping
# F1=1001 .. F64=1064 defined in screenio.cpy without 64 hand-written lines.
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

# --- Exception keys (COB-SCR-*, values 2xxx) -------------------------------
COB_SCR_PAGE_UP = 2001
COB_SCR_PAGE_DOWN = 2002
COB_SCR_KEY_UP = 2003
COB_SCR_KEY_DOWN = 2004
COB_SCR_ESC = 2005
COB_SCR_PRINT = 2006
COB_SCR_TAB = 2007
COB_SCR_BACK_TAB = 2008

# --- Input validation (COB-SCR-NO-FIELD, value 8000) -----------------------
COB_SCR_NO_FIELD = 8000

# --- Other errors (COB-SCR-FATAL, value 9000) ------------------------------
COB_SCR_FATAL = 9000

# Read-only mapping of colour code (0-7) -> COB-COLOR name, exported for tests
# and diagnostics.  Mirrors the eight-way switch in cob_screen_attr().
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

# ===========================================================================
# SCREEN item descriptor (cob_screen) - the struct the emitter constructs.
#
# The emitter (codegen.c output_screen_definition) replaces the C
# "static cob_screen s_N = { ... };" aggregate with a runtime constructor call
# ``s_N = screenio.cob_screen(next, child, field, value, line, column, foreg,
# backg, type, occurs, attr)``.  The positional order mirrors the C struct
# __cob_screen (common.h L709-L721) EXACTLY.  The COB_SCREEN_TYPE_* discriminant
# values match common.h L642-L645.
# ===========================================================================
COB_SCREEN_TYPE_GROUP = 0
COB_SCREEN_TYPE_FIELD = 1
COB_SCREEN_TYPE_VALUE = 2
COB_SCREEN_TYPE_ATTRIBUTE = 3


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
# static within the curses build.  They are reproduced here as module globals.
cob_screen_initialized = 0
cob_screen_mode = 0

# True once curses initialisation has failed; latches the termio fallback so we
# do not repeatedly attempt (and re-fail) initscr on every ACCEPT/DISPLAY.
_curses_failed = False

# The curses standard screen window returned by initscr(); ``None`` until the
# screen subsystem has been initialised (or while in fallback mode).
_stdscr = None

# Colour / capability state (screenio.c statics).
cob_has_color = 0
cob_max_y = 0
cob_max_x = 0
fore_color = 0
back_color = 0

# Editing / accept behaviour flags (screenio.c statics, seeded from env).
insert_mode = 0
cob_extended_status = 0
cob_use_esc = 0


# ===========================================================================
# Capability + lifecycle
# ===========================================================================
def _curses_available():
    """Return True when the curses screen path can be used.

    Curses is usable only when the module imported successfully and a previous
    initialisation has not already failed (the latched fallback).  This is the
    Python equivalent of the C ``#ifdef COB_GEN_SCREENIO`` compile-time gate
    combined with the runtime initscr() success check.
    """
    return _curses is not None and not _curses_failed


def _raise_screen_truncated():
    """Signal the screen-unavailable condition (AAP 0.3.4 / 0.6.1).

    Sets the COBOL exception ``EC-SCREEN-ITEM-TRUNCATED`` (hex ``0F03``) - the
    nearest authoritative ``exception.def`` entry to the prompt's
    ``EC-SCREEN-ITEM-TRUNCATION`` - and latches the termio fallback so the
    caller degrades gracefully instead of aborting.
    """
    global _curses_failed
    _curses_failed = True
    common.cob_set_exception(common.COB_EC_SCREEN_ITEM_TRUNCATED)


def cob_screen_init():
    """Initialise the curses screen subsystem (screenio.c L385-L440).

    Mirrors the C ``cob_screen_init``: read the COB_SCREEN_EXCEPTIONS /
    COB_SCREEN_ESC / COB_INSERT_MODE environment options, then call
    ``initscr`` and configure raw/keypad/noecho mode and colour support.  On
    any failure (no TTY, curses error) the screen exception is raised and the
    function returns without marking the screen initialised, so callers fall
    through to termio.
    """
    global cob_screen_initialized, cob_has_color, cob_max_y, cob_max_x
    global fore_color, back_color, _stdscr, insert_mode
    global cob_extended_status, cob_use_esc

    if cob_screen_initialized:
        return
    if not _curses_available():
        # No curses build available -> immediate graceful degradation.
        _raise_screen_truncated()
        return

    # --- Environment-driven options (screenio.c L389-L409) -----------------
    s = os.environ.get("COB_SCREEN_EXCEPTIONS")
    if s and s[:1] in ("Y", "y"):
        cob_extended_status = 1
        s = os.environ.get("COB_SCREEN_ESC")
        if s and s[:1] in ("Y", "y"):
            cob_use_esc = 1
    s = os.environ.get("COB_INSERT_MODE")
    if s and s[:1] in ("Y", "y"):
        insert_mode = 1

    # --- Curses initialisation (screenio.c L410-L438) ----------------------
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
            cob_has_color = 1
        cob_max_y, cob_max_x = _stdscr.getmaxyx()
        cob_screen_initialized = 1
    except Exception:
        # initscr / capability setup failed (the C path calls
        # cob_runtime_error + cob_stop_run, but the refactor's graceful
        # degradation contract requires falling back to termio instead).
        _stdscr = None
        _raise_screen_truncated()


def cob_screen_terminate():
    """Tear down the curses screen subsystem (screenio.c L441-L448).

    Resets the initialised flag and calls ``endwin`` when a screen is active.
    Safe to call when no screen was ever started (mirrors the C guard).
    """
    global cob_screen_initialized, _stdscr
    if cob_screen_initialized:
        cob_screen_initialized = 0
        if _curses is not None and _stdscr is not None:
            try:
                _curses.endwin()
            except Exception:  # pragma: no cover - endwin failure is non-fatal
                pass
        _stdscr = None


def cob_screen_set_mode(smode):
    """Set the screen mode flag (screenio.c L1387-L1395 / fallback L1430-L1433).

    ``smode`` 0 selects line/scroll mode; non-zero selects full-screen mode.
    The C curses build calls ``refresh`` on transition; here we simply record
    the mode (the refresh is implicit on the next ACCEPT/DISPLAY).
    """
    global cob_screen_mode
    cob_screen_mode = int(smode)


# ===========================================================================
# Pure helpers (testable without a controlling terminal)
# ===========================================================================
def get_line_column(fline, fcol):
    """Decode LINE/COLUMN operands into 0-based (line, col) (screenio.c L213-L250).

    Faithful reproduction of the C ``get_line_column``:

    * ``fline is None`` -> position (0, 0).
    * When only ``fline`` is supplied, the integer value is split into line and
      column: a 4-byte field uses ``/100`` and ``%100``; otherwise ``/1000`` and
      ``%1000`` (the COBOL ``LINE nn`` packed form).
    * When both are supplied, ``fline`` is the line and ``fcol`` the column.
    * 1-based COBOL positions are converted to 0-based curses positions
      (decrement when > 0).
    """
    if fline is None:
        return 0, 0

    p = move.cob_get_int(fline)
    if fcol is None:
        if fline.size == 4:
            line = p // 100
            col = p % 100
        else:
            line = p // 1000
            col = p % 1000
    else:
        line = p
        col = move.cob_get_int(fcol)

    if line > 0:
        line -= 1
    if col > 0:
        col -= 1
    return line, col


def _curses_color(code):
    """Map a COB-COLOR code (0-7) to the curses COLOR_* constant.

    Returns ``None`` for an out-of-range code, mirroring the ``default: break``
    arm of the C colour switch which leaves the colour unchanged.
    """
    if _curses is None:
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

    screenio.c L1129-L1132 maps a raw curses key in the open interval
    ``(KEY_F0, KEY_F(65))`` to ``1000 + (keyp - KEY_F0)``, i.e. F1->1001 ..
    F64->1064.  Pulled into a pure helper so the F1-F64 contract can be tested
    without a live terminal.
    """
    if key_f0 < keyp < key_f0 + 65:
        return 1000 + (keyp - key_f0)
    return None


def _special_key_fret(keyp):
    """Map a converted curses navigation/exception key to its CRT-STATUS value.

    Returns the ``fret`` value for page/arrow/print/escape/tab keys
    (screenio.c L1145-L1182) or ``None`` when the key is an ordinary editing
    key that does not terminate the ACCEPT.  Requires curses for the KEY_*
    constants; returns ``None`` when curses is unavailable.
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
    """Normalise a raw key code (screenio.c L96-L210).

    Translates control characters (Enter/Tab/Backspace) to their canonical
    curses KEY_* values and zeroes keys that should be ignored unless extended
    status / ESC handling is enabled.  Returns the (possibly rewritten) key
    code; a return value <= 0 means "ignore and re-read" exactly as the C code
    treats ``*keyp <= 0``.
    """
    if _curses is None:  # pragma: no cover - no curses constants to map to
        return keyp

    # --- Map control chars to KEY_xxx (screenio.c L99-L114) ----------------
    if keyp in (ord("\n"), ord("\r"), 0o004, 0o032):
        keyp = _curses.KEY_ENTER
    elif keyp == ord("\t"):
        keyp = _curses.KEY_STAB
    elif keyp in (ord("\b"), 0o177):
        keyp = _curses.KEY_BACKSPACE

    # --- Ignore-key filtering (screenio.c L185-L208) -----------------------
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
    """Store the ACCEPT result into CRT STATUS and CURSOR position.

    Faithful port of screenio.c L449-L489:

    * A non-zero ``fret`` raises ``EC-IMP-ACCEPT``.
    * ``crt_status`` of the current module receives ``fret`` - as an integer for
      a numeric field, else as a 4-digit zero-padded display string.
    * ``cursor_pos`` (when present) receives the encoded line/column.

    ``yx`` may be supplied as an explicit ``(line, column)`` tuple for testing;
    otherwise the live cursor position is read from curses when available.
    """
    if fret:
        common.cob_set_exception(common.COB_EC_IMP_ACCEPT)

    module = common.cob_current_module
    if module is None:
        return

    crt = getattr(module, "crt_status", None)
    if crt is not None:
        if common.COB_FIELD_IS_NUMERIC(crt):
            move.cob_set_int(crt, fret)
        else:
            datbuf = ("%4.4d" % fret).encode("latin-1")
            crt.data[0:4] = datbuf[0:4]

    cursor = getattr(module, "cursor_pos", None)
    if cursor is not None:
        if yx is None:
            if cob_screen_initialized and _stdscr is not None:
                yx = _stdscr.getyx()
            else:
                return
        sline, scolumn = yx
        if (common.COB_FIELD_IS_NUMERIC(cursor)
                and common.COB_FIELD_TYPE(cursor) != common.COB_TYPE_NUMERIC_DISPLAY):
            move.cob_set_int(cursor, sline * 1000 + scolumn)
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
    """Store the current screen line or column into *f* (screenio.c L1374-L1386).

    ``l_or_c`` 0 stores the line; non-zero stores the column.  When the screen
    is active the value comes from the live curses cursor; otherwise it is 0,
    matching the no-curses fallback stub (screenio.c L1434-L1437) which leaves
    the field at its initialised value.
    """
    if cob_screen_initialized and _stdscr is not None:
        line, col = _stdscr.getyx()
    else:
        line, col = 0, 0
    move.cob_set_int(f, line if l_or_c == 0 else col)


# ===========================================================================
# Curses rendering + input (active only with a controlling terminal)
# ===========================================================================
def cob_screen_attr(fgc, bgc, attr):
    """Apply colour and style attributes to the screen (screenio.c L250-L384).

    Reproduces the C ``cob_screen_attr``: reset to A_NORMAL, OR in the
    REVERSE/HIGHLIGHT(bold)/BLINK/UNDERLINE styles, then - when the terminal
    supports colour - resolve the COB-COLOR foreground/background codes via
    :func:`_curses_color` and install a colour pair.  A no-op when the screen
    is not active.
    """
    if not (cob_screen_initialized and _stdscr is not None):  # pragma: no cover
        return
    _stdscr.attrset(_curses.A_NORMAL)

    styles = 0
    if attr & common.COB_SCREEN_REVERSE:
        styles |= _curses.A_REVERSE
    if attr & common.COB_SCREEN_HIGHLIGHT:
        styles |= _curses.A_BOLD
    if attr & common.COB_SCREEN_BLINK:
        styles |= _curses.A_BLINK
    if attr & common.COB_SCREEN_UNDERLINE:
        styles |= _curses.A_UNDERLINE
    if styles:
        _stdscr.attron(styles)

    if cob_has_color:
        fgcolor = fore_color
        bgcolor = back_color
        if fgc is not None:
            mapped = _curses_color(move.cob_get_int(fgc))
            if mapped is not None:
                fgcolor = mapped
        if bgc is not None:
            mapped = _curses_color(move.cob_get_int(bgc))
            if mapped is not None:
                bgcolor = mapped
        # Install a colour pair (pair 1 reused, as the C code rebuilds pairs);
        # any curses error here is non-fatal and leaves the default attributes.
        try:  # pragma: no cover - requires a colour-capable live terminal
            _curses.init_pair(1, fgcolor, bgcolor)
            _stdscr.attron(_curses.color_pair(1))
        except Exception:  # pragma: no cover
            pass


def cob_field_display(f, line, column, fgc=None, bgc=None, scroll=None, attr=0):
    """DISPLAY a field on the screen (screenio.c L973-L1000).

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
        sline = move.cob_get_int(scroll)
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
    """ACCEPT input into a field (screenio.c L1002-L1372).

    Curses path: position at LINE/COLUMN, run the interactive edit loop, and on
    a terminating key store the CRT-STATUS / cursor position via
    :func:`cob_check_pos_status`.

    Fallback path (curses unavailable / initscr failed): delegate to
    :func:`libcob_py.termio.cob_accept` for plain line input and record a
    normal (``COB_SCR_OK``) status, matching the no-curses build whose
    ``cob_field_accept`` is an empty stub but where ACCEPT is still serviced by
    the plain terminal reader.
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
# Group-screen entry points (screenio.c L873-L972)
# ===========================================================================
def cob_screen_display(s, line, column):
    """DISPLAY a (possibly nested) SCREEN item (screenio.c L873-L905).

    Ensures the screen is initialised, then renders the screen tree.  Group and
    value/field nodes are walked recursively; in the fallback (no-curses) build
    this degrades to plain DISPLAY of each field's value via termio.
    """
    if not cob_screen_initialized:
        cob_screen_init()
    if not cob_screen_initialized:
        _display_screen_fallback(s)
        return
    _render_screen(s, line, column)  # pragma: no cover - needs a TTY


def cob_screen_accept(s, line, column):
    """ACCEPT into a (possibly nested) SCREEN item (screenio.c L906-L972).

    Initialises the screen and runs input; in the fallback build it services
    each input field through the plain termio reader and records OK status.
    """
    if not cob_screen_initialized:
        cob_screen_init()
    if not cob_screen_initialized:
        _accept_screen_fallback(s)
        cob_check_pos_status(COB_SCR_OK, yx=(0, 0))
        return
    _input_screen(s, line, column)  # pragma: no cover - needs a TTY


def _iter_screen_fields(s):
    """Yield the leaf field nodes of a SCREEN tree.

    A screen node is expected to expose ``child`` (group head), ``next``
    (sibling), and ``field``/``value`` leaf attributes mirroring the C
    ``cob_screen`` struct (common.h L705-L725).  Robust to plain field objects
    so callers may pass a bare ``cob_field`` as a degenerate one-field screen.
    """
    if s is None:
        return
    if hasattr(s, "child") or hasattr(s, "next") or hasattr(s, "field"):
        node = s
        while node is not None:
            child = getattr(node, "child", None)
            if child is not None:
                yield from _iter_screen_fields(child)
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
    """Plain-terminal ACCEPT into every input field of a screen tree."""
    for leaf in _iter_screen_fields(s):
        termio.cob_accept(leaf)


def _render_screen(s, line, column):  # pragma: no cover - needs a TTY
    """Curses render of a screen tree (screenio.c L493-L550, L874-L905)."""
    for leaf in _iter_screen_fields(s):
        cob_field_display(leaf, line, column, None, None, None, 0)


def _input_screen(s, line, column):  # pragma: no cover - needs a TTY
    """Curses input over a screen tree (screenio.c L551-L835, L906-L972)."""
    for leaf in _iter_screen_fields(s):
        cob_field_accept(leaf, line, column, None, None, None, 0)


# ===========================================================================
# Subsystem initialisation
# ===========================================================================
def cob_init_screenio():
    """Initialise the screenio subsystem state (screenio.c cob_init_screenio).

    The C runtime seeds the colour defaults here; the actual curses screen is
    started lazily on the first ACCEPT/DISPLAY (cob_screen_init).  Resets the
    fallback latch so a fresh runtime can attempt curses again.
    """
    global _curses_failed, cob_screen_initialized, cob_screen_mode
    _curses_failed = False
    cob_screen_initialized = 0
    cob_screen_mode = 0
