# dist/ — GNU Cobol Python runtime distribution

This directory holds the distributable build artifact of the GNU Cobol **Python
runtime**: the self-contained `zipapp` archive **`dist/libcob_py.pyz`**. It
packages the pure-Python runtime package [`../libcob_py/`](../libcob_py/) — the
Python-era successor to the C `libcob` run-time library (the former `libcob.so`,
`.o`, and linked-executable artifacts). The archive is **generated at
build/distribution time**; this `README.md` is the version-controlled build note
describing how it is produced and consumed.

## Generated artifact (not committed)

- **`dist/libcob_py.pyz`** — the runtime archive. It is produced by the build
  (see below) and is **not** version-controlled; rebuild it from `../libcob_py/`
  rather than expecting it in a fresh checkout.
- Only **`README.md`** and **`.gitkeep`** are tracked in `dist/`. The empty
  `.gitkeep` keeps this output-only directory present in fresh checkouts, which
  is **required**: `python -m zipapp -o dist/libcob_py.pyz …` does **not** create
  its target's parent directory, so a missing `dist/` makes the build fail with a
  `FileNotFoundError` (a non-zero exit).

## Build

Tooling is the Python **standard library only** — `py_compile` (syntax/bytecode
validation) and `zipapp` (packaging); **no third-party package** is involved. Use
the same Python **≥ 3.11** interpreter the build detects and exposes as
`$COB_PYTHON` (see `configure.ac`).

```sh
# Run from the repository root. $COB_PYTHON is the Python >=3.11 interpreter (see configure.ac).

# 1) (recommended) syntax/bytecode validation of the runtime — stdlib py_compile, no 3rd-party
"$COB_PYTHON" -m py_compile libcob_py/*.py

# 2) stage libcob_py/ as an importable SUBPACKAGE (so `import libcob_py` keeps working),
#    add a tiny entry point, then build the self-contained archive into the (existing) dist/ dir
rm -rf build/pyz && mkdir -p build/pyz/libcob_py
cp -p libcob_py/*.py build/pyz/libcob_py/
printf 'import libcob_py\nprint("GNU Cobol Python runtime", libcob_py.__version__)\n' > build/pyz/__main__.py
"$COB_PYTHON" -m zipapp build/pyz -o dist/libcob_py.pyz -p "/usr/bin/env python3"
```

`build/pyz` is a transient staging/scratch directory (not committed); the
resulting archive ships `libcob_py/` as a proper **subpackage**, so
`from libcob_py import …` resolves once the `.pyz` is on `sys.path`, and
`python dist/libcob_py.pyz` runs the staged entry point.

**Why not the one-line shorthand?** The *intent* is the shorthand
`python -m zipapp libcob_py -o dist/libcob_py.pyz`, but that bare form does not
work and the staged recipe above is the *working* form:

1. `libcob_py/` is a library package (`__init__.py`, no `__main__.py`), so
   `zipapp` rejects it with `ZipAppError: Archive has no entry point`.
2. `zipapp` copies the *contents* of its source directory to the archive root,
   which would flatten the package and break the emitter's
   `from libcob_py import …` calls (`ModuleNotFoundError: No module named
   'libcob_py'`).

## Build inputs

The archive bundles exactly the **11** standard-library-only runtime modules of
the [`../libcob_py/`](../libcob_py/) package — `__init__.py`, `common.py`,
`numeric.py`, `move.py`, `strings.py`, `intrinsic.py`, `fileio.py`, `call.py`,
`screenio.py`, `termio.py`, `system.py` — matching the `cp -p libcob_py/*.py …`
staging command above. `pyproject.toml` is **not** a build input to the `.pyz`:
it is the setuptools manifest used only for source distribution / packaging
metadata, and is intentionally excluded from the runtime archive (consistent with
`libcob_py/Makefile.am`'s `cobpy_DATA`/PYZ recipe and the driver's
`cobc_build_pyz`, which all bundle the runtime modules only). Rebuild
`dist/libcob_py.pyz` whenever anything under `libcob_py/` changes (current
version: `1.1.0`).

## How `cobc` consumes it

`cobc`'s `-m` (module) and `-x` (executable) modes package a compiled COBOL unit
together with this runtime; the module extension `COB_MODULE_EXT` is `.pyz` (set
by `configure.ac` and emitted into `defaults.h` by the top-level `Makefile.am`).
At run time the archive is placed on `sys.path` — e.g. via `COB_LIBRARY_PATH`,
honored by `libcob_py/call.py` — so the emitted COBOL `.py` modules can
`import libcob_py` / `from libcob_py import …`.

## Security note: the `SYSTEM` routine

The COBOL `SYSTEM` / `CALL "SYSTEM"` service in `libcob_py/system.py` is defined
by the language to hand its operand to the host command processor (`/bin/sh -c
<command>`), exactly mirroring the original C runtime's `system()` call. A full
command line — pipes, redirection, `&&` — is therefore a **legitimate and
required** feature, so shell metacharacters execute by design. `SYSTEM` is a
**trusted-command-only** facility: the command text must originate in the
running COBOL program (the same trust boundary as the original toolchain) and
**must not** be assembled from untrusted external input (CWE-78, OS command
injection). See the `SYSTEM` docstring in `libcob_py/system.py` for the full
rationale and the NUL-truncation / length-cap hardening that is applied.

---

Part of GNU Cobol. The packaged runtime replaces the C `libcob` run-time library.
