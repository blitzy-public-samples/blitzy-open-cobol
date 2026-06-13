"""Module-mode launcher for ``cobcrun`` (and ``python3 -m libcob_py <prog>``).

MIGRATION (C->Python): this module is the Python analogue of the C ``cobcrun``
run sequence (bin/cobcrun.c L154-L159).  The native runner used to do::

    cob_init (argc, argv);
    p = cob_resolve (argv[1]);      /* dlopen + dlsym the module entry */
    ret = ((cob_call_func) p) ();   /* invoke the COBOL program */
    cob_stop_run (ret);             /* run exit handlers, propagate status */

Under the CPython backend a COBOL program compiled with ``cobc -m`` is a
self-contained ``<PROGRAM-ID>.pyz`` zip archive that bundles both the emitted
module(s) and the ``libcob_py`` runtime package (see ``cobc_build_pyz`` in
cobc/cobc.c).  ``cobcrun`` locates that archive, puts it on ``PYTHONPATH``, and
re-invokes the interpreter as ``python3 -m libcob_py <PROGRAM> [param ...]`` so
that THIS launcher runs from *inside* the archive (zipimport).  We then perform
exactly the four native steps above:

    1. ``common.cob_init`` - subsystem start-up + command-line capture.
    2. ``call.cob_resolve`` - import the named module (now reachable on
       ``sys.path`` because the ``.pyz`` is on ``PYTHONPATH``) and fetch its
       entry callable; this is the ``dlopen``/``dlsym`` analogue.
    3. invoke the entry - the emitted ``def <PROGRAM-ID> (*_extra)`` returns the
       COBOL ``RETURN-CODE`` as an ``int`` (same value ``cobc -x`` mains pass to
       ``cob_stop_run`` - see the generated ``main()``).
    4. ``common.cob_stop_run`` - run exit handlers, tear the runtime down, and
       propagate the status as the process exit code (``cob_stop_run`` raises
       ``SystemExit``), preserving the native exit-status contract.

EXTERNAL data items shared across the run unit (e.g. test 38's ``EXT-VAR``)
work because every module imported during the run resolves ``import libcob_py``
to the SAME bundled package instance, so ``common``'s EXTERNAL storage registry
is shared between the caller and any CALLed subprogram - exactly as the single
C ``libcob.so`` instance shared EXTERNAL storage across dlopen'd modules.
"""

import sys

from libcob_py import common
from libcob_py import call


def _run(args):
    """Run COBOL program ``args[0]`` with ``args[1:]`` as its run-unit argv.

    Returns an ``int`` status only on the (unreachable) path where
    ``cob_stop_run`` does not exit; in practice ``cob_stop_run`` /
    ``cob_call_error`` raise ``SystemExit`` and this function does not return.
    """
    if not args:
        # No program name: mirror cobcrun's usage failure (exit code 1).
        common.cob_runtime_error("%s", "cobcrun: no PROGRAM name given")
        return 1

    name = args[0]

    # MIGRATION (C->Python): initialise the runtime with the program's OWN argv
    # (argv[0] == the program name, argv[1:] == its parameters).  The C cobcrun
    # passes the module name as argv[0] to the run unit, so ACCEPT FROM
    # COMMAND-LINE / cob_command_line observe the same vector here.
    common.cob_init(len(args), list(args))

    # MIGRATION (C->Python): cob_resolve is the dlopen+dlsym analogue - it
    # imports the named module from sys.path (the .pyz that cobcrun placed on
    # PYTHONPATH) and returns the entry callable, or None on failure.
    func = call.cob_resolve(name)
    if func is None:
        # Mirror the native cob_call_error(): report the pending resolve error
        # and stop the run unit with a non-zero status (raises SystemExit).
        call.cob_call_error()
        return 1

    # Invoke the COBOL program.  The emitted entry returns the RETURN-CODE.
    ret = func()

    # Propagate the status exactly as the generated ``main()`` does; a COBOL
    # program with no explicit RETURN-CODE returns None -> exit status 0.
    common.cob_stop_run(0 if ret is None else int(ret))
    return 0  # unreachable: cob_stop_run raised SystemExit


if __name__ == "__main__":
    # sys.argv[0] is this launcher; sys.argv[1] is the COBOL PROGRAM name and
    # sys.argv[2:] are its parameters (cobcrun built the vector this way).
    sys.exit(_run(sys.argv[1:]))
