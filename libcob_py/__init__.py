"""libcob_py - Pure-Python runtime package for GNU Cobol (OpenCOBOL).

This is the Python replacement for the C ``libcob`` shared library.  It is the
runtime targeted by the rewritten code emitter (``cobc/codegen.c``): every
``cob_*`` call-site emitted for a compiled COBOL program resolves to a function
in one of the ``libcob_py`` sub-modules.

This ``__init__`` module is intentionally minimal.  The full runtime *facade*
(re-exporting the ``cob_*``-equivalent surface and orchestrating the
``cob_init_*`` start-up sequence) is provided by the dedicated facade
implementation (AAP section 0.3.2 / 0.4.1).  Until that facade is in place this
file simply marks ``libcob_py`` as an importable package so that the individual
runtime modules (``common``, ``numeric``, ``move``, ...) can be imported as
``libcob_py.<module>``.

Standard library only - this package introduces ZERO third-party dependencies
(AAP sections 0.5 / 0.7.1).
"""

# NOTE: Deliberately no eager sub-module imports here.  ``common`` is the base
# module that every other runtime module imports; importing siblings at package
# import time would create import cycles.  The facade performs ordered
# initialisation explicitly via ``libcob_py.common.cob_init``.

__all__ = []  # populated by the runtime facade when it supersedes this marker
