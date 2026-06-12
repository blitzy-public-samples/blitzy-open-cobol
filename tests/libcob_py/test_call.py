"""Unit tests for :mod:`libcob_py.call` - the pure-Python dynamic CALL loader.

``libcob_py/call.py`` replaces the C runtime's 131-bucket name hash plus
``lt_dlopen``/``lt_dlsym`` resolver (``libcob/call.c``) with
:func:`importlib.import_module`, ``sys.path`` manipulation (seeded from
``COB_LIBRARY_PATH``) and ``sys.modules`` eviction for CANCEL (AAP sections
0.3.2 "Dynamic loader" and 0.4.1).  This suite verifies, against the behaviour
encoded in ``libcob/call.c``, that:

* program-name encoding (``cob_encode_program_id`` / ``cb_encode_program_id``)
  and ``COB_LOAD_CASE`` folding (``_apply_case``) are byte-for-byte faithful;
* the search path (``cob_set_library_path``, ``call.c`` L177-L202) and the call
  cache (``lookup`` / ``insert`` / ``cob_set_cancel``) behave as the C statics;
* a generated-style module resolves dynamically, a repeat resolve hits the
  cache (``importlib.import_module`` is *not* re-invoked), the not-found path
  raises ``EC-PROGRAM-NOT-FOUND`` and the aborting ``*_1`` variants stop the run
  unit;
* field-addressed resolution works (``cob_call_resolve`` / ``cob_field_cancel``);
* CANCEL evicts from ``sys.modules``, calls :func:`importlib.invalidate_caches`
  and a subsequent resolve re-imports a *fresh* module (``cobcancel``,
  ``call.c`` L481-L519);
* ``cob_init_call`` (``call.c`` L520-L600) wires the environment
  (``COB_LIBRARY_PATH`` -> ``sys.path``, ``COB_PRE_LOAD`` preloads,
  ``COB_LOAD_CASE`` folding) and registers the 43 ``CBL_``/``C$`` system
  routines from :mod:`libcob_py.system` into the resolver;
* the BY CONTENT / BY VALUE argument-passing wrappers and the ``cob_strdup`` /
  ``cob_get_buff`` helpers (``call.c`` L165-L213) that the emitter lowers CALL
  idioms onto behave exactly like their C originals.

HARD CONSTRAINTS (AAP sections 0.5 / 0.7.1)
-------------------------------------------
* **Standard library only.**  ``importlib`` / ``os`` / ``sys`` are standard
  library; ``pytest`` is a development-only test framework (never a runtime
  dependency).  No third-party package is imported.
* **Clean skip when the runtime is absent.**  The module under test is obtained
  with :func:`pytest.importorskip` at module top level (suite convention,
  documented in ``conftest.py``).
* **No state leakage.**  ``call`` keeps module-level resolver state
  (``_call_cache`` / ``_cancel_handlers`` / ``_resolve_paths`` / ``name_convert``
  / ``_resolve_error``) and the loader mutates the process-global ``sys.path`` /
  ``sys.modules``; the autouse :func:`_isolate_call_state` fixture snapshots and
  restores all of it (plus the touched ``common`` globals) so the dynamic-import
  tests never leak into sibling test modules.
* **Fixtures from conftest.**  The dynamic-loader tests consume the shared
  ``lib_dir`` / ``clean_cob_env`` / ``set_cob_env`` fixtures (``conftest.py``).
"""
# --- Standard-library imports (stdlib only; no third-party packages) -------
import importlib
import os
import sys

import pytest

# --- Runtime under test + its declared in-package dependencies -------------
# importorskip yields a clean SKIP (not a collection error) when the
# parallel-built runtime package is not yet importable (conftest convention).
call = pytest.importorskip("libcob_py.call")
common = pytest.importorskip("libcob_py.common")
system = pytest.importorskip("libcob_py.system")


# ===========================================================================
# Isolation fixture - snapshot/restore ALL resolver + process global state
# ===========================================================================
@pytest.fixture(autouse=True)
def _isolate_call_state():
    """Fully isolate the dynamic loader's mutable state around every test.

    ``libcob_py.call`` keeps file-scope statics (mirroring ``call.c``'s
    ``call_table`` / ``resolve_path`` / ``name_convert`` / ``resolve_error``)
    and the loader mutates the *process*-global ``sys.path`` and ``sys.modules``
    when it seeds the search path and imports/evicts program modules.  Without
    careful teardown a resolved ``DUMMYPROG`` (or a ``"."`` pushed onto
    ``sys.path``) would leak into sibling test modules.

    This autouse fixture therefore:

    * snapshots ``sys.path`` and the set of loaded ``sys.modules`` keys, plus
      every ``call`` resolver static and the three ``common`` globals the call
      paths touch (``cob_initialized`` / ``cob_call_params`` /
      ``cob_exception_code``);
    * starts each test from an EMPTY resolver (cache, cancel handlers and
      resolve paths cleared; ``name_convert`` reset; last error cleared) so the
      tests are deterministic regardless of execution order;
    * on teardown evicts any module imported during the test, restores
      ``sys.path`` verbatim, restores all resolver statics and ``common``
      globals, and invalidates the import caches.
    """
    saved_path = list(sys.path)
    saved_modules = set(sys.modules)
    saved_cache = dict(call._call_cache)
    saved_cancel = dict(call._cancel_handlers)
    saved_resolve_paths = list(call._resolve_paths)
    saved_convert = call.name_convert
    saved_error = call._resolve_error
    saved_initialized = common.cob_initialized
    saved_params = common.cob_call_params
    saved_exc = common.cob_exception_code

    # Deterministic clean slate for the resolver.
    call._call_cache.clear()
    call._cancel_handlers.clear()
    call._resolve_paths = []
    call.name_convert = 0
    call._resolve_error = None

    yield

    # Evict anything the test imported (dummy program modules, and any module
    # the runtime lazily pulled in - e.g. screenio/fileio via _shutdown_runtime
    # on an abort path); a clean re-import is harmless next time.
    for name in set(sys.modules) - saved_modules:
        sys.modules.pop(name, None)
    sys.path[:] = saved_path

    call._call_cache.clear()
    call._call_cache.update(saved_cache)
    call._cancel_handlers.clear()
    call._cancel_handlers.update(saved_cancel)
    call._resolve_paths = saved_resolve_paths
    call.name_convert = saved_convert
    call._resolve_error = saved_error
    common.cob_initialized = saved_initialized
    common.cob_call_params = saved_params
    common.cob_exception_code = saved_exc

    importlib.invalidate_caches()


# ===========================================================================
# Helpers
# ===========================================================================
def _write_dummy_module(directory, modname="DUMMYPROG", *, state=0,
                        entries=None):
    """Write a generated-style program module into *directory*.

    The code generator emits one ``.py`` module per COBOL compilation unit whose
    entry symbol is the *encoded* program-id (see ``cob_encode_program_id``).
    To resolve robustly regardless of which candidate attribute the loader
    selects (encoded name, folded module name, or ``main``), the module exposes
    the entry under *both* the encoded name and ``main``.

    Each entry records its positional arguments in a module-level ``CALLS`` list
    and returns ``len(args)`` so the call wrappers can be exercised, while a
    module-level ``STATE`` marker (default ``0``) lets the CANCEL test prove a
    *fresh* module is re-imported (the in-memory mutation is discarded).

    Returns the :class:`pathlib.Path` of the written file.
    """
    if entries is None:
        entries = (modname, "main")
    lines = ["STATE = %d" % int(state), "CALLS = []"]
    for name in entries:
        lines.append(
            "def %s(*args):\n"
            "    CALLS.append(args)\n"
            "    return len(args)" % name
        )
    path = directory / (modname + ".py")
    path.write_text("\n".join(lines) + "\n")
    return path


def _name_field(text, size=None):
    """Build an ALPHANUMERIC ``common.cob_field`` carrying a program *name*.

    Used to drive the field-addressed entry points ``cob_call_resolve`` /
    ``cob_field_cancel``, which read the program name out of a field via
    ``common.cob_field_to_string`` (trailing spaces/NULs are trimmed, so the
    field may be right-padded to a fixed width).
    """
    data = text.encode("latin-1")
    if size is not None:
        data = data.ljust(size, b" ")
    attr = common.cob_field_attr(
        type=common.COB_TYPE_ALPHANUMERIC, digits=0, scale=0, flags=0, pic=None)
    return common.cob_field(size=len(data), data=bytearray(data), attr=attr)


# ===========================================================================
# Program-name encoding (cb_encode_program_id parity, typeck.c L621-L646)
# ===========================================================================
def test_encode_plain_alnum():
    """An all-alphanumeric name is its own Python identifier (unchanged)."""
    assert call.cob_encode_program_id("PROG1") == "PROG1"


def test_encode_leading_digit():
    """A leading digit becomes ``_HH`` (uppercase hex of the byte): '1'==0x31."""
    assert call.cob_encode_program_id("1ABC") == "_31ABC"


def test_encode_hyphen_becomes_double_underscore():
    """COBOL '-' maps to '__' (the documented hyphen transform)."""
    assert call.cob_encode_program_id("MY-PROG") == "MY__PROG"


def test_encode_special_char():
    """A non-alnum/underscore char becomes ``_HH``: '.' (0x2E) -> '_2E'."""
    assert call.cob_encode_program_id("A.B") == "A_2EB"


def test_encode_empty():
    """The empty name encodes to the empty string (guard branch)."""
    assert call.cob_encode_program_id("") == ""


# ===========================================================================
# COB_LOAD_CASE folding (_apply_case, call.c name_convert L394-L408)
# ===========================================================================
def test_apply_case_default_is_identity():
    """name_convert == 0 leaves the name untouched."""
    call.name_convert = 0
    assert call._apply_case("MixedCase") == "MixedCase"


def test_apply_case_lower():
    """name_convert == 1 folds to lower case."""
    call.name_convert = 1
    assert call._apply_case("MixedCase") == "mixedcase"


def test_apply_case_upper():
    """name_convert == 2 folds to upper case."""
    call.name_convert = 2
    assert call._apply_case("MixedCase") == "MIXEDCASE"


# ===========================================================================
# Search path + cache primitives (cob_set_library_path / lookup / insert /
# cob_set_cancel)
# ===========================================================================
def test_set_library_path_prepends_syspath(tmp_path):
    """The PATHSEP-delimited path is split, recorded, and prepended to sys.path."""
    d1 = str(tmp_path / "one")
    d2 = str(tmp_path / "two")
    os.makedirs(d1, exist_ok=True)
    os.makedirs(d2, exist_ok=True)
    call.cob_set_library_path(d1 + os.pathsep + d2)
    assert d1 in sys.path and d2 in sys.path
    # Order is preserved (call.c strtok order over resolve_path[]).
    assert call._resolve_paths == [d1, d2]


def test_set_library_path_ignores_empty_segments(tmp_path):
    """Empty path segments (leading/trailing/double separators) are dropped."""
    d1 = str(tmp_path / "only")
    os.makedirs(d1, exist_ok=True)
    call.cob_set_library_path(os.pathsep + d1 + os.pathsep + os.pathsep)
    assert call._resolve_paths == [d1]


def test_lookup_insert_roundtrip():
    """``insert`` caches an entry that ``lookup`` then returns by identity."""
    def entry():
        return 0
    assert call.lookup("X") is None
    call.insert("X", entry)
    assert call.lookup("X") is entry


def test_set_cancel_register_refresh_and_none_branch():
    """cob_set_cancel registers, refreshes only the cancel handler, and the
    ``cancel is None`` refresh path is a no-op (call.c L302-L320)."""
    def entry():
        return 0

    def cancel(*a):
        return 0

    # First registration: not yet cached -> insert entry + cancel.
    call.cob_set_cancel("Y", entry, cancel)
    assert call.lookup("Y") is entry
    assert call._cancel_handlers["Y"] is cancel

    # Already cached + cancel is None -> no change (covers the False branch).
    call.cob_set_cancel("Y", None, None)
    assert call.lookup("Y") is entry
    assert call._cancel_handlers["Y"] is cancel

    # Already cached + new cancel -> refresh the handler only, keep the entry.
    def cancel2(*a):
        return 0
    call.cob_set_cancel("Y", None, cancel2)
    assert call.lookup("Y") is entry
    assert call._cancel_handlers["Y"] is cancel2


# ===========================================================================
# Phase 1 - Dynamic resolution + cache (cob_resolve, call.c L321-L447)
# ===========================================================================
def test_resolve_from_library_path(lib_dir):
    """A module dropped on COB_LIBRARY_PATH resolves to a callable entry point.

    ``cob_init_call`` reads ``COB_LIBRARY_PATH`` (pointed at *lib_dir* by the
    fixture) and feeds it into ``sys.path`` so :func:`importlib.import_module`
    can find the generated module.
    """
    _write_dummy_module(lib_dir, "DUMMYPROG")
    call.cob_init_call()
    func = call.cob_resolve("DUMMYPROG")
    assert func is not None and callable(func)
    # The COB_LIBRARY_PATH directory was fed onto sys.path.
    assert str(lib_dir) in sys.path


def test_resolve_cached(lib_dir, monkeypatch):
    """A second resolve of the same name hits the cache (no re-import).

    ``importlib.import_module`` is spied (after init, so only the resolve calls
    are counted): the first resolve imports once and caches; the second returns
    the identical cached entry without importing again.
    """
    _write_dummy_module(lib_dir, "DUMMYPROG")
    call.cob_init_call()  # seed sys.path from COB_LIBRARY_PATH BEFORE spying

    real_import = importlib.import_module
    calls = []

    def spy(name, *args, **kwargs):
        calls.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", spy)

    first = call.cob_resolve("DUMMYPROG")
    second = call.cob_resolve("DUMMYPROG")
    assert first is not None
    assert first is second                       # identical cached entry
    assert calls.count("DUMMYPROG") == 1         # imported exactly once


def test_resolve_encoded_name(lib_dir):
    """'MY-PROG' resolves through the encoded module name 'MY__PROG'."""
    _write_dummy_module(lib_dir, "MY__PROG")
    call.cob_init_call()
    func = call.cob_resolve("MY-PROG")
    assert callable(func)


def test_resolve_falls_back_to_main(lib_dir):
    """When the encoded-name attribute is absent, the loader falls back to main."""
    _write_dummy_module(lib_dir, "PROG2", entries=("main",))
    call.cob_init_call()
    func = call.cob_resolve("PROG2")
    assert callable(func)


def test_resolve_unknown_error(clean_cob_env):
    """An unresolvable name returns None, sets EC-PROGRAM-NOT-FOUND and records
    a resolve error that is cleared on read (call.c not-found path)."""
    common.cob_exception_code = 0
    func = call.cob_resolve("NO_SUCH_PROGRAM_ZZZ")
    assert func is None
    assert common.cob_exception_code != 0          # EC-PROGRAM-NOT-FOUND latched
    assert call._resolve_error is not None
    msg = call.cob_resolve_error()
    assert "NO_SUCH_PROGRAM_ZZZ" in msg
    # cob_resolve_error returns AND clears the message.
    assert call.cob_resolve_error() is None


def test_resolve_no_entry_point(lib_dir):
    """A module that exposes no entry callable resolves to None."""
    (lib_dir / "PROG3.py").write_text("X = 1\n")
    call.cob_init_call()
    assert call.cob_resolve("PROG3") is None


def test_resolve_1_aborts_when_missing(clean_cob_env, capsys):
    """cob_resolve_1 stops the run unit (SystemExit) on an unresolvable name and
    reports the resolve error to stderr (call.c L448-L459)."""
    with pytest.raises(SystemExit):
        call.cob_resolve_1("NO_SUCH_PROGRAM_YYY")
    err = capsys.readouterr().err
    assert "NO_SUCH_PROGRAM_YYY" in err


# ===========================================================================
# Field-addressed resolution (cob_call_resolve / cob_call_resolve_1)
# ===========================================================================
def test_call_resolve_via_field(lib_dir):
    """cob_call_resolve reads the program name out of a field and resolves it."""
    _write_dummy_module(lib_dir, "FPROG")
    call.cob_init_call()
    func = call.cob_call_resolve(_name_field("FPROG", 16))
    assert callable(func)


def test_call_resolve_1_aborts(clean_cob_env):
    """cob_call_resolve_1 aborts (SystemExit) when the field names a ghost."""
    with pytest.raises(SystemExit):
        call.cob_call_resolve_1(_name_field("GHOSTPROG", 16))


def test_call_resolve_1_success(lib_dir):
    """cob_call_resolve_1 returns the entry callable for a resolvable field
    (the non-aborting success path, call.c L470-L480)."""
    _write_dummy_module(lib_dir, "FPROG1")
    call.cob_init_call()
    func = call.cob_call_resolve_1(_name_field("FPROG1", 16))
    assert callable(func)


# ===========================================================================
# Phase 2 - CANCEL: module-cache eviction + invalidate_caches + fresh reload
# (cobcancel, call.c L481-L519)
# ===========================================================================
def test_cancel_reloads_module(lib_dir, monkeypatch):
    """CANCEL evicts the module, calls invalidate_caches and re-imports fresh.

    The in-memory module's ``STATE`` is mutated after the first resolve; after
    CANCEL the name is gone from both ``sys.modules`` and the call cache, and a
    subsequent resolve produces a *different* module object whose ``STATE`` is
    back to its source value (0) - proving a genuinely fresh import rather than
    a reuse of the mutated module.  ``importlib.invalidate_caches`` is spied to
    confirm the cancel path invalidated the import system.
    """
    _write_dummy_module(lib_dir, "DUMMYPROG", state=0)
    call.cob_init_call()

    func = call.cob_resolve("DUMMYPROG")
    assert func is not None
    assert "DUMMYPROG" in sys.modules
    assert call.lookup("DUMMYPROG") is func

    first_mod = sys.modules["DUMMYPROG"]
    first_mod.STATE = 999                         # mutate the live module

    # Spy importlib.invalidate_caches (call.py looks it up on the importlib
    # module at call time, so patching the attribute is observed).
    real_invalidate = importlib.invalidate_caches
    counter = {"n": 0}

    def spy_invalidate():
        counter["n"] += 1
        return real_invalidate()

    monkeypatch.setattr(importlib, "invalidate_caches", spy_invalidate)

    call.cobcancel("DUMMYPROG")

    # Evicted from the import system and the call cache.
    assert "DUMMYPROG" not in sys.modules
    assert call.lookup("DUMMYPROG") is None
    assert counter["n"] >= 1                      # invalidate_caches was called

    # A subsequent resolve re-imports a FRESH module (state reset to source 0).
    func2 = call.cob_resolve("DUMMYPROG")
    assert func2 is not None
    fresh_mod = sys.modules["DUMMYPROG"]
    assert fresh_mod is not first_mod
    assert fresh_mod.STATE == 0                   # mutation discarded


def test_cobcancel_runs_handler_and_evicts(lib_dir):
    """A registered cancel handler runs, then the program is uncached/evicted."""
    _write_dummy_module(lib_dir, "CPROG")
    call.cob_init_call()
    func = call.cob_resolve("CPROG")
    assert func is not None

    flags = {"cancelled": False}

    def cancel(*a):
        flags["cancelled"] = True
        return 0

    call._cancel_handlers["CPROG"] = cancel
    call.cobcancel("CPROG")
    assert flags["cancelled"] is True
    assert call.lookup("CPROG") is None
    assert "CPROG" not in sys.modules
    # REVIEW FIX (MAJOR #7): the cancel handler is evicted too (no stale entry).
    assert "CPROG" not in call._cancel_handlers

    # A second CANCEL of the same name must NOT re-run the (now removed) handler.
    flags["cancelled"] = False
    call.cobcancel("CPROG")
    assert flags["cancelled"] is False


def test_cobcancel_none_name_aborts():
    """CANCEL of a NULL name is a fatal runtime error (SystemExit)."""
    with pytest.raises(SystemExit):
        call.cobcancel(None)


def test_field_cancel(lib_dir):
    """cob_field_cancel reads the name from a field and cancels that program."""
    _write_dummy_module(lib_dir, "DPROG")
    call.cob_init_call()
    call.cob_resolve("DPROG")
    assert call.lookup("DPROG") is not None
    call.cob_field_cancel(_name_field("DPROG", 16))
    assert call.lookup("DPROG") is None
    assert "DPROG" not in sys.modules


# ===========================================================================
# Phase 2b - STOP RUN / tidy teardown of the (unbounded-by-design) caches
# (cob_exit_call, REVIEW FIX MAJOR #7)
# ===========================================================================
def test_cob_exit_call_clears_both_caches():
    """cob_exit_call releases the call cache AND the cancel-handler table."""
    call._call_cache["A"] = lambda: 0
    call._call_cache["B"] = lambda: 0
    call._cancel_handlers["A"] = lambda *a: 0
    call.cob_exit_call()
    assert call._call_cache == {}
    assert call._cancel_handlers == {}
    # Idempotent: a second call is a harmless no-op.
    call.cob_exit_call()
    assert call._call_cache == {}


def test_cob_exit_call_is_invoked_by_runtime_shutdown():
    """common._shutdown_runtime (the STOP RUN / cobtidy teardown) evicts the
    loader caches via cob_exit_call - so the caches never outlive the run."""
    call._call_cache["RESIDENT"] = lambda: 0
    call._cancel_handlers["RESIDENT"] = lambda *a: 0
    common._shutdown_runtime()
    assert "RESIDENT" not in call._call_cache
    assert "RESIDENT" not in call._cancel_handlers


def test_cobtidy_clears_loader_caches():
    """cobtidy() (STOP RUN without process exit) tears the caches down too."""
    call._call_cache["TIDYP"] = lambda: 0
    call._cancel_handlers["TIDYP"] = lambda *a: 0
    assert common.cobtidy() == 0
    assert call._call_cache == {}
    assert call._cancel_handlers == {}


def test_call_cache_is_unbounded_by_design(lib_dir):
    """Residency contract: repeated distinct inserts all persist (no eviction).

    A bounded/LRU cache would silently drop a resident program's WORKING-STORAGE
    state; the runtime must keep every entry until CANCEL or STOP RUN.
    """
    for i in range(64):
        call.insert("PROG%02d" % i, (lambda i=i: i))
    assert len(call._call_cache) >= 64
    # Every entry is still individually resolvable (none evicted).
    for i in range(64):
        assert call.lookup("PROG%02d" % i) is not None


# ===========================================================================
# Phase 3 - Environment variables (COB_LIBRARY_PATH / COB_PRE_LOAD /
# COB_LOAD_CASE), wired by cob_init_call (call.c L520-L600)
# ===========================================================================
def test_cob_library_path_to_syspath(tmp_path, clean_cob_env, set_cob_env):
    """COB_LIBRARY_PATH (one or more dirs) is fed onto sys.path by cob_init_call.

    Uses ``clean_cob_env`` for a deterministic baseline and ``set_cob_env`` to
    set a two-directory ``COB_LIBRARY_PATH`` (OS path separator); after init both
    directories must be present on ``sys.path`` and recorded as resolve paths.
    """
    d1 = tmp_path / "libone"
    d2 = tmp_path / "libtwo"
    d1.mkdir()
    d2.mkdir()
    set_cob_env("COB_LIBRARY_PATH", str(d1) + os.pathsep + str(d2))

    call.cob_init_call()

    assert str(d1) in sys.path
    assert str(d2) in sys.path
    assert str(d1) in call._resolve_paths
    assert str(d2) in call._resolve_paths


def test_cob_pre_load(lib_dir, set_cob_env):
    """COB_PRE_LOAD modules are imported at startup (present in sys.modules)."""
    _write_dummy_module(lib_dir, "PRELOADED")
    set_cob_env("COB_PRE_LOAD", "PRELOADED")
    call.cob_init_call()
    assert "PRELOADED" in sys.modules


def test_cob_pre_load_missing_module_skipped(lib_dir, set_cob_env):
    """A COB_PRE_LOAD entry that cannot be imported is silently skipped - no
    exception escapes (mirrors the C preload loop, call.c L571-L593)."""
    set_cob_env("COB_PRE_LOAD", "NO_SUCH_PRELOAD_MODULE")
    call.cob_init_call()                         # must not raise
    assert "NO_SUCH_PRELOAD_MODULE" not in sys.modules


def test_cob_pre_load_empty_entries_skipped(lib_dir, set_cob_env):
    """Empty COB_PRE_LOAD segments (leading/trailing separators) are skipped."""
    _write_dummy_module(lib_dir, "PRELOADED")
    set_cob_env("COB_PRE_LOAD", os.pathsep + "PRELOADED" + os.pathsep)
    call.cob_init_call()
    assert "PRELOADED" in sys.modules


def test_cob_load_case_upper_resolves_lower_name(lib_dir, set_cob_env):
    """COB_LOAD_CASE=UPPER folds a lower-case program name during resolution.

    The module file is ``DUMMYPROG.py``; with upper-case folding enabled,
    resolving the lower-case name ``"dummyprog"`` folds the encoded name to
    ``DUMMYPROG`` and finds the module.
    """
    _write_dummy_module(lib_dir, "DUMMYPROG")
    set_cob_env("COB_LOAD_CASE", "UPPER")
    call.cob_init_call()
    assert call.name_convert == 2
    func = call.cob_resolve("dummyprog")
    assert func is not None and callable(func)


def test_cob_load_case_lower(clean_cob_env, set_cob_env):
    """COB_LOAD_CASE=LOWER sets name_convert to 1 during init."""
    set_cob_env("COB_LOAD_CASE", "LOWER")
    call.cob_init_call()
    assert call.name_convert == 1


def test_cob_load_case_upper(clean_cob_env, set_cob_env):
    """COB_LOAD_CASE=UPPER sets name_convert to 2 during init."""
    set_cob_env("COB_LOAD_CASE", "UPPER")
    call.cob_init_call()
    assert call.name_convert == 2


def test_cob_load_case_invalid_value_ignored(clean_cob_env, set_cob_env):
    """A COB_LOAD_CASE value that is neither LOWER nor UPPER leaves folding off
    (name_convert stays 0), mirroring the C strcasecmp guards (call.c L547-L554)."""
    set_cob_env("COB_LOAD_CASE", "SOMETHING_ELSE")
    call.cob_init_call()
    assert call.name_convert == 0


# ===========================================================================
# Phase 4 - Builtin (system routine) registration (call.c L597-L599)
# ===========================================================================
def test_resolve_builtin_cbl_toupper(clean_cob_env):
    """After init, CBL_TOUPPER resolves to the system routine and upper-cases.

    The 43 ``CBL_``/``C$`` builtins from :mod:`libcob_py.system` are registered
    into the resolver by ``cob_init_call``; resolving ``"CBL_TOUPPER"`` must
    return that callable, and driving it must upper-case the supplied buffer
    in place (only ASCII lower-case letters are folded).
    """
    call.cob_init_call()
    fn = call.cob_resolve("CBL_TOUPPER")
    assert fn is not None and callable(fn)
    assert fn is system.CBL_TOUPPER

    buf = bytearray(b"abcDEF")
    rc = fn(buf, len(buf))
    assert rc == 0
    assert bytes(buf) == b"ABCDEF"


def test_resolve_builtin_alias_c_toupper(clean_cob_env):
    """The ACUCOBOL ``C$TOUPPER`` alias resolves to the same callable object."""
    call.cob_init_call()
    assert call.cob_resolve("C$TOUPPER") is call.cob_resolve("CBL_TOUPPER")


def test_init_registers_all_system_routines(clean_cob_env):
    """Every row of system.SYSTEM_TABLE is resolvable to its system callable.

    Cross-checks the full 43-entry dispatch contract: after ``cob_init_call``,
    ``cob_resolve(external_name)`` must return the exact callable named by the
    table's internal-name mapping for every registered builtin.
    """
    call.cob_init_call()
    checked = 0
    for ext_name, internal in system.SYSTEM_TABLE.items():
        fn = getattr(system, internal, None)
        if fn is None:
            continue
        assert call.cob_resolve(ext_name) is fn
        checked += 1
    # Sanity: the table is non-trivial (43 rows per system.def).
    assert checked == len(system.SYSTEM_TABLE)


# ===========================================================================
# Phase 5 - init & helpers (cob_init_call idempotency, env fallbacks)
# ===========================================================================
def test_init_call_callable_and_idempotent(clean_cob_env):
    """cob_init_call is callable and idempotent (a second call must not raise)."""
    call.cob_init_call()
    call.cob_init_call()
    # Builtins remain registered after a repeat init.
    assert call.lookup("CBL_TOUPPER") is system.CBL_TOUPPER


def test_init_call_library_path_unset_uses_default(clean_cob_env):
    """With COB_LIBRARY_PATH unset, the search path defaults to '.' plus the
    built-in default (call.c L558-L560)."""
    call.cob_init_call()
    assert "." in call._resolve_paths


# ===========================================================================
# CALL argument-passing wrappers (BY CONTENT / BY VALUE).
# These materialise the call-argument idioms the emitter lowers from the
# original C union/cast forms (codegen.c output_call).
# ===========================================================================
class TestContentInt:
    """cob_content_int - BY CONTENT / BY REFERENCE integer temporary."""

    def test_fits_int_is_4_byte_buffer(self):
        b = call.cob_content_int(5, 1)
        assert isinstance(b, bytearray) and len(b) == 4
        assert int.from_bytes(b, sys.byteorder) == 5

    def test_not_fits_int_is_8_byte_buffer(self):
        b = call.cob_content_int(5, 0)
        assert len(b) == 8 and int.from_bytes(b, sys.byteorder) == 5

    def test_negative_twos_complement(self):
        b = call.cob_content_int(-1, 1)
        assert int.from_bytes(b, sys.byteorder) == 0xFFFFFFFF

    def test_buffer_is_writable_by_reference(self):
        b = call.cob_content_int(0, 1)
        b[0] = 0x7F          # callee may write through (BY REFERENCE)
        assert b[0] == 0x7F


class TestContentBuffer:
    """cob_content_buffer - BY CONTENT independent copy."""

    def test_independent_copy(self):
        src = bytearray(b"HELLO")
        c = call.cob_content_buffer(memoryview(src)[0:], 5)
        assert c == b"HELLO" and isinstance(c, bytearray)
        c[0] = ord("J")
        assert src[0] == ord("H")        # BY CONTENT: caller storage untouched

    def test_short_data_nul_padded(self):
        c = call.cob_content_buffer(b"AB", 5)
        assert c == b"AB\x00\x00\x00"

    def test_from_bytes_source(self):
        assert call.cob_content_buffer(b"XYZ", 3) == b"XYZ"


class TestValueInt:
    """cob_value_int - BY VALUE numeric reduced to width."""

    def test_unsigned_short_wrap(self):
        assert call.cob_value_int(70000, 2, 1) == 70000 & 0xFFFF

    def test_unsigned_negative_wraps(self):
        assert call.cob_value_int(-1, 2, 1) == 0xFFFF

    def test_signed_short_negative(self):
        assert call.cob_value_int(-1, 2, 0) == -1

    def test_signed_byte_sign_extends(self):
        assert call.cob_value_int(0x1FF, 1, 0) == -1     # 0xFF as signed char

    def test_four_byte_passthrough(self):
        assert call.cob_value_int(1234567, 4, 1) == 1234567


class TestValueBuffer:
    """cob_value_buffer - BY VALUE numeric wrapped into a width-N buffer."""

    def test_two_byte_native_endian(self):
        vb = call.cob_value_buffer(258, 2, 0)
        assert isinstance(vb, bytearray) and len(vb) == 2
        assert int.from_bytes(vb, sys.byteorder) == 258

    def test_width_respected(self):
        assert len(call.cob_value_buffer(1, 8, 1)) == 8

    def test_negative_twos_complement(self):
        vb = call.cob_value_buffer(-1, 2, 0)
        assert int.from_bytes(vb, sys.byteorder) == 0xFFFF


# ===========================================================================
# cob_strdup (call.c L165-L175) - independent-copy semantics
# ===========================================================================
class TestStrdup:
    def test_str_returned_unchanged(self):
        # str is immutable; the duplicate is value-equal and still a str.
        result = call.cob_strdup("ABC")
        assert result == "ABC" and isinstance(result, str)

    def test_bytes_yields_mutable_bytearray(self):
        result = call.cob_strdup(b"xyz")
        assert isinstance(result, bytearray) and result == bytearray(b"xyz")
        result[0] = ord("Q")            # the copy is writable (strtok use case)
        assert result == bytearray(b"Qyz")

    def test_bytearray_copy_is_independent(self):
        src = bytearray(b"123")
        result = call.cob_strdup(src)
        result[0] = 0
        assert src == bytearray(b"123")     # source untouched

    def test_memoryview_source(self):
        src = bytearray(b"MVW")
        result = call.cob_strdup(memoryview(src))
        assert isinstance(result, bytearray) and result == bytearray(b"MVW")

    def test_none_passthrough(self):
        assert call.cob_strdup(None) is None

    def test_other_object_normalised_to_str(self):
        assert call.cob_strdup(12345) == "12345"


# ===========================================================================
# cob_get_buff (call.c L204-L213) - reusable, growable scratch buffer
# ===========================================================================
class TestGetBuff:
    def test_returns_zeroed_buffer_of_width(self):
        buf = call.cob_get_buff(8)
        assert len(buf) == 8 and all(x == 0 for x in buf)

    def test_writable(self):
        buf = call.cob_get_buff(4)
        buf[0] = 0x41
        assert buf[0] == 0x41

    def test_smaller_request_rezeroes_window(self):
        big = call.cob_get_buff(16)
        big[0] = 0xFF
        small = call.cob_get_buff(4)         # reuse + re-zero active window
        assert len(small) == 4 and all(x == 0 for x in small)

    def test_grow_enlarges_backing_store(self):
        call.cob_get_buff(4)
        grown = call.cob_get_buff(128)
        assert len(grown) == 128

    def test_negative_treated_as_zero(self):
        assert len(call.cob_get_buff(-5)) == 0


# ===========================================================================
# cobcall / cobfunc (call.c L602 / L638) - call-by-name with argv
# ===========================================================================
@pytest.fixture
def runtime_initialized():
    """Force common.cob_initialized truthy for cobcall/cobfunc, then restore.

    cobcall publishes ``common.cob_call_params`` (the CALL argument count); the
    autouse ``_isolate_call_state`` fixture also snapshots/restores that global,
    but this fixture makes the per-test intent explicit and self-contained.
    """
    saved_init = common.cob_initialized
    saved_params = common.cob_call_params
    common.cob_initialized = 1
    yield
    common.cob_initialized = saved_init
    common.cob_call_params = saved_params


def test_cobcall_resolves_and_invokes(lib_dir, runtime_initialized):
    _write_dummy_module(lib_dir, "RECPROG")
    call.cob_init_call()
    rc = call.cobcall("RECPROG", 2, ["A", "B"])
    assert rc == 2                                  # entry returned len(args)
    assert common.cob_call_params == 2              # parameter count published
    assert sys.modules["RECPROG"].CALLS[-1] == ("A", "B")


def test_cobcall_pads_short_argv_with_none(lib_dir, runtime_initialized):
    _write_dummy_module(lib_dir, "PADPROG")
    call.cob_init_call()
    rc = call.cobcall("PADPROG", 3, ["X"])
    assert rc == 3
    assert sys.modules["PADPROG"].CALLS[-1] == ("X", None, None)


def test_cobcall_none_argv(lib_dir, runtime_initialized):
    _write_dummy_module(lib_dir, "NILPROG")
    call.cob_init_call()
    rc = call.cobcall("NILPROG", 0, None)
    assert rc == 0
    assert sys.modules["NILPROG"].CALLS[-1] == ()


def test_cobcall_not_initialized_stops(monkeypatch):
    monkeypatch.setattr(common, "cob_initialized", 0)
    with pytest.raises(SystemExit):
        call.cobcall("ANY", 0, [])


def test_cobcall_bad_argc_stops(runtime_initialized):
    with pytest.raises(SystemExit):
        call.cobcall("ANY", 99, [])
    with pytest.raises(SystemExit):
        call.cobcall("ANY", -1, [])


def test_cobcall_none_name_stops(runtime_initialized):
    with pytest.raises(SystemExit):
        call.cobcall(None, 0, [])


def test_cobfunc_calls_then_cancels(lib_dir, runtime_initialized):
    _write_dummy_module(lib_dir, "FUNCPROG")
    call.cob_init_call()
    rc = call.cobfunc("FUNCPROG", 1, ["Z"])
    assert rc == 1
    # cobfunc cancels after calling: the module is evicted and the cache cleared.
    assert "FUNCPROG" not in sys.modules
    assert call.lookup("FUNCPROG") is None


def test_cobfunc_not_initialized_stops(monkeypatch):
    monkeypatch.setattr(common, "cob_initialized", 0)
    with pytest.raises(SystemExit):
        call.cobfunc("ANY", 0, [])


def test_max_cobcall_parms_constant():
    """The fixed upper bound on CALL arguments mirrors COB_MAX_COBCALL_PARMS."""
    assert call.COB_MAX_COBCALL_PARMS == 16

