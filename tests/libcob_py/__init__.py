"""Package marker for the ``libcob_py`` pytest suite.

This package holds the pytest unit tests and the byte-for-byte numeric
*parity* harness for the pure-Python ``libcob_py`` runtime that replaces the
C ``libcob`` shared library in the GNU Cobol (OpenCOBOL) C->Python backend
refactor (AAP sections 0.2.1, 0.3.1, 0.4.1).

This directory carries this ``__init__.py`` so it is a Python *package*: that
gives pytest a stable, importable package path for the ``test_*.py`` modules it
contains and lets the suite expose shared helpers such as :data:`REPO_ROOT`.

Collision-free collection, however, is guaranteed by the project's pytest
*invocation* -- not by any repository-level package marker.  ``tests/Makefile.am``
runs pytest with ``--import-mode=importlib -o consider_namespace_packages=true``.
The importlib import mode gives every test module a unique, path-derived module
name, so identically-named ``test_*`` files or ``conftest`` modules across the
repository's several test trees -- notably the GNU Autotest ``*.at`` suites
under ``tests/`` and the NIST CCVS85 oracle under ``tests/cobol85/`` -- never
collide (no "import-file-mismatch" during collection).  The
``consider_namespace_packages`` option lets the runtime package ``libcob_py`` at
the repository root resolve cleanly without being shadowed by this
identically-stemmed test package.  No repository-level ``tests/__init__.py``
marker is used (or needed); adding one would be out-of-scope for the AAP file
set (REVIEW FIX CRITICAL #4).

Standard library only -- this file (like the runtime it tests) introduces ZERO
third-party dependencies (AAP sections 0.5 / 0.7.1).  ``pytest`` and
``coverage.py`` are development-only tools and are intentionally NOT imported
here; nor is the runtime package ``libcob_py`` imported at module top level,
because the suite may be authored before its sibling ``libcob_py/`` package
exists.  The repository root is placed on ``sys.path`` at test-run time by
``conftest.py`` -- this marker performs no ``sys.path`` mutation of its own.
"""

import pathlib

# Absolute path to the repository root, derived purely from this file's
# location so it is independent of the caller's current working directory.
#
# Path layout (``Path.resolve().parents`` indexing):
#   this file -> <repo>/tests/libcob_py/__init__.py
#   parents[0] -> <repo>/tests/libcob_py
#   parents[1] -> <repo>/tests
#   parents[2] -> <repo>            (the repository root)
#
# Test modules and ``conftest.py`` may import this constant for building paths
# to reference fixtures or the sibling ``libcob_py`` package.  NOTE: this
# module only *defines* the constant; it deliberately does not append it to
# ``sys.path`` -- that is the responsibility of ``conftest.py``.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

__all__ = ["REPO_ROOT"]
