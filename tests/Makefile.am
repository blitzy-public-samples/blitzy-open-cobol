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
# (AC_SUBST'd by configure.ac). pytest/coverage are dev-only tools (AAP 0.5.1):
# if absent we warn and skip rather than break `make check`.
check-local:
	@echo "=== libcob_py runtime suite (C->Python migration) ==="; \
	if $(COB_PYTHON) -m pytest --version >/dev/null 2>&1; then \
	  PYTHONPATH="$(abs_top_srcdir):$$PYTHONPATH"; export PYTHONPATH; \
	  if $(COB_PYTHON) -m coverage --version >/dev/null 2>&1; then \
	    $(COB_PYTHON) -m coverage run --branch --source="$(abs_top_srcdir)/libcob_py" \
	        -m pytest -q "$(abs_srcdir)/libcob_py" && \
	    $(COB_PYTHON) -m coverage report --fail-under=80; \
	  else \
	    echo "NOTE: coverage.py not installed; running pytest without the >=80% coverage gate"; \
	    $(COB_PYTHON) -m pytest -q "$(abs_srcdir)/libcob_py"; \
	  fi; \
	else \
	  echo "WARNING: '$(COB_PYTHON) -m pytest' unavailable; skipping libcob_py suite (dev-only pytest not installed)"; \
	fi
