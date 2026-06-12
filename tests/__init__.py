"""Package marker for the repository ``tests`` tree.

WHY THIS FILE EXISTS (import-collision avoidance)
-------------------------------------------------
The GNU Cobol C->Python backend refactor adds a pure-Python runtime package at
the repository root: ``libcob_py/`` (AAP sections 0.3.1 / 0.4.1).  The new
pytest suite lives in ``tests/libcob_py/`` and is itself a Python *package*
(it carries ``tests/libcob_py/__init__.py``) so that pytest has a stable,
importable path for its ``test_*.py`` modules and the shared ``conftest.py``.

Those two packages share the *basename* ``libcob_py``.  Under pytest's default
("prepend") import mode the module name assigned to a test file is derived from
its path relative to the first ancestor directory that does **not** contain an
``__init__.py`` (pytest's "rootdir" for that file).  If ``tests/`` were not a
package, that ancestor would be ``tests/`` itself, pytest would put ``tests/``
on ``sys.path`` and import the suite as the *top-level* package ``libcob_py`` --
shadowing the real runtime package of the same name.  Every
``import libcob_py`` (and every ``pytest.importorskip("libcob_py.<module>")``)
inside a test would then resolve to the *test* package instead of the runtime,
and the runtime tests would silently skip, defeating the >=80% coverage gate.

Marking ``tests/`` as a package (this file) makes the suite import under the
fully-qualified, collision-free name ``tests.libcob_py.<name>``: pytest now
walks up past ``tests/`` to the repository root, places the repository root on
``sys.path``, and ``import libcob_py`` correctly resolves to the *runtime*
package ``<repo>/libcob_py/``.  This is exactly the design documented in
``tests/libcob_py/__init__.py`` ("modules import as ``tests.libcob_py.<name>``,
which is collision-free").  See ``tests/libcob_py/conftest.py`` for the
companion ``sys.path`` insertion that guarantees the repository root is present
regardless of how pytest is invoked.

SCOPE / SAFETY
--------------
This is a pure Python import marker.  It is invisible to the GNU Autotest
``*.at`` suites (``tests/syntax``, ``tests/run``, ``tests/data-rep``) and to the
NIST CCVS85 oracle under ``tests/cobol85/`` -- those are driven by shell/`make`
and never import ``tests`` as a Python package.  It introduces ZERO third-party
dependencies (AAP sections 0.5 / 0.7.1) and contains no executable logic.
"""
