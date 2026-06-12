"""libcob_py - Pure-Python runtime package for GNU Cobol (OpenCOBOL).

This is the Python replacement for the C ``libcob`` shared library.  It is the
runtime targeted by the rewritten code emitter (``cobc/codegen.c``): every
``cob_*`` call-site emitted for a compiled COBOL program resolves to a function
in one of the ``libcob_py`` sub-modules.

Runtime facade (AAP sections 0.3.2 "Runtime API facade" and 0.4.1)
------------------------------------------------------------------
Importing ``libcob_py`` eagerly loads every runtime sub-module in the canonical
dependency order (``common`` first - it is the base that all others import -
then ``numeric``, ``move``, ``strings``, ``intrinsic``, ``fileio``, ``call``,
``screenio``, ``termio`` and ``system``) and then **re-exports** the public
``cob_*`` / ``CBL_*`` / ``COB_*`` surface of every sub-module at the package
level.  This is the contract the emitter relies on:

* Generated modules begin with
  ``from libcob_py import common, numeric, move, strings, intrinsic, fileio,
  call, screenio, termio, system`` - satisfied because every name is a real
  sub-module of this package.
* The emitter's call router (``codegen.c`` ``codegen_pymod``) maps each emitted
  symbol to a provider sub-module, but its final fallback routes any otherwise
  unresolved symbol to the bare package ``libcob_py``.  Because the facade
  re-exports the entire ``cob_*`` surface, ``libcob_py.<symbol>`` resolves for
  every emitted symbol - so the fallback can never dangle (review finding:
  codegen "Runtime Facade Contract").

The facade also exposes the runtime start-up orchestration: :func:`cob_init`
runs the ``cob_init_*`` sequence (``numeric -> strings -> move -> intrinsic ->
fileio -> termio -> call``) exactly as the C ``cob_init`` does (common.c
L784-L790); ``screenio`` initialises lazily on first ACCEPT/DISPLAY and the
``system`` routines are registered by ``cob_init_call``, mirroring the C.

Standard library only - this package introduces ZERO third-party dependencies
(AAP sections 0.5 / 0.7.1).
"""

# ---------------------------------------------------------------------------
# Eager, ordered sub-module imports (the facade load order).
# ``common`` MUST be imported first: it is the runtime base that every other
# sub-module imports as ``from libcob_py import common``.  Importing it here
# binds it in ``sys.modules`` so the siblings' top-level imports resolve to the
# already-loaded module rather than triggering a re-entrant package import.
# The remaining order follows the cob_init_* dependency chain.
# ---------------------------------------------------------------------------
from libcob_py import common      # noqa: E402  base: exceptions, field model
from libcob_py import numeric     # noqa: E402  decimal.Decimal arithmetic
from libcob_py import move        # noqa: E402  cob_move / data-movement family
from libcob_py import strings     # noqa: E402  INSPECT / STRING / UNSTRING
from libcob_py import intrinsic   # noqa: E402  68 cob_intr_* functions
from libcob_py import fileio      # noqa: E402  seq/rel/indexed I/O, SORT/MERGE
from libcob_py import call        # noqa: E402  importlib dynamic CALL loader
from libcob_py import screenio    # noqa: E402  curses SCREEN SECTION I/O
from libcob_py import termio      # noqa: E402  plain ACCEPT/DISPLAY fallback
from libcob_py import system      # noqa: E402  CBL_*/C$ system routines

#: The runtime sub-modules in canonical facade order (also the search order
#: used when re-exporting the public symbol surface, below).
_RUNTIME_MODULES = (
    common, numeric, move, strings, intrinsic,
    fileio, call, screenio, termio, system,
)

#: Prefixes that identify a public runtime symbol eligible for re-export.
#: ``cob_*`` is the emitted runtime API; ``CBL_``/``C$`` (and the ACUCOBOL
#: ``cob_acuw_*`` internals) are the system routines; ``COB_`` are the public
#: constants (types, flags, exception ids, CRT-status values, ...).
_EXPORT_PREFIXES = ("cob_", "COB_", "CBL_")


def _build_facade():
    """Re-export the public ``cob_*`` / ``COB_*`` / ``CBL_*`` surface.

    Walks each runtime sub-module in dependency order and binds every matching
    public name into this package's namespace using *first-wins* precedence, so
    ``common`` (imported first) is authoritative for the shared ``COB_*``
    constants while each unique function (``cob_move``, ``cob_intr_length``,
    ``CBL_AND``, ...) is bound from its owning module.  Returns the sorted list
    of exported names for :data:`__all__`.
    """
    g = globals()
    exported = set()
    for module in _RUNTIME_MODULES:
        for name in dir(module):
            if name.startswith("_"):
                continue
            if not name.startswith(_EXPORT_PREFIXES):
                continue
            if name in g:           # first-wins: keep the earliest binding
                continue
            g[name] = getattr(module, name)
            exported.add(name)
    # Always expose the sub-modules themselves and the orchestration helpers.
    exported.update(m.__name__.rsplit(".", 1)[1] for m in _RUNTIME_MODULES)
    exported.update({"cob_init", "cobinit", "cob_init_runtime"})
    return sorted(exported)


def cob_init(argc=0, argv=None):
    """Initialise the COBOL runtime (delegates to :func:`common.cob_init`).

    Idempotent and order-faithful: runs the subsystem ``cob_init_*`` sequence
    ``numeric -> strings -> move -> intrinsic -> fileio -> termio -> call``
    (common.c L784-L790).  Exposed on the package so generated ``main()``
    entry points and embedders can call ``libcob_py.cob_init(...)`` directly.
    """
    return common.cob_init(argc, argv)


def cobinit():
    """Convenience runtime initialiser returning 0 (mirrors common.cobinit)."""
    return common.cobinit()


def cob_init_runtime(argc=0, argv=None):
    """Alias for :func:`cob_init` with an explicit, descriptive name."""
    return cob_init(argc, argv)


# Build the re-export surface now that the orchestration helpers are defined.
__all__ = _build_facade()
