#
# Makefile gnucobol/tests
# Process this file with automake to produce Makefile.in
#
# Copyright (C) 2003,2004,2005,2006,2007 Keisuke Nishida
# Copyright (C) 2008-2011 Roger While
#
# This file is part of GNU Cobol.
#
# The GNU Cobol compiler is free software; you can redistribute it
# and/or modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# GNU Cobol is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with GNU Cobol; see the file COPYING. If not, write to
# the Free Software Foundation, 51 Franklin Street, Fifth Floor,
# Boston, MA 02110-1301 USA

SUBDIRS = cobol85

# MIGRATION (C->Python): the C->Python backend refactor requires regenerating
# the build (autoreconf) so configure emits the new COB_PYTHON macro into
# defaults.h.  The original tree was built from a pre-generated configure made
# by automake 1.9.x, whose implicit "serial-tests" harness permits "$(srcdir)"
# inside the TESTS list below.  Modern automake (>= 1.13) defaults to the
# "parallel-tests" harness, which rejects "$(srcdir)" in TESTS and aborts
# autoreconf.  Restoring "serial-tests" here re-establishes the EXACT original
# Autotest behaviour (the suites already ran serially) with zero change to how
# the syntax/run/data-rep oracles execute, and is the minimal fix that unblocks
# autoreconf.  The NIST CCVS85 oracle under cobol85/ is untouched.
AUTOMAKE_OPTIONS = serial-tests

TESTS = $(srcdir)/syntax \
	$(srcdir)/run \
	$(srcdir)/run-O \
	$(srcdir)/data-rep \
	$(srcdir)/data-rep-O

syntax_DEPENDENCIES = \
	syntax.at \
	syntax.src/copy.at \
	syntax.src/definition.at \
	syntax.src/expression.at \
	syntax.src/multiply.at \
	syntax.src/redefines.at \
	syntax.src/value.at \
	syntax.src/move.at \
	syntax.src/set.at \
	syntax.src/occurs.at \
	syntax.src/subscripts.at

run_DEPENDENCIES = \
	run.at \
	run.src/fundamental.at \
	run.src/accept.at \
	run.src/initialize.at \
	run.src/misc.at \
	run.src/ref-mod.at \
	run.src/subscripts.at \
	run.src/extensions.at \
	run.src/return-code.at \
	run.src/functions.at

data_rep_DEPENDENCIES = \
	data-rep.at \
	data-rep.src/binary.at \
	data-rep.src/display.at \
	data-rep.src/packed.at \
	data-rep.src/pointer.at \
	data-rep.src/numeric-dump.cob \
	data-rep.src/numeric-display.cob

EXTRA_DIST = $(srcdir)/package.m4 $(TESTS) \
	$(syntax_DEPENDENCIES) \
	$(run_DEPENDENCIES) \
	$(data_rep_DEPENDENCIES)

# MIGRATION (C→Python): distribute the new pytest-based libcob_py runtime suite.
# These files are authored by the tests/libcob_py/ child agent in this same
# migration; listing them here ensures `make dist` packages the Python unit +
# byte-for-byte parity suite alongside the existing Autotest dependencies above.
EXTRA_DIST += $(srcdir)/libcob_py/__init__.py \
	$(srcdir)/libcob_py/conftest.py \
	$(srcdir)/libcob_py/test_common.py \
	$(srcdir)/libcob_py/test_numeric.py \
	$(srcdir)/libcob_py/test_move.py \
	$(srcdir)/libcob_py/test_strings.py \
	$(srcdir)/libcob_py/test_intrinsic.py \
	$(srcdir)/libcob_py/test_fileio.py \
	$(srcdir)/libcob_py/test_call.py \
	$(srcdir)/libcob_py/test_screenio.py \
	$(srcdir)/libcob_py/test_termio.py \
	$(srcdir)/libcob_py/test_system.py \
	$(srcdir)/libcob_py/test_numeric_parity.py

DISTCLEANFILES = atconfig

all: $(TESTS)

clean-local:
	rm -rf *.dir *.log
# MIGRATION (C→Python): also remove Python test artifacts (coverage/pytest caches)
# produced by the check-local libcob_py suite below.
	rm -rf $(srcdir)/libcob_py/__pycache__ $(srcdir)/libcob_py/.pytest_cache
	rm -f $(srcdir)/.coverage $(srcdir)/libcob_py/.coverage

$(srcdir)/package.m4: $(top_srcdir)/configure.ac
	@echo "creating $(srcdir)/package.m4"
	@{								   \
	  echo '# Signature of the current package.';			   \
	  echo 'm4_define([AT_PACKAGE_NAME],	  [@PACKAGE_NAME@])';	   \
	  echo 'm4_define([AT_PACKAGE_TARNAME],	  [@PACKAGE_TARNAME@])';   \
	  echo 'm4_define([AT_PACKAGE_VERSION],	  [@PACKAGE_VERSION@])';   \
	  echo 'm4_define([AT_PACKAGE_STRING],	  [@PACKAGE_STRING@])';    \
	  echo 'm4_define([AT_PACKAGE_BUGREPORT], [@PACKAGE_BUGREPORT@])'; \
	} > $(srcdir)/package.m4

$(srcdir)/%: $(srcdir)/%.at $(srcdir)/package.m4
	autom4te --language=autotest -I $(srcdir) -I $@.src -o $@ $<
	chmod +x $@

$(srcdir)/syntax: $(syntax_DEPENDENCIES)
$(srcdir)/run: $(run_DEPENDENCIES)
$(srcdir)/data-rep: $(data_rep_DEPENDENCIES)

# MIGRATION (C→Python): the libcob.so C runtime is replaced by the pure-Python
# libcob_py package (repo root). Its pytest-based unit + byte-for-byte parity suite
# lives in $(srcdir)/libcob_py and is run here so `make check` enforces the
# runtime-regression gate (AAP 0.7.1) and the >=80% per-module coverage gate.
# Additive: does NOT touch the Autotest TESTS above. Interpreter = $(COB_PYTHON)
# (AC_SUBST'd by configure.ac). pytest/coverage are dev-only tools (AAP 0.5.1).
#
# MIGRATION (C->Python) / REVIEW FIX (MAJOR #6, R5/R7): the runtime pytest suite
# and the >=80% PER-MODULE line-coverage gate are MANDATORY by default, so the
# delivery / default `make check` path can no longer silently skip them.  If
# either pytest or coverage.py is unavailable, `make check` FAILS (exit 1).
# A single, explicitly-named local opt-out -- COB_PY_SKIP_RUNTIME_TESTS=1
# (e.g. `make check COB_PY_SKIP_RUNTIME_TESTS=1`) -- lets a developer who lacks
# the dev-only tools run the rest of `make check`; it must NEVER be set in CI /
# release gating.  pytest/coverage remain dev-only tools (AAP 0.5.1); they are
# not runtime dependencies of libcob_py.
#
# Two further review fixes are folded in here:
#   * Per-module enforcement (R5): instead of a single aggregate
#     `coverage report --fail-under=80`, EACH of the 11 libcob_py modules is
#     gated individually at >=80% LINE coverage (R5 specifies line coverage),
#     using coverage.py's own --include + --fail-under, so any one module
#     dropping below 80% fails the build.  Coverage is collected WITHOUT
#     --branch so the reported/gated metric is line coverage exactly as R5
#     requires (a blended line+branch metric would over-count and misrepresent
#     the R5 threshold).
#   * Collision-free import (REVIEW FIX CRITICAL #4): pytest is invoked with
#     --import-mode=importlib -o consider_namespace_packages=true, so the test
#     package $(abs_srcdir)/libcob_py no longer needs a tests/__init__.py marker
#     (that out-of-scope file is removed) and `import libcob_py` resolves to the
#     runtime package at the repo root, never the identically-named test package.
#   * Run-from-root (QA FIX MAJOR M1): the coverage/pytest step is launched from
#     the repository ROOT (`cd "$(abs_top_srcdir)"`), NOT from $(srcdir)=tests.
#     `python -m pytest` puts its working directory on sys.path[0]; when that was
#     tests/, the test package tests/libcob_py SHADOWED the runtime package, so
#     every `import libcob_py.<module>` resolved to the test package, raised
#     ModuleNotFoundError, and pytest.importorskip skipped ALL 11 modules ("No
#     data was collected" -> the gate enforced nothing).  Running from the repo
#     root makes sys.path[0] the root so `import libcob_py` resolves to the
#     runtime.  COVERAGE_FILE is pinned to $(srcdir)/.coverage so the data file
#     still lands under tests/ for the clean-local/distclean cleanup above, and
#     the per-module `coverage report --include=<abs>` calls already use absolute
#     paths so they are cwd-independent.
check-local:
	@echo "=== libcob_py runtime suite (C->Python migration) ==="; \
	if test -n "$(COB_PY_SKIP_RUNTIME_TESTS)"; then \
	  echo "WARNING: COB_PY_SKIP_RUNTIME_TESTS is set; SKIPPING the libcob_py runtime +"; \
	  echo "         per-module coverage gate.  This is a LOCAL-ONLY opt-out and must"; \
	  echo "         never be set in CI / release gating (AAP 0.7.1 / R5 / R7)."; \
	else \
	  if ! $(COB_PYTHON) -m pytest --version >/dev/null 2>&1; then \
	    echo "ERROR: '$(COB_PYTHON) -m pytest' is unavailable; the runtime-regression gate" >&2; \
	    echo "       (AAP 0.7.1 / R7) cannot run.  Install pytest, or set" >&2; \
	    echo "       COB_PY_SKIP_RUNTIME_TESTS=1 for a local-only build without the gate." >&2; \
	    exit 1; \
	  fi; \
	  if ! $(COB_PYTHON) -m coverage --version >/dev/null 2>&1; then \
	    echo "ERROR: '$(COB_PYTHON) -m coverage' is unavailable; the >=80% per-module" >&2; \
	    echo "       line-coverage gate (AAP 0.7.1 / R5) cannot run.  Install coverage.py," >&2; \
	    echo "       or set COB_PY_SKIP_RUNTIME_TESTS=1 for a local-only build." >&2; \
	    exit 1; \
	  fi; \
	  PYTHONPATH="$(abs_top_srcdir):$$PYTHONPATH"; export PYTHONPATH; \
	  COVERAGE_FILE="$(abs_srcdir)/.coverage"; export COVERAGE_FILE; \
	  cd "$(abs_top_srcdir)" && \
	  $(COB_PYTHON) -m coverage run --source="$(abs_top_srcdir)/libcob_py" \
	      -m pytest -q --import-mode=importlib -o consider_namespace_packages=true \
	      "$(abs_srcdir)/libcob_py" || exit 1; \
	  echo "--- per-module line coverage (gate: each module >= 80%) ---"; \
	  cov_fail=0; \
	  for m in __init__ common numeric move strings intrinsic fileio call screenio termio system; do \
	    if $(COB_PYTHON) -m coverage report --include="$(abs_top_srcdir)/libcob_py/$$m.py" --fail-under=80 >/dev/null 2>&1; then \
	      pc=`$(COB_PYTHON) -m coverage report --include="$(abs_top_srcdir)/libcob_py/$$m.py" 2>/dev/null | awk '/TOTAL/{print $$NF}'`; \
	      echo "  PASS libcob_py/$$m.py  $$pc"; \
	    else \
	      pc=`$(COB_PYTHON) -m coverage report --include="$(abs_top_srcdir)/libcob_py/$$m.py" 2>/dev/null | awk '/TOTAL/{print $$NF}'`; \
	      echo "  FAIL libcob_py/$$m.py  $$pc (below the 80% line-coverage gate)" >&2; \
	      cov_fail=1; \
	    fi; \
	  done; \
	  if test "$$cov_fail" != "0"; then \
	    echo "ERROR: one or more libcob_py modules are below the >=80% per-module" >&2; \
	    echo "       line-coverage gate (AAP 0.7.1 / R5)." >&2; \
	    exit 1; \
	  fi; \
	  echo "PASS: every libcob_py module meets the >=80% per-module line-coverage gate."; \
	fi
