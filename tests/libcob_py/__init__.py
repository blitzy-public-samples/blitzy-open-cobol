"""Package marker for the ``libcob_py`` pytest suite.

This package holds the pytest unit tests and the byte-for-byte numeric
*parity* harness for the pure-Python ``libcob_py`` runtime that replaces the
C ``libcob`` shared library in the GNU Cobol (OpenCOBOL) C->Python backend
refactor (AAP sections 0.2.1, 0.3.1, 0.4.1).

The sole reason this directory is a Python *package* (rather than a bare
folder of scripts) is to give pytest a stable, importable package path for the
``test_*.py`` modules it contains.  The repository ships several unrelated
test trees -- notably the GNU Autotest ``*.at`` suites under ``tests/`` and the
NIST CCVS85 oracle under ``tests/cobol85/`` -- and a number of ``test_*`` files
can share a basename across trees.  Without this ``__init__.py`` marker pytest
would fall back to "rootdir" import mode and could assign colliding top-level
module names (e.g. two ``conftest`` or ``test_common`` modules), raising an
import-file-mismatch error during collection.  Marking ``tests/libcob_py`` as a
package makes the modules import as ``tests.libcob_py.<name>``, which is
collision-free.

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
