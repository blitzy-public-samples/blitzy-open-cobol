"""Shared pytest fixtures and scaffolding for the ``libcob_py`` test suite.

This ``conftest.py`` is the *linchpin* of the ``tests/libcob_py/`` suite created
for the GNU Cobol (OpenCOBOL) C->Python backend refactor (AAP sections 0.2.1,
0.3.1, 0.4.1, 0.7.1).  Its responsibilities are:

1. Put the repository **root** on ``sys.path`` so that ``import libcob_py``
   resolves to the pure-Python runtime package ``<repo>/libcob_py/`` (Phase 1).
2. Provide an availability helper and skip marker for the (parallel-built)
   ``libcob_py`` runtime so collection never hard-errors when it is absent
   (Phase 2).
3. Offer reusable fixtures: an isolated working directory and COBOL
   environment-variable patching for the file-I/O and dynamic-loader tests
   (Phase 3).
4. Locate the built ``cobc`` compiler binaries for the byte-for-byte numeric
   *parity* harness, degrading to a clean SKIP when they are unavailable
   (Phase 4).
5. Provide a ``curses`` availability skip marker for the SCREEN SECTION tests
   (Phase 5).
6. Register the suite's custom pytest markers (Phase 6).
7. Offer small, standard-library-only assertion helpers (Phase 7).

HARD CONSTRAINTS (AAP sections 0.5 / 0.7.1)
-------------------------------------------
* **Standard library only.**  The sole non-stdlib import is ``pytest`` itself,
  which is a development-only test framework (never a runtime dependency) and is
  therefore permitted in test scaffolding.  ``coverage.py`` is likewise a
  dev-only tool and is *not* imported here.  NO third-party runtime package is
  imported anywhere in this file.
* **No eager ``import libcob_py``.**  The runtime package is built in parallel by
  other agents and may not exist when this suite is authored or collected.  It
  is therefore only accessed lazily/guarded (via ``importlib.util.find_spec`` or
  ``pytest.importorskip``), never at module top level.

CONVENTIONS FOR TEST MODULES IN THIS SUITE
------------------------------------------
* Each ``test_<module>.py`` should obtain the runtime under test with
  ``pytest.importorskip`` at the top of the module, e.g.::

      import pytest
      libcob_py = pytest.importorskip("libcob_py")
      numeric = pytest.importorskip("libcob_py.numeric")

  This yields a clean *skip* (not a collection error) when the parallel-built
  runtime - or a specific runtime sub-module - is not yet importable.
* The suite is intended to run from the ``tests/`` directory with the repository
  root on ``sys.path`` (placed there by this ``conftest.py``), matching the
  ``tests/Makefile.am`` ``check-local`` invocation::

      $(COB_PYTHON) -m coverage run --branch --source=<repo>/libcob_py \\
          -m pytest -q <repo>/tests/libcob_py
      coverage report --fail-under=80

* The skip markers defined below (``requires_libcob_py``, ``requires_cobc``,
  ``requires_dual_cobc``, ``requires_curses``) are exposed as module-level names
  *and* registered as named pytest markers.  Because ``tests/`` is a package
  (see ``tests/__init__.py``), test modules in this suite may import them with
  ``from tests.libcob_py.conftest import requires_curses`` when convenient; the
  rootdir-safe ``pytest.importorskip`` pattern above is preferred for the
  runtime dependency itself.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

# ===========================================================================
# Phase 1 - Put the repository ROOT on sys.path (so ``import libcob_py`` works)
# ===========================================================================
#
# Path arithmetic proof (``Path.resolve().parents`` indexing):
#
#     this file -> <repo>/tests/libcob_py/conftest.py
#     parents[0] -> <repo>/tests/libcob_py     (the suite package directory)
#     parents[1] -> <repo>/tests               (the tests package directory)
#     parents[2] -> <repo>                      (the repository root; the
#                                                directory that contains the
#                                                ``libcob_py/`` runtime package)
#
# ``.resolve()`` makes the path absolute and symlink-free so the result is
# independent of the caller's current working directory.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Insert the repository root at the FRONT of ``sys.path`` if it is not already
# present.  When ``tests/`` is a package (it is - see ``tests/__init__.py``),
# pytest already discovers the repository root as its rootdir and places it on
# ``sys.path``; this insertion is then a harmless no-op.  It still guarantees
# correctness for alternative invocation styles (e.g. running a single test
# file directly, or from a different working directory) where pytest might not
# have added the repository root.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def repo_root() -> pathlib.Path:
    """Return the absolute path to the repository root.

    Convenience accessor mirroring the module-level :data:`REPO_ROOT` constant
    for tests that prefer dependency injection over importing the constant.
    """
    return REPO_ROOT


# ===========================================================================
# Phase 2 - ``libcob_py`` availability helper + skip marker
# ===========================================================================
def _libcob_py_available() -> bool:
    """Return ``True`` iff the ``libcob_py`` runtime package is importable.

    Uses :func:`importlib.util.find_spec` so that the package is *not* actually
    imported (avoiding side effects and hard collection errors).  The Phase 1
    ``sys.path`` insertion above must already have run, which it has - this
    function is defined after it and is only ever called at import time (to
    build the marker below) and on demand.
    """
    try:
        return importlib.util.find_spec("libcob_py") is not None
    except (ImportError, ValueError):
        # ImportError: a parent package failed to import.
        # ValueError: ``__spec__`` is None for an already-imported edge case.
        return False


# Reusable skip marker for tests that need the runtime but want to apply the
# skip declaratively (``@requires_libcob_py``).  Test modules are nonetheless
# encouraged to use ``pytest.importorskip("libcob_py.<module>")`` at module top
# level (the idiomatic, rootdir-safe pattern documented above).
requires_libcob_py = pytest.mark.skipif(
    not _libcob_py_available(),
    reason="libcob_py runtime package not importable "
    "(built in parallel; ensure the repository root is on sys.path)",
)


# ===========================================================================
# Phase 3 - Temp working directory & environment-variable fixtures
# ===========================================================================

# The COBOL runtime environment variables honoured by ``libcob_py`` (AAP
# sections 0.1.1 / 0.7.2).  These are normalised away by ``clean_cob_env`` so
# tests start from a deterministic state.
_COB_RUNTIME_ENV_VARS = (
    "COB_LIBRARY_PATH",   # search path fed into sys.path by call.py
    "COB_PRE_LOAD",       # modules preloaded at startup by call.py
    "COB_LOAD_CASE",      # program-name case folding for the loader
    "COB_CONFIG_DIR",     # dialect-configuration directory
)


@pytest.fixture
def work_dir(tmp_path, monkeypatch):
    """Change into a fresh temporary directory for the duration of a test.

    File-I/O and SORT/MERGE tests create files on disk; running them inside a
    throwaway ``tmp_path`` keeps the source tree clean.  ``monkeypatch.chdir``
    records the previous working directory and restores it automatically at
    fixture teardown, so the change is fully isolated.

    Yields the :class:`pathlib.Path` of the (now current) working directory.
    """
    monkeypatch.chdir(tmp_path)
    yield tmp_path


@pytest.fixture
def clean_cob_env(monkeypatch):
    """Remove the COBOL runtime environment variables for a known baseline.

    Every variable in :data:`_COB_RUNTIME_ENV_VARS` is deleted with
    ``raising=False`` so the fixture succeeds whether or not the variable was
    set.  ``monkeypatch`` restores the original environment at teardown.

    Returns the tuple of variable names that were normalised, for convenience.
    """
    for name in _COB_RUNTIME_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return _COB_RUNTIME_ENV_VARS


@pytest.fixture
def set_cob_env(monkeypatch):
    """Return a factory that sets a COBOL runtime environment variable.

    Usage in a test::

        def test_library_path(set_cob_env, lib_dir):
            set_cob_env("COB_PRE_LOAD", "mymod")
            ...

    The factory delegates to ``monkeypatch.setenv`` so every assignment is
    automatically reverted at the end of the test.  It returns the value that
    was set, enabling ``value = set_cob_env(name, value)`` idioms.
    """
    def _set(name: str, value: str) -> str:
        monkeypatch.setenv(name, value)
        return value

    return _set


@pytest.fixture
def lib_dir(tmp_path, monkeypatch):
    """Create a temporary library directory and point ``COB_LIBRARY_PATH`` at it.

    Convenience fixture for the dynamic-loader tests in ``test_call.py``: a test
    can drop a dummy module into the returned directory and then resolve it via
    the ``libcob_py.call`` loader (which feeds ``COB_LIBRARY_PATH`` into
    ``sys.path``).  A dedicated sub-directory is used so the fixture composes
    cleanly with :func:`work_dir` (which ``chdir`` s into the same ``tmp_path``).

    Returns the :class:`pathlib.Path` of the created library directory.
    """
    directory = tmp_path / "cob_library"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COB_LIBRARY_PATH", str(directory))
    return directory


# ===========================================================================
# Phase 4 - Locate the built ``cobc`` binaries for the parity harness
# ===========================================================================
def _usable_executable(candidate) -> bool:
    """Return ``True`` iff *candidate* names an existing, executable file."""
    if not candidate:
        return False
    path = pathlib.Path(candidate)
    return path.is_file() and os.access(path, os.X_OK)


def cobc_path() -> pathlib.Path | None:
    """Locate a usable ``cobc`` compiler binary, or ``None`` if unavailable.

    Resolution order:

    1. The ``COBC`` environment variable, if it names an executable file.
    2. ``<repo>/cobc/cobc`` - the in-tree build location.
    3. A ``cobc`` found on ``PATH`` (e.g. an installed compiler) as a final
       fallback.

    Returns ``None`` when no usable binary is found.  In a source-only checkout
    ``cobc/cobc`` is frequently *not* built, so ``None`` is an expected result
    that must lead to a clean SKIP of the parity tests - never a failure.
    """
    env_cobc = os.environ.get("COBC")
    if _usable_executable(env_cobc):
        return pathlib.Path(env_cobc)

    in_tree = REPO_ROOT / "cobc" / "cobc"
    if _usable_executable(in_tree):
        return in_tree

    on_path = shutil.which("cobc")
    if _usable_executable(on_path):
        return pathlib.Path(on_path)

    return None


def cobc_orig_path() -> pathlib.Path | None:
    """Locate the ORIGINAL C-backend ``cobc`` for the dual-compile parity test.

    Read from the ``COBC_ORIG`` environment variable.  Returns ``None`` when the
    variable is unset or does not name an executable, so the dual-compile parity
    comparison skips cleanly rather than failing.
    """
    env_orig = os.environ.get("COBC_ORIG")
    if _usable_executable(env_orig):
        return pathlib.Path(env_orig)
    return None


def cobc_py_path() -> pathlib.Path | None:
    """Locate the REFACTORED Python-backend ``cobc`` for the parity test.

    Read from the ``COBC_PY`` environment variable.  Returns ``None`` when the
    variable is unset or does not name an executable, so the dual-compile parity
    comparison skips cleanly rather than failing.
    """
    env_py = os.environ.get("COBC_PY")
    if _usable_executable(env_py):
        return pathlib.Path(env_py)
    return None


def run_cobc(cobc_binary, args, *, cwd=None, timeout=120):
    """Invoke a ``cobc`` compiler binary as a subprocess and capture its output.

    This is the shared compilation primitive for the numeric parity harness: it
    runs ``[cobc_binary, *args]`` (e.g. ``["-x", "-o", "out", "prog.cob"]``) and
    returns the :class:`subprocess.CompletedProcess`.

    The child's ``stdout``/``stderr`` are captured as **raw bytes** (``text`` is
    intentionally left unset) so that byte-for-byte comparisons - the core
    requirement of the parity gate (AAP section 0.6.2) - are exact and unaffected
    by locale-dependent text decoding.  ``check`` is ``False`` so the caller can
    assert on the return code and diagnostics; ``timeout`` guards against a
    hung compiler.

    Callers must ensure *cobc_binary* is non-``None`` (gate the test with the
    ``requires_cobc`` / ``requires_dual_cobc`` markers); a ``None`` binary raises
    :class:`ValueError` rather than producing a confusing ``subprocess`` error.
    """
    if cobc_binary is None:
        raise ValueError(
            "run_cobc() requires a usable cobc binary; gate the test with "
            "@requires_cobc (or @requires_dual_cobc) so it skips when absent"
        )
    argv = [str(cobc_binary), *(str(arg) for arg in args)]
    return subprocess.run(
        argv,
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        timeout=timeout,
        check=False,
    )


# The two read-only COBOL templates that the numeric parity harness compiles
# with both the original (C) and refactored (Python) ``cobc``.
_PARITY_TEMPLATES = ("numeric-display.cob", "numeric-dump.cob")


@pytest.fixture
def template_dir():
    """Return the read-only directory holding the numeric parity templates.

    This is ``<repo>/tests/data-rep.src`` - the home of ``numeric-display.cob``
    and ``numeric-dump.cob`` (an immutable part of the existing test tree, AAP
    section 0.2.3).  If either template is missing the fixture issues a clean
    ``pytest.skip`` so parity tests depending on it are skipped rather than
    failing.

    Returns the :class:`pathlib.Path` of the template directory.
    """
    directory = REPO_ROOT / "tests" / "data-rep.src"
    missing = [name for name in _PARITY_TEMPLATES if not (directory / name).is_file()]
    if missing:
        pytest.skip(
            "numeric parity templates missing from %s: %s"
            % (directory, ", ".join(missing))
        )
    return directory


# Declarative skip markers for the parity harness, exposed as module-level
# names AND registered as pytest markers in ``pytest_configure`` below.
requires_cobc = pytest.mark.skipif(
    cobc_path() is None,
    reason="built cobc not available (source-only tree); parity test skipped",
)

requires_dual_cobc = pytest.mark.skipif(
    cobc_orig_path() is None or cobc_py_path() is None,
    reason="original C cobc and/or refactored Python cobc not both available "
    "(set COBC_ORIG and COBC_PY); parity comparison skipped",
)


# ===========================================================================
# Phase 5 - ``curses`` availability skip marker (SCREEN SECTION tests)
# ===========================================================================
def _curses_available() -> bool:
    """Return ``True`` iff the stdlib ``curses`` module is importable.

    ``curses`` ships with CPython but may be unavailable on some platforms
    (notably stock Windows builds).  Note that importing ``libcob_py.screenio``
    itself must NOT require a TTY - the runtime module is designed to be
    importable without a terminal.  This marker therefore guards only the tests
    that actually *drive* curses behaviour.
    """
    return importlib.util.find_spec("curses") is not None


requires_curses = pytest.mark.skipif(
    not _curses_available(),
    reason="curses not available on this platform",
)


# ===========================================================================
# Phase 6 - Register the suite's custom pytest markers
# ===========================================================================
def pytest_configure(config):
    """Register the suite's custom markers to keep strict-marker runs clean.

    Registering the markers via ``addinivalue_line`` prevents
    ``PytestUnknownMarkWarning`` (and hard failures under ``--strict-markers`` /
    ``-W error``) when test modules apply them with ``@pytest.mark.<name>`` or
    via the declarative skip markers exposed by this module.
    """
    markers = (
        "parity: byte-for-byte numeric parity tests (may require a built cobc)",
        "curses: tests that drive curses-backed SCREEN SECTION behaviour",
        "slow: tests that are comparatively slow to execute",
        "requires_libcob_py: tests that need the parallel-built libcob_py runtime",
        "requires_cobc: tests that need a built cobc compiler binary",
        "requires_dual_cobc: tests that need both the original C and refactored "
        "Python cobc binaries",
        "requires_curses: tests that need the stdlib curses module",
    )
    for marker in markers:
        config.addinivalue_line("markers", marker)


# ===========================================================================
# Phase 7 - Small shared, standard-library-only assertion helpers
# ===========================================================================
def hexbytes(value) -> str:
    """Return the lowercase hex representation of a byte-like value.

    Accepts ``bytes``, ``bytearray``, ``memoryview`` or any iterable of ints
    that :class:`bytes` accepts.  Handy for producing readable diffs in
    byte-for-byte storage-layout and numeric-parity assertions, e.g.::

        assert hexbytes(field.data) == "f0f1f2c3"
    """
    return bytes(value).hex()


@pytest.fixture
def make_field():
    """Return a defensive factory that builds a ``libcob_py.common.cob_field``.

    The factory is created lazily so that this fixture only touches the runtime
    when a test actually requests it: ``pytest.importorskip`` is called at
    *fixture-call* time, yielding a clean skip when the parallel-built
    ``libcob_py`` runtime (or its ``common`` module) is not yet importable.

    The factory deliberately does NOT encode COBOL values itself - byte layout
    per ``USAGE`` lives in ``libcob_py.numeric`` / ``libcob_py.move`` (which may
    not exist yet).  Callers therefore supply the already-encoded ``data``
    bytes plus the field attributes::

        def test_zoned(make_field):
            common = pytest.importorskip("libcob_py.common")
            f = make_field(b"\\xf1\\xf2\\xf3", type=common.COB_TYPE_NUMERIC_DISPLAY,
                           digits=3, scale=0)
            assert f.size == 3
    """
    common = pytest.importorskip("libcob_py.common")

    def _make_field(data, type=None, digits=0, scale=0, flags=0, pic=None,
                    size=None):
        field_type = common.COB_TYPE_UNKNOWN if type is None else type
        attr = common.cob_field_attr(
            type=field_type, digits=digits, scale=scale, flags=flags, pic=pic,
        )
        payload = b"" if data is None else bytes(data)
        field_size = len(payload) if size is None else size
        return common.cob_field(size=field_size, data=bytearray(payload), attr=attr)

    return _make_field
