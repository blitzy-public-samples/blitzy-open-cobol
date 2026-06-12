#
# Makefile gnucobol/libcob_py
# Process this file with automake to produce Makefile.in
#
# Copyright (C) 2003,2004,2005,2006,2007 Keisuke Nishida
# Copyright (C) 2008-2011 Roger While
#
# This file is part of GNU Cobol.
#
# The GNU Cobol compiler is free software: you can redistribute it
# and/or modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation, either version 2 of the
# License, or (at your option) any later version.
#
# GNU Cobol is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with GNU Cobol.  If not, see <http://www.gnu.org/licenses/>.

# MIGRATION (C->Python): this directory is the pure-Python, standard-library
# runtime package "libcob_py" that REPLACES the former C runtime shared object
# (libcob/libcob.la, no longer built).  It is added to the top-level SUBDIRS
# (Makefile.am) and is wired into Automake recursion via AC_CONFIG_FILES in
# configure.ac (the "libcob_py/Makefile" entry).  This Makefile.am exists so
# that autoreconf/configure can generate libcob_py/Makefile and "make" can
# recurse into this directory without error (review: missing libcob_py/Makefile.am
# broke the build recursion).  See AAP 0.2.1 / 0.3.1 / 0.4.1.

# MIGRATION (C->Python): the runtime is delivered in three forms - (1) the
# in-tree package found via sys.path / COB_LIBRARY_PATH when cobc-emitted
# modules execute "from libcob_py import ...", (2) the installed package below,
# and (3) the self-contained zipapp archive dist/libcob_py.pyz built by the
# all-local rule.  All sources are pure standard-library Python (AAP 0.5.1
# stdlib-only requirement); nothing here is compiled by a C toolchain.

# The Python runtime modules plus the setuptools manifest.  The module list and
# ordering mirror the cob_init_* runtime initialisation sequence documented in
# AAP 0.3.2 (common is the base; numeric precedes move; the remaining modules
# depend on common).
COBPY_MODULES = \
	__init__.py \
	common.py \
	numeric.py \
	move.py \
	strings.py \
	intrinsic.py \
	fileio.py \
	call.py \
	screenio.py \
	termio.py \
	system.py

COBPY_FILES = $(COBPY_MODULES) pyproject.toml

# MIGRATION (C->Python): install the runtime package under a dedicated
# "libcob_py" directory inside pkgdatadir.  A plain *_DATA primary is used
# (rather than the python_PYTHON primary) because the backend interpreter is
# resolved through COB_PYTHON / AC_SUBST in configure.ac rather than through
# Automake's AM_PATH_PYTHON, so no $(pythondir) is defined.  Consumers locate
# the package by placing this directory on COB_LIBRARY_PATH / PYTHONPATH.
cobpydir = $(pkgdatadir)/libcob_py
# REVIEW FIX (MINOR #10): install EXACTLY the 11 runtime Python modules as
# package data.  pyproject.toml is a setuptools build manifest, not a runtime
# module, so it must NOT be installed alongside the runtime (it is distributed
# only via EXTRA_DIST below).  This also keeps the installed layout consistent
# with the self-contained ".pyz" staging (cobc_build_pyz / the PYZ recipe), both
# of which bundle modules only.  The "dist_" prefix is required because Automake
# does NOT distribute plain *_DATA files by default; it makes the 11 modules part
# of BOTH the install set and the "make dist" tarball (the previous
# "EXTRA_DIST = $(COBPY_FILES)" had distributed them, so dropping that line
# without "dist_" here would silently remove them from the source distribution).
dist_cobpy_DATA = $(COBPY_MODULES)

# MIGRATION (C->Python): ship the setuptools manifest in the distribution tarball
# so "make dist" packages it alongside the runtime.  REVIEW FIX (MINOR #10):
# pyproject.toml lives ONLY here (source-distribution input), never in
# (dist_)cobpy_DATA (installed runtime data).  The 11 runtime modules are
# distributed via the dist_cobpy_DATA primary above; pyproject.toml is added
# explicitly so the manifest is not dropped from the tarball.
EXTRA_DIST = pyproject.toml

# MIGRATION (C->Python): build the self-contained runtime archive
# dist/libcob_py.pyz (AAP 0.3.1 / 0.5.1) with the standard-library zipapp
# module.  The package sources are staged under a "libcob_py/" sub-directory of
# a temporary tree so the resulting archive exposes an importable "libcob_py"
# package (consumers can "import libcob_py" with the .pyz on sys.path), matching
# the in-tree and installed layouts.  A minimal "__main__.py" is staged at the
# archive root because zipapp requires an entry point; running the archive
# directly ("python libcob_py.pyz") prints a short usage note, while the package
# remains importable.  zipapp ships with CPython >= 3.11 (the COB_PYTHON gate
# enforced by configure.ac), so no extra dependency is introduced.  Built via
# all-local so a normal "make" produces the artifact; the .pyz is a generated
# build product (the dist/ directory is tracked only by its .gitkeep) and must
# not be committed.
PYZ_ARCHIVE = $(top_builddir)/dist/libcob_py.pyz

# REVIEW FIX (MINOR #10 / MINOR #11): stage ONLY the 11 runtime modules into the
# archive (no pyproject.toml) so the .pyz build inputs match dist/README.md and
# the driver's cobc_build_pyz behaviour - the manifest is a source-distribution
# input, not a runtime module bundled into the archive.
$(PYZ_ARCHIVE): $(COBPY_MODULES)
	@$(MKDIR_P) "$(top_builddir)/dist"
	@echo "Creating dist/libcob_py.pyz (zipapp) ..."
	@stagedir=`mktemp -d`; \
	$(MKDIR_P) "$$stagedir/libcob_py"; \
	for f in $(COBPY_MODULES); do \
		cp "$(srcdir)/$$f" "$$stagedir/libcob_py/$$f"; \
	done; \
	printf '%s\n' \
		'print("GNU Cobol pure-Python runtime package libcob_py.")' \
		'print("Place this .pyz on PYTHONPATH or COB_LIBRARY_PATH, then import libcob_py.")' \
		> "$$stagedir/__main__.py"; \
	$(COB_PYTHON) -m zipapp "$$stagedir" -o "$(PYZ_ARCHIVE)"; \
	status=$$?; \
	rm -rf "$$stagedir"; \
	exit $$status

all-local: $(PYZ_ARCHIVE)

# MIGRATION (C->Python): remove the generated zipapp archive and Python caches
# on "make clean" so the build tree returns to a pristine state.
clean-local:
	rm -f "$(PYZ_ARCHIVE)"
	rm -rf "$(srcdir)/__pycache__"
