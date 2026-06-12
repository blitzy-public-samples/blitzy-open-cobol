"""Unit tests for :mod:`libcob_py.screenio` - the SCREEN SECTION terminal I/O.

Exercises the pure-Python port of ``libcob/screenio.c`` together with the 83
CRT-status constants defined in ``copy/screenio.cpy`` (AAP 0.3.4 / 0.6.1).

The continuous-integration environment has no controlling terminal, so the
curses rendering/edit loops (marked ``# pragma: no cover`` in the module) are
not driven here.  Instead the suite focuses on:

* the 83 CRT-status constant VALUES (colours 0-7, F1-F64 = 1001-1064,
  navigation 2001-2008, OK=0, NO-FIELD=8000, FATAL=9000);
* the pure decode/translate helpers (``get_line_column``,
  ``cob_function_key_fret``, ``_special_key_fret``, ``cob_convert_key``,
  ``cob_check_pos_status``, ``_curses_color``) that are factored out of the
  curses loops precisely so they are testable without a TTY;
* the graceful-degradation contract: when ``initscr`` is unavailable the screen
  exception ``EC-SCREEN-ITEM-TRUNCATED`` (hex ``0F03``) is raised and DISPLAY /
  ACCEPT fall through to :mod:`libcob_py.termio` (AAP 0.3.4);
* the SCREEN-tree walk (``_iter_screen_fields``) and the subsystem lifecycle.

Standard library only; ``pytest`` is a development-only framework (AAP 0.5).
"""
import pytest

libcob_py = pytest.importorskip("libcob_py")
common = pytest.importorskip("libcob_py.common")
move = pytest.importorskip("libcob_py.move")
termio = pytest.importorskip("libcob_py.termio")
screenio = pytest.importorskip("libcob_py.screenio")


# ---------------------------------------------------------------------------
# Field builder
# ---------------------------------------------------------------------------
def numdisp(digits):
    data = digits.encode("latin-1")
    attr = common.cob_field_attr(
        type=common.COB_TYPE_NUMERIC_DISPLAY, digits=len(digits), scale=0,
        flags=0, pic=None)
    return common.cob_field(size=len(data), data=bytearray(data), attr=attr)


def alnum(text, size=None):
    data = (text.encode("latin-1") if isinstance(text, str) else bytes(text))
    if size is not None:
        data = data.ljust(size, b" ")[:size]
    attr = common.cob_field_attr(
        type=common.COB_TYPE_ALPHANUMERIC, digits=0, scale=0, flags=0, pic=None)
    return common.cob_field(size=len(data), data=bytearray(data), attr=attr)


def numbin4(value=0):
    """4-byte signed BINARY field (for numeric CRT-STATUS / CURSOR tests)."""
    f = common.cob_field(
        size=4, data=bytearray(4),
        attr=common.cob_field_attr(type=common.COB_TYPE_NUMERIC_BINARY,
                                   digits=8, scale=0,
                                   flags=common.COB_FLAG_HAVE_SIGN, pic=None))
    move.cob_set_int(f, value)
    return f


@pytest.fixture(autouse=True)
def _reset_screen_state():
    """Keep each test independent of the module-level screen latches."""
    saved_mod = common.cob_current_module
    saved_exc = common.cob_exception_code
    screenio.cob_init_screenio()
    yield
    screenio.cob_init_screenio()
    common.cob_current_module = saved_mod
    common.cob_exception_code = saved_exc


# ===========================================================================
# CRT-status constants (copy/screenio.cpy - 83 values)
# ===========================================================================
def test_color_constants():
    assert screenio.COB_COLOR_BLACK == 0
    assert screenio.COB_COLOR_BLUE == 1
    assert screenio.COB_COLOR_GREEN == 2
    assert screenio.COB_COLOR_CYAN == 3
    assert screenio.COB_COLOR_RED == 4
    assert screenio.COB_COLOR_MAGENTA == 5
    assert screenio.COB_COLOR_YELLOW == 6
    assert screenio.COB_COLOR_WHITE == 7


def test_function_key_constants_sequential():
    assert screenio.COB_SCR_F1 == 1001
    assert screenio.COB_SCR_F64 == 1064
    # all 64 function-key constants exist and are sequential
    for n in range(1, 65):
        assert getattr(screenio, "COB_SCR_F%d" % n) == 1000 + n


def test_navigation_constants():
    assert screenio.COB_SCR_OK == 0
    assert screenio.COB_SCR_PAGE_UP == 2001
    assert screenio.COB_SCR_PAGE_DOWN == 2002
    assert screenio.COB_SCR_KEY_UP == 2003
    assert screenio.COB_SCR_KEY_DOWN == 2004
    assert screenio.COB_SCR_ESC == 2005
    assert screenio.COB_SCR_PRINT == 2006
    assert screenio.COB_SCR_TAB == 2007
    assert screenio.COB_SCR_BACK_TAB == 2008
    assert screenio.COB_SCR_NO_FIELD == 8000
    assert screenio.COB_SCR_FATAL == 9000


# ===========================================================================
# get_line_column - all four operand forms (screenio.c L213-L250)
# ===========================================================================
def test_get_line_column_none():
    assert screenio.get_line_column(None, None) == (0, 0)


def test_get_line_column_combined_four_byte():
    # 4-byte field -> /100, %100 ; then 1-based -> 0-based decrement.
    fline = numdisp("1234")  # size 4 -> line 12, col 34 -> (11, 33)
    assert screenio.get_line_column(fline, None) == (11, 33)


def test_get_line_column_combined_other_width():
    # non-4-byte field -> /1000, %1000.
    fline = numdisp("12034")  # size 5 -> line 12, col 34 -> (11, 33)
    assert screenio.get_line_column(fline, None) == (11, 33)


def test_get_line_column_separate():
    fline = numdisp("5")
    fcol = numdisp("10")
    assert screenio.get_line_column(fline, fcol) == (4, 9)


# ===========================================================================
# Function-key + special-key mapping (screenio.c L1129-L1182)
# ===========================================================================
def test_function_key_fret_range():
    key_f0 = screenio._curses.KEY_F0 if screenio._curses is not None else 264
    assert screenio.cob_function_key_fret(key_f0 + 1, key_f0) == 1001
    assert screenio.cob_function_key_fret(key_f0 + 64, key_f0) == 1064
    # out of range -> None
    assert screenio.cob_function_key_fret(key_f0 + 65, key_f0) is None
    assert screenio.cob_function_key_fret(key_f0, key_f0) is None


def test_special_key_fret():
    if screenio._curses is None:  # pragma: no cover - curses always present in CI
        pytest.skip("curses not available")
    c = screenio._curses
    assert screenio._special_key_fret(c.KEY_PPAGE) == 2001
    assert screenio._special_key_fret(c.KEY_NPAGE) == 2002
    assert screenio._special_key_fret(c.KEY_UP) == 2003
    assert screenio._special_key_fret(c.KEY_DOWN) == 2004
    assert screenio._special_key_fret(0o33) == 2005  # ESC
    # an ordinary editing key is not a terminator
    assert screenio._special_key_fret(ord("A")) is None


def test_convert_key_control_chars():
    if screenio._curses is None:  # pragma: no cover
        pytest.skip("curses not available")
    c = screenio._curses
    assert screenio.cob_convert_key(ord("\n"), False) == c.KEY_ENTER
    assert screenio.cob_convert_key(ord("\t"), False) == c.KEY_STAB
    assert screenio.cob_convert_key(ord("\b"), False) == c.KEY_BACKSPACE
    # ESC is ignored (-> 0) unless extended status + ESC handling are enabled
    assert screenio.cob_convert_key(0o33, False) == 0


def test_curses_color_mapping():
    if screenio._curses is None:  # pragma: no cover
        pytest.skip("curses not available")
    c = screenio._curses
    assert screenio._curses_color(screenio.COB_COLOR_BLACK) == c.COLOR_BLACK
    assert screenio._curses_color(screenio.COB_COLOR_WHITE) == c.COLOR_WHITE
    # out-of-range code leaves the colour unchanged (None)
    assert screenio._curses_color(99) is None


# ===========================================================================
# cob_check_pos_status (screenio.c L449-L489)
# ===========================================================================
def test_check_pos_status_alnum_crt_and_cursor():
    # Use the production module class so the sign/display machinery sees a
    # fully-formed module (display_sign etc.).
    mod = common.cob_module(crt_status=alnum("0000"), cursor_pos=alnum("0000"))
    common.cob_current_module = mod
    common.cob_exception_code = 0
    screenio.cob_check_pos_status(1007, yx=(4, 9))
    assert bytes(mod.crt_status.data[:4]) == b"1007"
    # 4-digit cursor encoding: line*100 + col = 409 -> "0409"
    assert bytes(mod.cursor_pos.data[:4]) == b"0409"
    # non-zero fret raises EC-IMP-ACCEPT
    assert common.cob_exception_code != 0


def test_check_pos_status_six_digit_cursor():
    mod = common.cob_module(crt_status=alnum("0000"), cursor_pos=alnum("000000"))
    common.cob_current_module = mod
    screenio.cob_check_pos_status(0, yx=(4, 9))
    assert bytes(mod.cursor_pos.data[:6]) == b"004009"


def test_check_pos_status_numeric_crt():
    mod = common.cob_module(crt_status=numbin4(0))
    common.cob_current_module = mod
    screenio.cob_check_pos_status(1064, yx=(0, 0))
    assert move.cob_get_int(mod.crt_status) == 1064


def test_check_pos_status_no_module_is_safe():
    common.cob_current_module = None
    # Must not raise when there is no current module.
    screenio.cob_check_pos_status(0, yx=(0, 0))


# ===========================================================================
# Graceful degradation: no-TTY init + termio fallback (AAP 0.3.4)
# ===========================================================================
def test_screen_init_without_tty_degrades():
    common.cob_exception_code = 0
    screenio.cob_screen_init()
    # No controlling terminal -> not initialised, fallback latched, exception set.
    assert screenio.cob_screen_initialized == 0
    assert screenio._curses_failed is True
    # cob_set_exception maps the EC-SCREEN-ITEM-TRUNCATED enum to hex 0F03.
    assert common.cob_exception_code == 0x0F03


def test_field_display_falls_back_to_termio(monkeypatch):
    calls = []
    monkeypatch.setattr(screenio.termio, "cob_display",
                        lambda *a, **k: calls.append(a))
    screenio.cob_field_display(alnum("HELLO"), None, None)
    assert len(calls) == 1
    # delegated with the field as the operand
    assert calls[0][3].data[:5] == bytearray(b"HELLO")


def test_field_accept_falls_back_to_termio(monkeypatch):
    calls = []
    monkeypatch.setattr(screenio.termio, "cob_accept",
                        lambda f: calls.append(f))
    common.cob_current_module = None       # cob_check_pos_status is then a no-op
    screenio.cob_field_accept(alnum("     "), None, None)
    assert len(calls) == 1


# ===========================================================================
# SCREEN-tree walk + screen-level DISPLAY/ACCEPT fallback
# ===========================================================================
def test_iter_screen_fields_bare_field():
    f = alnum("X")
    leaves = list(screenio._iter_screen_fields(f))
    assert leaves == [f]


def test_iter_screen_fields_none():
    assert list(screenio._iter_screen_fields(None)) == []


class _ScreenNode:
    def __init__(self, field=None, child=None, nxt=None):
        self.field = field
        self.child = child
        self.next = nxt


def test_iter_screen_fields_tree():
    f1 = alnum("A")
    f2 = alnum("B")
    # group node -> child list of two leaf fields
    leaf1 = _ScreenNode(field=f1)
    leaf2 = _ScreenNode(field=f2)
    leaf1.next = leaf2
    group = _ScreenNode(child=leaf1)
    leaves = list(screenio._iter_screen_fields(group))
    assert leaves == [f1, f2]


def test_screen_display_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(screenio.termio, "cob_display",
                        lambda *a, **k: calls.append(a[3]))
    f1 = alnum("A")
    f2 = alnum("B")
    leaf1 = _ScreenNode(field=f1)
    leaf2 = _ScreenNode(field=f2)
    leaf1.next = leaf2
    group = _ScreenNode(child=leaf1)
    screenio.cob_screen_display(group, None, None)
    assert calls == [f1, f2]


def test_screen_accept_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(screenio.termio, "cob_accept", lambda f: calls.append(f))
    common.cob_current_module = None
    f = alnum("     ")
    screenio.cob_screen_accept(f, None, None)
    assert calls == [f]


# ===========================================================================
# Subsystem lifecycle
# ===========================================================================
def test_set_mode():
    screenio.cob_screen_set_mode(1)
    assert screenio.cob_screen_mode == 1
    screenio.cob_screen_set_mode(0)
    assert screenio.cob_screen_mode == 0


def test_terminate_when_not_initialised_is_safe():
    # Calling terminate without an active screen must be a no-op (no raise).
    screenio.cob_screen_terminate()
    assert screenio.cob_screen_initialized == 0


def test_screen_line_col_without_tty_is_zero():
    f = numbin4(99)
    screenio.cob_screen_line_col(f, 0)   # line
    assert move.cob_get_int(f) == 0
    g = numbin4(99)
    screenio.cob_screen_line_col(g, 1)   # column
    assert move.cob_get_int(g) == 0


def test_screen_attr_noop_without_tty():
    # With no active screen, cob_screen_attr returns immediately (no raise).
    screenio.cob_screen_attr(None, None, 0)


def test_cob_init_screenio_resets_latch():
    screenio._curses_failed = True
    screenio.cob_init_screenio()
    assert screenio._curses_failed is False
    assert screenio.cob_screen_initialized == 0
    assert screenio.cob_screen_mode == 0


# ===========================================================================
# cob_screen - the SCREEN item descriptor the emitter constructs (replaces the
# C "static cob_screen s_N = { ... };" aggregate). Positional argument order
# mirrors the C struct __cob_screen (common.h L709-L721) exactly.
# ===========================================================================
class TestCobScreen:
    def test_type_constants_match_common_h(self):
        assert screenio.COB_SCREEN_TYPE_GROUP == 0
        assert screenio.COB_SCREEN_TYPE_FIELD == 1
        assert screenio.COB_SCREEN_TYPE_VALUE == 2
        assert screenio.COB_SCREEN_TYPE_ATTRIBUTE == 3

    def test_positional_constructor_struct_order(self):
        # next, child, field, value, line, column, foreg, backg, type, occurs, attr
        s = screenio.cob_screen(None, None, "F", "V", "L", "C", "FG", "BG",
                                screenio.COB_SCREEN_TYPE_FIELD, 3, 0x20)
        assert s.next is None and s.child is None
        assert s.field == "F" and s.value == "V"
        assert s.line == "L" and s.column == "C"
        assert s.foreg == "FG" and s.backg == "BG"
        assert s.type == 1 and s.occurs == 3 and s.attr == 0x20

    def test_group_links_child_and_sibling(self):
        child = screenio.cob_screen(type=screenio.COB_SCREEN_TYPE_FIELD)
        sib = screenio.cob_screen(type=screenio.COB_SCREEN_TYPE_FIELD)
        grp = screenio.cob_screen(sib, child, None, None, None, None, None, None,
                                  screenio.COB_SCREEN_TYPE_GROUP, 0, 0)
        assert grp.child is child and grp.next is sib
        assert grp.type == screenio.COB_SCREEN_TYPE_GROUP

    def test_defaults_are_group_zero(self):
        s = screenio.cob_screen()
        assert s.type == 0 and s.occurs == 0 and s.attr == 0

    def test_repr_is_safe(self):
        s = screenio.cob_screen(type=2, occurs=1, attr=5)
        assert "cob_screen" in repr(s)
