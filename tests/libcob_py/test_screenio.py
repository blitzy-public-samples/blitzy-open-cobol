"""Unit tests for :mod:`libcob_py.screenio` - the COBOL SCREEN SECTION I/O.

These tests verify the pure-Python port of ``libcob/screenio.c`` together with
the IMMUTABLE CRT-status constants defined in ``copy/screenio.cpy`` (AAP
sections 0.3.4 / 0.6.1).  The four pillars exercised here mirror the agent
specification:

* **Constant values** - all 83 CRT-status constants (8 colours ``0-7``,
  ``COB_SCR_OK=0``, 64 function keys ``F1..F64 = 1001..1064``, 8
  navigation/exception keys ``2001..2008``, ``COB_SCR_NO_FIELD=8000`` and
  ``COB_SCR_FATAL=9000``).
* **Headless importability** - importing the module must never require a TTY;
  :mod:`curses` is imported defensively and every curses call is guarded.
* **Key mapping** - ``cob_convert_key`` plus the ``cob_function_key_fret`` /
  ``_special_key_fret`` helpers that map raw curses key codes to the
  ``1001-1064`` / ``2001-2008`` CRT-status values.
* **Graceful degradation** - when ``initscr`` fails (unsupported terminal) the
  screen exception ``EC-SCREEN-ITEM-TRUNCATED`` (hex ``0F03``) is raised and
  DISPLAY / ACCEPT fall through to :mod:`libcob_py.termio` (AAP 0.3.4).

HARD CONSTRAINTS (AAP 0.5 / 0.7.1)
---------------------------------
* **Standard library only.**  ``pytest`` is a development-only test framework
  (never a runtime dependency) and is therefore permitted; no third-party
  runtime package is imported.
* **Dependency whitelist.**  The only runtime modules touched are
  ``libcob_py.screenio`` (the module under test) and its declared dependencies
  ``libcob_py.common`` and ``libcob_py.termio``.  Integer storage that the C
  tests performed through ``libcob_py.move`` is here routed through the
  whitelisted ``common._lazy_set_int`` / ``common._lazy_get_int`` bridges, so
  this test module never imports ``move`` directly.
* **No TTY required.**  The module is obtained with ``pytest.importorskip`` at
  import time (a clean skip if the parallel-built runtime is absent); the
  curses-driving tests are guarded by the ``requires_curses`` marker so nothing
  fails for lack of a terminal.
"""

import pytest

# --- Dependency whitelist: screenio (under test) + common + termio ----------
# Obtained lazily so a missing parallel-built runtime yields a clean SKIP
# rather than a hard collection error (conftest convention).  ``move`` is
# deliberately NOT imported - it is outside this file's dependency whitelist;
# integer field access goes through ``common``'s lazy bridges instead.
libcob_py = pytest.importorskip("libcob_py")
common = pytest.importorskip("libcob_py.common")
termio = pytest.importorskip("libcob_py.termio")
screenio = pytest.importorskip("libcob_py.screenio")

# The curses skip marker for the (few) tests that actually drive curses key
# constants / rendering.  The suite convention (see test_numeric_parity.py) is
# to import it from the package conftest; a defensive fallback rebuilds an
# equivalent marker so this module is importable even when the ``tests``
# package path is unusual (the constant/degradation tests never need it).
try:  # pragma: no cover - exercised implicitly by collection
    from tests.libcob_py.conftest import requires_curses
except Exception:  # pragma: no cover - fallback when package import is unavailable
    import importlib.util as _ilu

    requires_curses = pytest.mark.skipif(
        _ilu.find_spec("curses") is None,
        reason="curses not available on this platform",
    )


# ===========================================================================
# Field builders (standard-library only; no ``move`` import)
# ===========================================================================
def _numdisp(digits):
    """Build a ``NUMERIC_DISPLAY`` (zoned) field from a digit string."""
    data = digits.encode("latin-1")
    attr = common.cob_field_attr(
        type=common.COB_TYPE_NUMERIC_DISPLAY, digits=len(digits), scale=0,
        flags=0, pic=None)
    return common.cob_field(size=len(data), data=bytearray(data), attr=attr)


def _alnum(text, size=None):
    """Build an ``ALPHANUMERIC`` field from *text* (optionally space-padded)."""
    data = text.encode("latin-1") if isinstance(text, str) else bytes(text)
    if size is not None:
        data = data.ljust(size, b" ")[:size]
    attr = common.cob_field_attr(
        type=common.COB_TYPE_ALPHANUMERIC, digits=0, scale=0, flags=0, pic=None)
    return common.cob_field(size=len(data), data=bytearray(data), attr=attr)


def _numbin4(value=0):
    """Build a 4-byte signed BINARY field, seeded via the whitelisted bridge.

    Integer storage goes through ``common._lazy_set_int`` (which internally
    defers to the data-movement runtime) so this test file stays strictly
    within its dependency whitelist - it never imports ``libcob_py.move``.
    """
    f = common.cob_field(
        size=4, data=bytearray(4),
        attr=common.cob_field_attr(type=common.COB_TYPE_NUMERIC_BINARY,
                                   digits=8, scale=0,
                                   flags=common.COB_FLAG_HAVE_SIGN, pic=None))
    common._lazy_set_int(f, value)
    return f


@pytest.fixture(autouse=True)
def _reset_screen_state():
    """Isolate every test from the module-level screen latches.

    ``screenio`` keeps process-global state (the curses init flag, the failure
    latch, colour/size caps and the input table) mirroring the file-scope
    globals in ``screenio.c``.  Re-seeding it via :func:`cob_init_screenio`
    before and after each test guarantees order-independence; the current
    module pointer and the exception code (owned by :mod:`libcob_py.common`)
    are saved and restored so screen tests never leak into sibling suites.
    """
    saved_mod = common.cob_current_module
    saved_exc = common.cob_exception_code
    screenio.cob_init_screenio()
    yield
    screenio.cob_init_screenio()
    common.cob_current_module = saved_mod
    common.cob_exception_code = saved_exc


# ===========================================================================
# The authoritative 83 CRT-status constants (copy/screenio.cpy).
#
# Built explicitly so the test asserts the GROUND TRUTH independently of the
# module's own definitions: 8 colours + 1 (OK) + 64 function keys + 8
# navigation keys + 2 (NO_FIELD, FATAL) = 83.
# ===========================================================================
def _build_expected_constants():
    expected = {
        # 8 colours (COB-COLOR-*, values 0-7)
        "COB_COLOR_BLACK": 0,
        "COB_COLOR_BLUE": 1,
        "COB_COLOR_GREEN": 2,
        "COB_COLOR_CYAN": 3,
        "COB_COLOR_RED": 4,
        "COB_COLOR_MAGENTA": 5,
        "COB_COLOR_YELLOW": 6,
        "COB_COLOR_WHITE": 7,
        # normal return
        "COB_SCR_OK": 0,
    }
    # 64 function keys: COB_SCR_F<n> = 1000 + n for n in 1..64
    for n in range(1, 65):
        expected["COB_SCR_F%d" % n] = 1000 + n
    # 8 navigation / exception keys (values 2001..2008)
    expected.update({
        "COB_SCR_PAGE_UP": 2001,
        "COB_SCR_PAGE_DOWN": 2002,
        "COB_SCR_KEY_UP": 2003,
        "COB_SCR_KEY_DOWN": 2004,
        "COB_SCR_ESC": 2005,
        "COB_SCR_PRINT": 2006,
        "COB_SCR_TAB": 2007,
        "COB_SCR_BACK_TAB": 2008,
        # input-validation + fatal
        "COB_SCR_NO_FIELD": 8000,
        "COB_SCR_FATAL": 9000,
    })
    return expected


EXPECTED_CONSTANTS = _build_expected_constants()


# ===========================================================================
# Phase 1 - CRT-status constant values (headline; runs headless)
# ===========================================================================
def test_all_83_constants_present_and_valued():
    """Every one of the 83 CRT-status constants is defined with the EXACT value."""
    # Sanity: the expected mapping itself must contain exactly 83 entries.
    assert len(EXPECTED_CONSTANTS) == 83
    missing = [name for name in EXPECTED_CONSTANTS if not hasattr(screenio, name)]
    assert not missing, "screenio is missing CRT-status constants: %s" % missing
    for name, value in EXPECTED_CONSTANTS.items():
        actual = getattr(screenio, name)
        assert actual == value, (
            "screenio.%s == %r, expected %r" % (name, actual, value))


def test_constant_count_83():
    """The CRT-status surface comprises exactly 83 constants; spot-check edges."""
    # The module advertises its own count via a compile-time self-check.
    assert screenio.CRT_STATUS_CONSTANT_COUNT == 83
    assert len(EXPECTED_CONSTANTS) == 83
    # Boundary spot-checks across each value band.
    assert screenio.COB_SCR_OK == 0
    assert screenio.COB_COLOR_WHITE == 7
    assert screenio.COB_SCR_F1 == 1001
    assert screenio.COB_SCR_F64 == 1064
    assert screenio.COB_SCR_PAGE_UP == 2001
    assert screenio.COB_SCR_BACK_TAB == 2008
    assert screenio.COB_SCR_NO_FIELD == 8000
    assert screenio.COB_SCR_FATAL == 9000


def test_function_keys_contiguous():
    """F1..F64 are all present and equal ``1000 + n`` (contiguous, no gaps)."""
    for n in range(1, 65):
        name = "COB_SCR_F%d" % n
        assert hasattr(screenio, name), "missing %s" % name
        assert getattr(screenio, name) == 1000 + n
    # The band is exactly [1001, 1064] - no F0 and no F65.
    assert not hasattr(screenio, "COB_SCR_F0")
    assert not hasattr(screenio, "COB_SCR_F65")


# ===========================================================================
# Phase 2 - Importable with no TTY (the import at the top already succeeded)
# ===========================================================================
def test_import_no_tty():
    """The module imported cleanly with no controlling terminal.

    ``pytest.importorskip`` succeeded at module top, so the import itself did
    not require a TTY.  Confirm the module object is usable, a constant is
    accessible, and importing did NOT eagerly initialise a curses screen
    (curses is imported defensively and screen init is lazy).
    """
    import types

    assert isinstance(screenio, types.ModuleType)
    # A constant is accessible without touching a terminal.
    assert screenio.COB_SCR_OK == 0
    # The defensive curses import attribute exists (the real module or None);
    # either way the import did not raise for lack of a terminal.
    assert hasattr(screenio, "_curses")
    # Importing must not have started a screen (lazy init contract).
    assert screenio.cob_screen_initialized == 0
    assert screenio._curses_failed is False


# ===========================================================================
# Phase 3 - Key mapping (cob_convert_key + the fret helpers)
# ===========================================================================
def test_function_key_fret_range():
    """``cob_function_key_fret`` maps F1..F64 to 1001..1064 (pure integer math).

    This helper does not reference any ``curses.KEY_*`` constant - it is the
    open-interval test ``key_f0 < keyp < key_f0 + 65 -> 1000 + (keyp - key_f0)``
    - so it is validated headless against an arbitrary ``key_f0`` base.
    """
    key_f0 = 264  # the conventional curses KEY_F0 value; any base works
    assert screenio.cob_function_key_fret(key_f0 + 1, key_f0) == 1001
    assert screenio.cob_function_key_fret(key_f0 + 64, key_f0) == 1064
    # boundaries are exclusive: F0 itself and F65 are out of range -> None
    assert screenio.cob_function_key_fret(key_f0, key_f0) is None
    assert screenio.cob_function_key_fret(key_f0 + 65, key_f0) is None


@requires_curses
def test_convert_key():
    """``cob_convert_key`` + the fret helpers map curses key codes correctly.

    Drives the representative key classes the agent specification calls out:

    * control characters (Enter / Tab / Backspace) are normalised to the
      canonical ``curses.KEY_*`` values;
    * a function-key code maps into the ``1001..1064`` band via
      :func:`cob_function_key_fret`;
    * page up / down map to ``2001`` / ``2002`` and arrow up / down to
      ``2003`` / ``2004`` via :func:`_special_key_fret`.

    Guarded by ``requires_curses`` because it references ``curses.KEY_*``.
    """
    c = screenio._curses
    assert c is not None  # guaranteed by the marker

    # --- control-character normalisation (cob_convert_key) -----------------
    assert screenio.cob_convert_key(ord("\n"), False) == c.KEY_ENTER
    assert screenio.cob_convert_key(ord("\r"), False) == c.KEY_ENTER
    assert screenio.cob_convert_key(ord("\t"), False) == c.KEY_STAB
    assert screenio.cob_convert_key(ord("\b"), False) == c.KEY_BACKSPACE
    # ESC is ignored (-> 0) unless extended status + ESC handling are enabled.
    assert screenio.cob_convert_key(0o33, False) == 0

    # --- function-key band: F1 .. F64 -> 1001 .. 1064 ----------------------
    assert screenio.cob_function_key_fret(c.KEY_F0 + 1, c.KEY_F0) == 1001
    assert screenio.cob_function_key_fret(c.KEY_F0 + 64, c.KEY_F0) == 1064

    # --- navigation / exception keys -> 2001 .. 2008 -----------------------
    assert screenio._special_key_fret(c.KEY_PPAGE) == 2001
    assert screenio._special_key_fret(c.KEY_NPAGE) == 2002
    assert screenio._special_key_fret(c.KEY_UP) == 2003
    assert screenio._special_key_fret(c.KEY_DOWN) == 2004
    assert screenio._special_key_fret(0o33) == 2005  # ESC
    # an ordinary editing key does not terminate the ACCEPT
    assert screenio._special_key_fret(ord("A")) is None


@requires_curses
def test_special_key_fret_full_map():
    """Every navigation/exception key resolves to its documented 2xxx value."""
    c = screenio._curses
    assert screenio._special_key_fret(c.KEY_PRINT) == 2006
    assert screenio._special_key_fret(c.KEY_STAB) == 2007
    assert screenio._special_key_fret(c.KEY_BTAB) == 2008


@requires_curses
def test_convert_key_ppage_ignored_without_extended_status():
    """PAGE-UP / PAGE-DOWN are zeroed unless ``COB_SCREEN_EXCEPTIONS`` is on."""
    c = screenio._curses
    assert screenio.cob_extended_status == 0
    assert screenio.cob_convert_key(c.KEY_PPAGE, False) == 0
    assert screenio.cob_convert_key(c.KEY_NPAGE, False) == 0


@requires_curses
def test_convert_key_up_down_ignored_in_field_accept():
    """Cursor UP/DOWN are ignored during a single-field ACCEPT, else pass through."""
    c = screenio._curses
    # field_accept truthy + extended status off -> ignored (0)
    assert screenio.cob_convert_key(c.KEY_UP, 1) == 0
    assert screenio.cob_convert_key(c.KEY_DOWN, 1) == 0
    # not a field ACCEPT -> the key passes through unchanged
    assert screenio.cob_convert_key(c.KEY_UP, 0) == c.KEY_UP


@requires_curses
def test_curses_color_mapping():
    """``_curses_color`` maps the 8 COB-COLOR codes to curses ``COLOR_*``."""
    c = screenio._curses
    assert screenio._curses_color(screenio.COB_COLOR_BLACK) == c.COLOR_BLACK
    assert screenio._curses_color(screenio.COB_COLOR_BLUE) == c.COLOR_BLUE
    assert screenio._curses_color(screenio.COB_COLOR_WHITE) == c.COLOR_WHITE
    # out-of-range code leaves the colour unchanged (None)
    assert screenio._curses_color(99) is None


# ===========================================================================
# Phase 4 - Graceful degradation (CRITICAL; AAP 0.3.4 / 0.6.1)
#
# When the screen subsystem cannot be brought up (no TTY / unsupported
# terminal), screenio must (1) raise EC-SCREEN-ITEM-TRUNCATED (hex 0F03) and
# (2) fall through to the plain libcob_py.termio ACCEPT/DISPLAY path.  All of
# these tests run headless - the failure is simulated deterministically.
# ===========================================================================
class _FakeCursesInitFails(object):
    """A minimal ``curses`` stand-in whose :meth:`initscr` always raises.

    Assigning this to ``screenio._curses`` forces the curses-init *failure*
    branch deterministically, independent of the host terminal, so the
    graceful-degradation contract can be asserted in headless CI.
    """

    error = Exception

    def initscr(self):
        raise RuntimeError("simulated unsupported terminal")


def test_initscr_failure_degrades_to_termio(monkeypatch):
    """initscr failure -> EC-SCREEN-ITEM-TRUNCATED (0F03) -> termio fallback.

    This is the headline degradation test mandated by AAP 0.3.4 / 0.6.1.  It
    asserts BOTH halves of the contract:

    1. ``cob_screen_init`` fails gracefully: the screen is NOT marked
       initialised, the fallback latch is set, and the COBOL exception
       ``EC-SCREEN-ITEM-TRUNCATED`` (hex ``0F03``) is dispatched through
       :mod:`libcob_py.common`.
    2. A subsequent field DISPLAY falls through to
       :func:`libcob_py.termio.cob_display`, so the value still reaches the
       user instead of aborting.
    """
    # Simulate an unsupported terminal: initscr raises.
    monkeypatch.setattr(screenio, "_curses", _FakeCursesInitFails())
    common.cob_exception_code = 0

    # (1) Lazy init fails and degrades.
    screenio.cob_screen_init()
    assert common.cob_exception_code == 0x0F03, "EC-SCREEN-ITEM-TRUNCATED expected"
    assert screenio._curses_failed is True
    assert screenio.cob_screen_initialized == 0

    # (2) DISPLAY now delegates to the plain termio path.
    calls = []
    monkeypatch.setattr(screenio.termio, "cob_display",
                        lambda *a, **k: calls.append(a))
    screenio.cob_field_display(_alnum("HELLO"), None, None)
    assert len(calls) == 1, "field DISPLAY must fall through to termio.cob_display"
    # termio.cob_display(to_stderr=False, newline=True, varcnt=1, field) -> field at index 3
    assert bytes(calls[0][3].data[:5]) == b"HELLO"


def test_initscr_failure_accept_degrades_to_termio(monkeypatch):
    """initscr failure -> field ACCEPT falls through to termio.cob_accept."""
    monkeypatch.setattr(screenio, "_curses", _FakeCursesInitFails())
    common.cob_current_module = None  # so cob_check_pos_status is a safe no-op

    accepted = []
    monkeypatch.setattr(screenio.termio, "cob_accept",
                        lambda f: accepted.append(f))
    screenio.cob_field_accept(_alnum("     "), None, None)
    assert len(accepted) == 1, "field ACCEPT must fall through to termio.cob_accept"


def test_screen_init_reads_env_then_degrades(monkeypatch):
    """Env options are read before initscr, then degradation latches on failure.

    Drives the environment-driven option block (``COB_SCREEN_EXCEPTIONS`` /
    ``COB_SCREEN_ESC`` / ``COB_INSERT_MODE``) and then the initscr ``except``
    arm, all without a real terminal.
    """
    monkeypatch.setattr(screenio, "_curses", _FakeCursesInitFails())
    monkeypatch.setenv("COB_SCREEN_EXCEPTIONS", "Y")
    monkeypatch.setenv("COB_SCREEN_ESC", "Y")
    monkeypatch.setenv("COB_INSERT_MODE", "Y")
    common.cob_exception_code = 0

    screenio.cob_screen_init()
    # Environment options were applied before initscr was attempted.
    assert screenio.cob_extended_status == 1
    assert screenio.cob_use_esc == 1
    assert screenio.insert_mode == 1
    # initscr then failed -> graceful degradation.
    assert screenio.cob_screen_initialized == 0
    assert screenio._curses_failed is True
    assert common.cob_exception_code == 0x0F03


def test_screen_init_no_curses_module_degrades(monkeypatch):
    """With no curses module at all, init degrades immediately (no env read)."""
    monkeypatch.setattr(screenio, "_curses", None)
    common.cob_exception_code = 0
    screenio.cob_screen_init()
    assert screenio.cob_screen_initialized == 0
    assert screenio._curses_failed is True
    assert common.cob_exception_code == 0x0F03


# ---------------------------------------------------------------------------
# Partial curses-init failure (Checkpoint-2 review MAJOR finding regression)
#
# initscr() SUCCEEDS (the terminal is now switched into curses mode) but a
# subsequent setup call raises.  cob_screen_init must restore the terminal on
# the exception path - call nocbreak()/echo()/endwin() (the same teardown
# cob_screen_terminate() performs, screenio.c L441-L448) - BEFORE latching the
# termio fallback, so the user's shell is not left in a broken raw/no-echo
# state.  These run headless; the failure is simulated deterministically.
# ---------------------------------------------------------------------------
class _FakeStdscrPartial(object):
    """Minimal stdscr returned by :class:`_FakeCursesPartialInit`.

    ``initscr()`` returns this object so ``screenio._stdscr`` becomes non-None
    (the terminal is in curses mode).  ``keypad`` / ``getmaxyx`` succeed unless
    the owning fake is configured to fail at that step.
    """

    def __init__(self, owner):
        self._owner = owner

    def keypad(self, flag):
        if self._owner.fail_on == "keypad":
            raise RuntimeError("simulated keypad failure")

    def getmaxyx(self):
        if self._owner.fail_on == "getmaxyx":
            raise RuntimeError("simulated getmaxyx failure")
        return (24, 80)


class _FakeCursesPartialInit(object):
    """A ``curses`` stand-in whose ``initscr()`` succeeds but a later setup fails.

    Reproduces the partial-initialisation hazard from the Checkpoint-2 review:
    ``initscr()`` switches the terminal into curses mode and then a subsequent
    mode/size call (``cbreak``/``keypad``/``nl``/``noecho``/``getmaxyx``) raises.
    The teardown entry points (``nocbreak``/``echo``/``endwin``) are recorded so
    a test can assert the terminal is restored on the exception path before
    degrading to termio.
    """

    error = Exception

    def __init__(self, fail_on="noecho"):
        self.fail_on = fail_on
        self.endwin_called = 0
        self.nocbreak_called = 0
        self.echo_called = 0
        self._screen = _FakeStdscrPartial(self)

    # --- init + setup (initscr succeeds; the configured step raises) --------
    def initscr(self):
        return self._screen

    def cbreak(self):
        if self.fail_on == "cbreak":
            raise RuntimeError("simulated cbreak failure")

    def nl(self):
        if self.fail_on == "nl":
            raise RuntimeError("simulated nl failure")

    def noecho(self):
        if self.fail_on == "noecho":
            raise RuntimeError("simulated noecho failure")

    def has_colors(self):
        return False

    # --- teardown entry points the exception path must invoke ---------------
    def nocbreak(self):
        self.nocbreak_called += 1

    def echo(self):
        self.echo_called += 1

    def endwin(self):
        self.endwin_called += 1


@pytest.mark.parametrize("fail_on", ["cbreak", "keypad", "nl", "noecho", "getmaxyx"])
def test_partial_init_failure_restores_terminal(monkeypatch, fail_on):
    """Partial curses init -> terminal restored (endwin) THEN degrade to termio.

    Regression test for the Checkpoint-2 MAJOR finding (``screenio.py`` L409-415).
    ``initscr()`` succeeds - so the terminal is in curses mode - but a later
    setup call raises.  ``cob_screen_init`` MUST restore the terminal on the
    exception path (``nocbreak``/``echo``/``endwin``) before arming the termio
    fallback.  Every failure position is exercised to prove the restore happens
    regardless of where after ``initscr`` the failure occurs.
    """
    fake = _FakeCursesPartialInit(fail_on=fail_on)
    # The partial-init path is only reachable when curses is "available": reset
    # the fallback latch and the init flag so cob_screen_init enters the try.
    monkeypatch.setattr(screenio, "_curses", fake)
    monkeypatch.setattr(screenio, "_curses_failed", False)
    monkeypatch.setattr(screenio, "cob_screen_initialized", 0)
    monkeypatch.setattr(screenio, "_stdscr", None)
    common.cob_exception_code = 0

    screenio.cob_screen_init()

    # (1) Terminal restored on the exception path: endwin() MUST be called (the
    #     C teardown contract), with the cbreak/noecho mode-restores attempted.
    assert fake.endwin_called == 1, "endwin() must be called to restore the terminal"
    assert fake.nocbreak_called == 1, "nocbreak() must undo cbreak() on the failure path"
    assert fake.echo_called == 1, "echo() must undo noecho() on the failure path"
    # (2) Graceful degradation still latches: screen NOT initialised, fallback
    #     armed, EC-SCREEN-ITEM-TRUNCATED (hex 0F03) dispatched, _stdscr cleared.
    assert screenio.cob_screen_initialized == 0
    assert screenio._stdscr is None
    assert screenio._curses_failed is True
    assert common.cob_exception_code == 0x0F03


def test_partial_init_failure_then_display_degrades_to_termio(monkeypatch):
    """After a partial-init failure (with restore), DISPLAY still reaches termio.

    Confirms the new terminal-restore path does not break the second half of the
    graceful-degradation contract (AAP 0.3.4): once the screen fails to come up,
    a field DISPLAY is routed to :func:`libcob_py.termio.cob_display`.
    """
    fake = _FakeCursesPartialInit(fail_on="noecho")
    monkeypatch.setattr(screenio, "_curses", fake)
    monkeypatch.setattr(screenio, "_curses_failed", False)
    monkeypatch.setattr(screenio, "cob_screen_initialized", 0)
    monkeypatch.setattr(screenio, "_stdscr", None)
    common.cob_exception_code = 0

    screenio.cob_screen_init()
    assert fake.endwin_called == 1, "endwin() restores the terminal on partial init"
    assert screenio._curses_failed is True

    calls = []
    monkeypatch.setattr(screenio.termio, "cob_display",
                        lambda *a, **k: calls.append(a))
    screenio.cob_field_display(_alnum("HELLO"), None, None)
    assert len(calls) == 1, "DISPLAY must fall through to termio after partial-init failure"
    # termio.cob_display(to_stderr=False, newline=True, varcnt=1, field) -> field at index 3
    assert bytes(calls[0][3].data[:5]) == b"HELLO"


def test_screen_init_real_environment_degrades_or_skips():
    """Exercise the REAL initscr in the actual (headless) environment.

    No simulation: in headless CI the genuine ``curses.initscr`` fails and the
    subsystem degrades, raising ``0F03`` and latching the termio fallback.  On
    the rare host that does provide a drivable terminal, the screen would
    initialise instead - that is cleaned up and the degradation assertions are
    skipped (they only apply without a TTY).  Either way the suite never fails
    for lack of a terminal (AAP 0.3.4).
    """
    common.cob_exception_code = 0
    screenio.cob_screen_init()
    if screenio.cob_screen_initialized:  # pragma: no cover - only with a live TTY
        screenio.cob_screen_terminate()
        pytest.skip("a real curses terminal is available; degradation N/A")
    assert screenio._curses_failed is True
    assert common.cob_exception_code == 0x0F03


def test_screen_display_fallback(monkeypatch):
    """Screen-level DISPLAY degrades to a plain termio DISPLAY of each leaf."""
    monkeypatch.setattr(screenio, "_curses", _FakeCursesInitFails())
    seen = []
    monkeypatch.setattr(screenio.termio, "cob_display",
                        lambda *a, **k: seen.append(a[3]))
    f1 = _alnum("A")
    f2 = _alnum("B")
    leaf1 = screenio.cob_screen(field=f1, type=screenio.COB_SCREEN_TYPE_FIELD)
    leaf2 = screenio.cob_screen(field=f2, type=screenio.COB_SCREEN_TYPE_FIELD)
    leaf1.next = leaf2
    group = screenio.cob_screen(child=leaf1, type=screenio.COB_SCREEN_TYPE_GROUP)
    screenio.cob_screen_display(group, None, None)
    assert seen == [f1, f2]


def test_screen_accept_fallback(monkeypatch):
    """Screen-level ACCEPT degrades to a plain termio ACCEPT of each input leaf."""
    monkeypatch.setattr(screenio, "_curses", _FakeCursesInitFails())
    common.cob_current_module = None
    seen = []
    monkeypatch.setattr(screenio.termio, "cob_accept", lambda f: seen.append(f))
    f = _alnum("     ")
    node = screenio.cob_screen(field=f, type=screenio.COB_SCREEN_TYPE_FIELD)
    screenio.cob_screen_accept(node, None, None)
    assert seen == [f]


# ===========================================================================
# Phase 5 - Screen ops under curses (OPTIONAL; marked, never fails headless)
# ===========================================================================
@requires_curses
def test_screen_display_accept_under_curses():
    """OPTIONAL smoke test of a real curses DISPLAY when a terminal is drivable.

    In a headless environment ``cob_screen_init`` cannot bring up the screen,
    so the screen stays uninitialised and we SKIP cleanly - the absence of a
    TTY must never fail the suite (AAP Phase 5).  When a real terminal is
    available the screen is brought up, a one-field SCREEN item is DISPLAYed,
    and the subsystem is torn down.
    """
    screenio.cob_screen_init()
    if not screenio.cob_screen_initialized:
        pytest.skip("no drivable curses terminal (headless); screen ops skipped")
    try:  # pragma: no cover - only reachable on a live terminal
        node = screenio.cob_screen(field=_alnum("HI"),
                                   type=screenio.COB_SCREEN_TYPE_FIELD)
        screenio.cob_screen_display(node, 1, 1)
        screenio.cob_field_display(_alnum("X"), 1, 1)
    finally:  # pragma: no cover - only reachable on a live terminal
        screenio.cob_screen_terminate()


# ===========================================================================
# Supporting coverage - the pure decode/translate helpers and the SCREEN-tree
# walk + lifecycle that are reachable WITHOUT a controlling terminal.  These
# round out >=80% line coverage of screenio.py (AAP 0.7.1 coverage gate).
# ===========================================================================

# --- get_line_column: all four operand forms (screenio.c L213-L250) --------
def test_get_line_column_none():
    assert screenio.get_line_column(None, None) == (0, 0)


def test_get_line_column_combined_four_byte():
    # 4-byte field -> /100, %100 ; then 1-based -> 0-based decrement.
    fline = _numdisp("1234")  # line 12, col 34 -> (11, 33)
    assert screenio.get_line_column(fline, None) == (11, 33)


def test_get_line_column_combined_other_width():
    # non-4-byte field -> /1000, %1000.
    fline = _numdisp("12034")  # line 12, col 34 -> (11, 33)
    assert screenio.get_line_column(fline, None) == (11, 33)


def test_get_line_column_separate():
    fline = _numdisp("5")
    fcol = _numdisp("10")
    assert screenio.get_line_column(fline, fcol) == (4, 9)


# --- cob_check_pos_status (screenio.c L450-L489) ---------------------------
def test_check_pos_status_alnum_crt_and_cursor():
    mod = common.cob_module(crt_status=_alnum("0000"), cursor_pos=_alnum("0000"))
    common.cob_current_module = mod
    common.cob_exception_code = 0
    screenio.cob_check_pos_status(1007, yx=(4, 9))
    assert bytes(mod.crt_status.data[:4]) == b"1007"
    # 4-digit cursor encoding (size < 6): line*100 + col = 409 -> "0409"
    assert bytes(mod.cursor_pos.data[:4]) == b"0409"
    # a non-zero fret raises EC-IMP-ACCEPT
    assert common.cob_exception_code != 0


def test_check_pos_status_six_digit_cursor():
    mod = common.cob_module(crt_status=_alnum("0000"), cursor_pos=_alnum("000000"))
    common.cob_current_module = mod
    screenio.cob_check_pos_status(0, yx=(4, 9))
    # 6-digit cursor encoding (size >= 6): line*1000 + col = 4009 -> "004009"
    assert bytes(mod.cursor_pos.data[:6]) == b"004009"


def test_check_pos_status_numeric_crt():
    mod = common.cob_module(crt_status=_numbin4(0))
    common.cob_current_module = mod
    screenio.cob_check_pos_status(1064, yx=(0, 0))
    # numeric CRT STATUS stores the integer fret directly
    assert common._lazy_get_int(mod.crt_status) == 1064


def test_check_pos_status_numeric_nondisplay_cursor():
    # A non-DISPLAY numeric CURSOR field is encoded as line*1000 + column.
    mod = common.cob_module(cursor_pos=_numbin4(0))
    common.cob_current_module = mod
    screenio.cob_check_pos_status(0, yx=(2, 3))
    assert common._lazy_get_int(mod.cursor_pos) == 2 * 1000 + 3


def test_check_pos_status_no_module_is_safe():
    common.cob_current_module = None
    # Must not raise when there is no current module.
    screenio.cob_check_pos_status(0, yx=(0, 0))


# --- SCREEN-tree leaf iteration (_iter_screen_fields) ----------------------
def test_iter_screen_fields_bare_field():
    f = _alnum("X")
    assert list(screenio._iter_screen_fields(f)) == [f]


def test_iter_screen_fields_none():
    assert list(screenio._iter_screen_fields(None)) == []


def test_iter_screen_fields_tree():
    f1 = _alnum("A")
    f2 = _alnum("B")
    leaf1 = screenio.cob_screen(field=f1, type=screenio.COB_SCREEN_TYPE_FIELD)
    leaf2 = screenio.cob_screen(field=f2, type=screenio.COB_SCREEN_TYPE_FIELD)
    leaf1.next = leaf2
    group = screenio.cob_screen(child=leaf1, type=screenio.COB_SCREEN_TYPE_GROUP)
    assert list(screenio._iter_screen_fields(group)) == [f1, f2]


# --- Subsystem lifecycle ---------------------------------------------------
def test_set_mode():
    screenio.cob_screen_set_mode(1)
    assert screenio.cob_screen_mode == 1
    screenio.cob_screen_set_mode(0)
    assert screenio.cob_screen_mode == 0


def test_terminate_when_not_initialised_is_safe():
    screenio.cob_screen_terminate()  # no active screen -> no-op, must not raise
    assert screenio.cob_screen_initialized == 0


def test_screen_line_col_without_tty_is_zero():
    f = _numbin4(99)
    screenio.cob_screen_line_col(f, 0)   # LINES
    assert common._lazy_get_int(f) == 0
    g = _numbin4(99)
    screenio.cob_screen_line_col(g, 1)   # COLS
    assert common._lazy_get_int(g) == 0


def test_screen_attr_noop_without_tty():
    # With no active screen, cob_screen_attr returns immediately (no raise).
    screenio.cob_screen_attr(None, None, 0)


def test_cob_init_screenio_resets_latch():
    screenio._curses_failed = True
    screenio.cob_screen_mode = 1
    screenio.cob_init_screenio()
    assert screenio._curses_failed is False
    assert screenio.cob_screen_initialized == 0
    assert screenio.cob_screen_mode == 0


def test_screen_init_idempotent_when_already_initialised(monkeypatch):
    # The C guard `if (cob_screen_initialized) return;` - a second call is a
    # no-op that leaves the (notionally active) screen untouched.
    monkeypatch.setattr(screenio, "cob_screen_initialized", 1)
    screenio.cob_screen_init()
    assert screenio.cob_screen_initialized == 1


def test_terminate_when_active_calls_endwin(monkeypatch):
    calls = []

    class _Win(object):
        pass

    class _FakeC(object):
        def endwin(self):
            calls.append("endwin")

    monkeypatch.setattr(screenio, "cob_screen_initialized", 1)
    monkeypatch.setattr(screenio, "_stdscr", _Win())
    monkeypatch.setattr(screenio, "_curses", _FakeC())
    screenio.cob_screen_terminate()
    assert screenio.cob_screen_initialized == 0
    assert screenio._stdscr is None
    assert calls == ["endwin"]


# --- cob_screen descriptor (the emitter-constructed SCREEN item) -----------
class TestCobScreen:
    """The ``cob_screen`` struct mirror the rewritten emitter constructs.

    Positional argument order must mirror the C struct ``__cob_screen``
    (common.h L709-L721) EXACTLY so the emitter's positional constructor maps
    one-to-one.
    """

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
        assert "cob_screen" in repr(screenio.cob_screen(type=2, occurs=1, attr=5))


# --- Multi-field SCREEN preparation (cob_prep_input / cob_screen_get_all) ---
def _input_field(text="     "):
    return screenio.cob_screen(
        field=_alnum(text), type=screenio.COB_SCREEN_TYPE_FIELD,
        attr=common.COB_SCREEN_INPUT)


def test_prep_input_collects_input_fields():
    fld1 = _input_field()
    fld2 = _input_field()
    fld1.next = fld2
    grp = screenio.cob_screen(child=fld1, type=screenio.COB_SCREEN_TYPE_GROUP)

    screenio._cob_base_inp = []
    screenio._totl_index = 0
    screenio.cob_prep_input(grp)

    assert screenio._totl_index == 2
    assert [e.scr for e in screenio._cob_base_inp] == [fld1, fld2]
    # captured cursor positions default to 0 with no active terminal
    assert all(e.this_y == 0 and e.this_x == 0 for e in screenio._cob_base_inp)


def test_prep_input_value_and_attribute_nodes_collect_nothing():
    val = screenio.cob_screen(value=_alnum("LABEL"),
                              type=screenio.COB_SCREEN_TYPE_VALUE, occurs=3)
    attrnode = screenio.cob_screen(type=screenio.COB_SCREEN_TYPE_ATTRIBUTE)
    val.next = attrnode
    grp = screenio.cob_screen(child=val, type=screenio.COB_SCREEN_TYPE_GROUP)

    screenio._cob_base_inp = []
    screenio._totl_index = 0
    screenio.cob_prep_input(grp)
    assert screenio._totl_index == 0
    assert screenio._cob_base_inp == []


def test_prep_input_none_is_safe():
    screenio.cob_prep_input(None)  # must not raise


def test_screen_puts_noop_without_tty():
    # No active screen -> cob_screen_puts returns immediately (no raise).
    screenio.cob_screen_puts(_input_field("AB"), _alnum("AB"))


def test_screen_get_all_defensive_guard_without_tty():
    # Without a terminal cob_screen_get_all returns the read-error status 8001
    # and never blocks.
    screenio._global_return = 0
    assert screenio.cob_screen_get_all() == 8001
    assert screenio._global_return == 8001
