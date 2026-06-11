#              
# Configure template for GNU Cobol                                         -*- Autoconf -*-
# Process this file with autoconf to produce a configure script.
#
# Copyright (C) 2001,2002,2003,2004,2005,2006,2007 Keisuke Nishida
# Copyright (C) 2007-2010 Roger While
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

AC_PREREQ(2.59)

AC_INIT([GNU Cobol],[1.1],[open-cobol-list@lists.sourceforge.net],[gnu-cobol])
AC_CONFIG_SRCDIR([libcob.h])
AC_CONFIG_HEADERS([config.h])
AC_CONFIG_TESTDIR([tests])
AC_CONFIG_MACRO_DIR([m4])

# Note for SUN Solaris (gcc)
# export/setenv CFLAGS "-m64 -mptr64"
# export/setenv LDFLAGS "-m64 -mptr64 -L/usr/local/lib/sparcv9"
# ./configure --libdir=/usr/local/lib/sparcv9
#
# Hack for AIX 64 bit (gcc)
# Required -
# export/setenv CFLAGS=-maix64
# export/setenv LDFLAGS=-maix64

if echo $CFLAGS | grep 'aix64' 1>/dev/null 2>&1
then
	if test -f /usr/ccs/bin/ar
	then
		AR="/usr/ccs/bin/ar -X64"
	else
		AR="ar -X64"
	fi
	NM="/usr/ccs/bin/nm -X64 -B"
fi

AM_INIT_AUTOMAKE([gnu no-texinfo.tex])
AM_MAINTAINER_MODE

# Autoheader templates
# MIGRATION (C→Python): Removed the Berkeley DB and ISAM autoheader templates
# (WITH_DB, USE_DB41, WITH_INDEX_EXTFH, WITH_SEQRA_EXTFH, WITH_CISAM,
# WITH_DISAM, WITH_VBISAM). Indexed/sequential file I/O is now provided by the
# pure-Python libcob_py.fileio module (Python stdlib dbm + sqlite3); the C
# Berkeley DB / external ISAM backends and their config macros are no longer
# built. See AAP 0.3.2 / 0.5.2 / 0.6.3.
# AH_TEMPLATE([WITH_LFS64], [Compile with large file system 64])
AH_TEMPLATE([COB_EXPERIMENTAL], [Enable experimemental code (Developers only!)])
AH_TEMPLATE([COB_PARAM_CHECK], [Enable CALL parameter checking])
AH_TEMPLATE([COB_EXPORT_DYN], [Compile/link option for exporting symbols])
AH_TEMPLATE([COB_PIC_FLAGS], [Compile/link option for PIC code])
AH_TEMPLATE([COB_SHARED_OPT], [Compile/link option for shared code])
AH_TEMPLATE([COB_STRIP_CMD], [Strip command])
# MIGRATION (C→Python): Removed USE_LIBDL template — dynamic program loading
# (CALL/CANCEL) is now handled by libcob_py.call via Python importlib +
# sys.path instead of the C dlopen/libltdl loader. See AAP 0.3.2 / 0.5.2.
AH_TEMPLATE([PATCH_LEVEL], [Define a patch level])
AH_TEMPLATE([WITH_VARSEQ], [Define variable sequential file format])
# AH_TEMPLATE([DLLHIDDEN], [Has visibility attribute])
AH_TEMPLATE([HAVE_PSIGN_OPT], [Has -Wno-pointer-sign])
AH_TEMPLATE([HAVE_ATTRIBUTE_ALIGNED], [Has __attribute__((aligned))])
AH_TEMPLATE([HAVE_TIMEZONE], [Has timezone variable])
AH_TEMPLATE([COB_EXEEXT], [Executable extension])
AH_TEMPLATE([COB_EXTRA_FLAGS], [Extra Compiler Flags])
AH_TEMPLATE([__USE_STRING_INLINES], [Compiler optimization])
AH_TEMPLATE([COB_HAS_INLINE], [Can use inline keyword])
# MIGRATION (C→Python): Removed COB_NO_SELFOPEN template — the libcob.so
# self-dlopen capability is irrelevant under the Python importlib loader.
# See AAP 0.5.2.
AH_TEMPLATE([COB_STRFTIME], [Can use strftime for timezone])
AH_TEMPLATE([COB_LI_IS_LL], [long int is long long])
# MIGRATION (C→Python): Removed HAVE_COLOR_SET template — SCREEN SECTION color
# handling is now provided by libcob_py.screenio via the Python stdlib curses
# module, not the C ncurses color_set() probe. See AAP 0.3.4 / 0.5.2.

AC_DEFINE(__USE_STRING_INLINES)

LIBCOB_LIBS="-lm"
LIBCOB_CFLAGS="$lt_prog_compiler_pic"

save_libs="$LIBS"

# MIGRATION (C→Python): AC_PROG_CC / AC_PROG_CPP / AC_PROG_EGREP are RETAINED,
# but their role has changed. Under the C→Python backend refactor (AAP 0.1.2)
# the C compiler ($CC) is used ONLY to build the cobc compiler driver and the
# cobcrun launcher, which remain C binaries. $CC is NO LONGER the COBOL
# code-generation backend: emitted COBOL units are now Python, compiled and
# executed by CPython (detected below as COB_PYTHON). See AAP 0.5.2.
AC_PROG_CC(gcc xlc cc)
AC_PROG_CPP
AC_PROG_EGREP

# MIGRATION (C→Python): Detect the Python 3.11+ interpreter that is the new
# COBOL backend. This conceptually replaces the C-backend detection the AAP
# table refers to ("AC_PROG_CC → python3>=3.11"); the build compiler above is
# kept for the driver binaries only. A bracket-free version test is used so
# the slicing expression is not consumed by m4 quoting. If no suitable Python
# is found, configure aborts with the AAP-mandated fatal message. See AAP
# 0.5.1 / 0.5.2 / 0.7.2.
AC_PATH_PROGS([PYTHON], [python3.11 python3 python], [])
AC_MSG_CHECKING([for Python 3.11 or newer (GNU Cobol backend)])
if test "x$PYTHON" = "x"
then
	AC_MSG_RESULT([not found])
	AC_MSG_ERROR([cobc: Python 3.11+ interpreter not found. Set COB_PYTHON (or use --with-python).])
fi
if $PYTHON -c 'import sys; sys.exit(0 if sys.version_info.major > 3 or (sys.version_info.major == 3 and sys.version_info.minor >= 11) else 1)' >/dev/null 2>&1
then
	cob_python_version=`$PYTHON -c 'import sys; print("%d.%d.%d" % (sys.version_info.major, sys.version_info.minor, sys.version_info.micro))' 2>/dev/null`
	AC_MSG_RESULT([$PYTHON ($cob_python_version)])
else
	AC_MSG_RESULT([$PYTHON is too old])
	AC_MSG_ERROR([cobc: Python 3.11+ interpreter not found. Set COB_PYTHON (or use --with-python).])
fi

# Configure options.

# MIGRATION (C→Python): The COB_CC variable and the --with-cc option (which
# named the C compiler the cobc driver shelled out to) become COB_PYTHON and
# --with-python: the path to the Python 3.11+ interpreter cobc uses as its
# backend. The detected $PYTHON above is the default. COB_PYTHON supersedes
# both COB_CC and COB_CFLAGS (the latter is removed entirely). See AAP 0.5.2 /
# 0.7.2.
COB_PYTHON=""
AC_ARG_WITH(python,
  [  --with-python=<path>    (GNU Cobol) specify the Python 3.11+ interpreter used by cobc],
  [ COB_PYTHON=$withval ], [ COB_PYTHON=$PYTHON] )

# MIGRATION (C→Python): Removed the external file-handler / ISAM / Berkeley DB
# selection options (--with-seqra-extfh, --with-cisam [incl. the ifisam/ifisamx
# lib check], --with-disam, --with-vbisam, --with-index-extfh, --with-db1,
# --with-db) together with their AC_CHECK_HEADERS/AC_CHECK_LIB probes and the
# LIBDB selector. Sequential/relative/indexed I/O (including SORT/MERGE and
# alternate keys) is now implemented entirely in pure Python by
# libcob_py.fileio on top of the Python stdlib dbm and sqlite3 modules, so no
# C ISAM/BDB library is detected or linked. See AAP 0.5.2 / 0.6.3.

#AC_ARG_WITH(lfs64,
#  [  --with-lfs64            (GNU Cobol) use large file system for file I/O (default)],
#  [ if test "$with_lfs64" = "yes"
#    then
#	AC_DEFINE(WITH_LFS64)
#    fi ],
#  [ AC_DEFINE(WITH_LFS64) ])

# MIGRATION (C→Python): Removed the --with-dl option and its DEFINE_DL /
# COB_TRY_DL selectors. Dynamic program loading (CALL/CANCEL) is now performed
# by libcob_py.call using Python importlib + sys.path (fed by COB_LIBRARY_PATH),
# replacing the C system dlopen / libltdl loader. See AAP 0.3.2 / 0.5.2.

GEN_DEBUG_OPT="no"
AC_ARG_ENABLE(debug,
  [  --enable-debug          (GNU Cobol) enable -g C compiler debug option],
  [if test "$enable_debug" = "yes"
   then
	GEN_DEBUG_OPT="yes"
   fi])

AC_ARG_ENABLE(experimental,
  [  --enable-experimental   (GNU Cobol) enable experimental code (Developers only!)],
  [if test "$enable_experimental" = "yes"
   then
	AC_DEFINE(COB_EXPERIMENTAL)
   fi])

AC_ARG_ENABLE(param-check,
  [  --enable-param-check    (GNU Cobol) enable CALL parameter checking],
  [if test "$enable_param_check" = "yes"
   then
	AC_DEFINE(COB_PARAM_CHECK)
   fi])

PATCH_LEVEL=0
AC_ARG_WITH(patch-level, [  --with-patch-level      (GNU Cobol) define a patch level (default 0)],
[case $with_patch_level in  
    yes)	AC_MSG_ERROR(["You must give --with-patch-level an argument."])
		;;
    no)		AC_MSG_ERROR(["--without-patch-level not supported."])
		;;
    [[0-9]]*)	PATCH_LEVEL=$with_patch_level
		;;
    *)		AC_MSG_ERROR(["You must use a numeric patch level"])
		;;
esac])
AC_DEFINE_UNQUOTED(PATCH_LEVEL, $PATCH_LEVEL)

WITH_VARSEQ=0
AC_ARG_WITH(varseq, [  --with-varseq           (GNU Cobol) define variable sequential format (default 0)],
[case $with_varseq in  
    yes)	AC_MSG_ERROR(["You must give --with-varseq an argument."])
		;;
    no)		AC_MSG_ERROR(["--without-varseq not supported."])
		;;
    [[0-3]])	WITH_VARSEQ=$with_varseq
		;;
    *)		AC_MSG_ERROR(["Invalid --with-varseq argument"])
		;;
esac])
AC_DEFINE_UNQUOTED(WITH_VARSEQ, $WITH_VARSEQ)

# Checks for programs.
# Stop the stupid tests for C++ and Fortran
AC_PROVIDE([AC_PROG_F77])
AC_PROVIDE([AC_PROG_CXX])
with_tags=
AC_PROG_LN_S
AC_PROG_INSTALL
AC_LIBTOOL_DLOPEN
AC_LIBTOOL_WIN32_DLL
AC_PROG_LIBTOOL
AC_PROG_MAKE_SET

# Checks for header files.
AC_HEADER_STDC
AC_CHECK_HEADERS([stdint.h sys/types.h signal.h fcntl.h malloc.h locale.h \
	stddef.h wchar.h dlfcn.h sys/time.h])

# Checks for typedefs, structures, and compiler characteristics.
AC_C_CONST
AC_C_BIGENDIAN
AC_TYPE_SIZE_T
AC_STRUCT_TM
# AX_C___ATTRIBUTE__
AC_C_INLINE
if test "$ac_cv_c_inline" != no 
then
	AC_DEFINE(COB_HAS_INLINE, 1)
fi


# Checks for library functions.
AC_FUNC_ALLOCA
AC_FUNC_VPRINTF
AC_CHECK_FUNCS([memmove memset setlocale fcntl strerror \
		strcasecmp strchr strrchr strdup strstr strtol gettimeofday])

# Check for timezone
AC_COMPILE_IFELSE([AC_LANG_PROGRAM([[#include <time.h>]], [[timezone = 3600;]])],
	[AC_DEFINE(HAVE_TIMEZONE, 1)],
	[])

# Checks for internationalization stuff
AM_ICONV
AM_LANGINFO_CODESET

# Checks for getopt_long.
# Cygwin has getopt_long_only prototype in kpathsea/getopt.h
# FreeBSD has gnugetopt library - needs checking

AC_CHECK_FUNC([getopt_long_only],
  [AC_CHECK_HEADERS([kpathsea/getopt.h getopt.h], [], [])],
  [AC_LIBOBJ(getopt)
   AC_LIBOBJ(getopt1)
   AC_LIBSOURCE(getopt.h)])

# MIGRATION (C→Python): Removed the GMP checks (gmp.h header probe, the -lgmp
# library check, the LIBCOB_LIBS append, and the header/library version-match
# test). COBOL fixed-point arithmetic is now performed by libcob_py.numeric
# using the Python stdlib decimal module (arbitrary-precision Decimal with a
# per-PICTURE context), which fully covers COBOL's 18-31 digit precision and
# every ROUNDED MODE — so the former GMP "blocker" does not apply. See AAP
# 0.3.3 / 0.5.2 / 0.6.2.

# Solaris has nanosleep in other libraries
AC_LINK_IFELSE([AC_LANG_PROGRAM([[#include <time.h>]],
  [[nanosleep (NULL, NULL);]])],
  [],
  [AC_CHECK_LIB([rt], [nanosleep])
   if test "x$ac_cv_lib_rt_nanosleep" = "xyes"
   then
     LIBCOB_LIBS="$LIBCOB_LIBS -lrt"
   else
     AC_CHECK_LIB([posix4], [nanosleep])
     if test "x$ac_cv_lib_posix4_nanosleep" = "xyes"
     then
       LIBCOB_LIBS="$LIBCOB_LIBS -lposix4"
     fi
   fi])

# Checks for gettext.
AM_GNU_GETTEXT([external])
AM_GNU_GETTEXT_VERSION(0.13)
if test "x$LTLIBINTL" != "x"
then
  COBC_LIBS="$COBC_LIBS $LTLIBINTL"
  LIBCOB_LIBS="$LIBCOB_LIBS $LTLIBINTL"
fi

# MIGRATION (C→Python): Removed the ncurses/pdcurses/curses library cascade,
# the matching header probes, the USE_NCURSES selector, the -lncurses/
# -lpdcurses/-lcurses LIBCOB_LIBS appends, and the HAVE_COLOR_SET color_set()
# link test. SCREEN SECTION terminal I/O is now provided by libcob_py.screenio
# (with a libcob_py.termio fallback) via the Python stdlib curses module, so no
# C curses library is detected or linked. See AAP 0.3.4 / 0.5.2.

# MIGRATION (C→Python): Removed the Berkeley DB detection block (USE_DB /
# HAS_DB41 selectors; the db1/db.h and db.h/db_185.h header probes; the
# db1/db-4.x/db version-compatibility AC_RUN_IFELSE tests; and every
# -ldb* LIBCOB_LIBS append). Indexed file storage is now provided by the
# pure-Python libcob_py.fileio module on top of the Python stdlib dbm (single
# key) and sqlite3 (alternate keys / complex indexing) modules. Existing BDB
# data files are migrated via the documented one-time utility; an unreadable
# indexed file yields COBOL file status 30. See AAP 0.5.2 / 0.6.3.

# MIGRATION (C→Python): Removed the dl/libltdl dynamic-loader detection
# (the _WIN32 probe, the dlopen checks in libc and -ldl, the USE_LIBDL define,
# the -ldl append, and the ltdl.h header / -lltdl library fallback). Dynamic
# program loading for CALL/CANCEL is now performed by libcob_py.call using
# Python importlib with sys.path fed by COB_LIBRARY_PATH (CANCEL evicts the
# module from sys.modules and calls importlib.invalidate_caches()). See AAP
# 0.3.2 / 0.5.2.

# Checks for workable strftime
AC_RUN_IFELSE([AC_LANG_PROGRAM([[#include <stdio.h>
	#include <time.h>]], [[
	time_t	ticks;
	int	ret;
	char	buff[16];
	ticks = time(NULL);
	ret = strftime(buff, 15, "%z", localtime(&ticks));
	if (ret != 5) {
		return 1;
	}
	if (buff[0] != '+' && buff[0] != '-') {
		return 1;
	}
	return 0;]])],
	[AC_DEFINE(COB_STRFTIME)],
	[],
	[])

# Checks for size of long
AC_RUN_IFELSE([AC_LANG_PROGRAM([[]], [[
	if (sizeof(long int) == sizeof(long long))
		return 0;
	return 1;
	]])],
	[AC_DEFINE(COB_LI_IS_LL)],
	[],
	[])

# GNU Cobol Configuration

# MIGRATION (C→Python): Removed the COB_CFLAGS include-path computation and the
# CPPFLAGS append that followed it. COB_CFLAGS held the C preprocessor/compiler
# include flags the cobc driver passed to the C backend when compiling
# generated C; under the Python backend there is no C compilation of generated
# code, so AAP 0.5.2 removes COB_CFLAGS entirely. The backend is configured
# solely through COB_PYTHON. See AAP 0.5.2.

# MIGRATION (C→Python): COB_LIBS / COB_LDFLAGS are legacy/no-op under the Python
# backend — they described the C link flags (-lcob and friends) used to link
# generated C objects, and there is no longer any C link step for emitted code.
# They are kept defined (harmless) so the defaults.h echoes in the top-level
# Makefile.am continue to expand without change. See AAP 0.5.2.
COB_LIBS="-L$libdir -lcob ${LIBCOB_LIBS}"
COB_LDFLAGS=""
if test "x$LDFLAGS" != "x"
then
  COB_LIBS="$LDFLAGS $COB_LIBS"
  COB_LDFLAGS="$LDFLAGS"
fi

COB_CONFIG_DIR="$datadir/$PACKAGE_TARNAME/config"
COB_COPY_DIR="$datadir/$PACKAGE_TARNAME/copy"
COB_LIBRARY_PATH="$libdir/$PACKAGE_TARNAME"
# MIGRATION (C→Python): The -m module mode now produces a self-contained Python
# zipapp archive (python -m zipapp) rather than a native shared object, so the
# COBOL module extension is fixed to ".pyz" instead of the platform shared-lib
# extension ($shlibext). See AAP 0.6.4.
COB_MODULE_EXT=".pyz"
LIBS="$save_libs"
COB_EXPORT_DYN="`eval echo $export_dynamic_flag_spec`"
COB_PIC_FLAGS=`echo "$lt_prog_compiler_pic" | sed -e 's/^ //'`
COB_SHARED_OPT="-shared"
COB_EXEEXT="$EXEEXT"
COB_EXTRA_FLAGS=""

if test "$GEN_DEBUG_OPT" = "no"
then
        CFLAGS=`echo "$CFLAGS" | sed -e 's/^-g//' -e 's/-g$//' -e 's/-g / /'`
fi

# MIGRATION (C→Python): Removed the block that folded the build CFLAGS into
# COB_CFLAGS (stripping optimization/debug options). COB_CFLAGS is removed
# under the Python backend (AAP 0.5.2); the build CFLAGS continue to apply to
# the cobc/cobcrun C driver build through the normal Automake mechanism.

# MIGRATION (C→Python): Removed the gcc cpucheck.c backend CPU-tuning block. It
# compiled and ran cpucheck.c with the COBOL backend compiler ($COB_CC) to
# derive CPU-specific tuning flags for generated C. The backend is no longer a
# C compiler, so this tuning is obsolete. (cpucheck.c is left in place but
# unused, per scope.) See AAP 0.5.2.

# Check gcc 4 option
MYOLDCFLAGS="$CFLAGS"
CFLAGS="$CFLAGS -Wno-pointer-sign"
AC_COMPILE_IFELSE([AC_LANG_PROGRAM([[]], [[int testint;]])],
	[AC_DEFINE(HAVE_PSIGN_OPT, 1)],
	[])
CFLAGS="$MYOLDCFLAGS"

# Check if aligned attribute works
AC_COMPILE_IFELSE([AC_LANG_PROGRAM([[]],
	[[char testchar[4] __attribute__((aligned));]])],
	[AC_DEFINE(HAVE_ATTRIBUTE_ALIGNED, 1)],
	[])

# Special stuff

AH_VERBATIM([_XOPEN_SOURCE_EXTENDED],
[/* Define to 1 if on HPUX.  */
#ifndef _XOPEN_SOURCE_EXTENDED
# undef _XOPEN_SOURCE_EXTENDED
#endif])dnl

COB_FIX_LIB="$COB_EXPORT_DYN"
case $host_os in
  mingw*)
	if test "$prefix" = "NONE"
	then
	COB_CONFIG_DIR="`cd /usr && pwd -W`/local/share/$PACKAGE_TARNAME/config"
	COB_COPY_DIR="`cd /usr && pwd -W`/local/share/$PACKAGE_TARNAME/copy"
	fi
	COB_EXPORT_DYN="-Wl,--export-all-symbols -Wl,--enable-auto-import"
	COB_FIX_LIB="$COB_EXPORT_DYN"
	;;
  cygwin*)
	COB_EXPORT_DYN="-Wl,--export-all-symbols -Wl,--enable-auto-import"
	COB_FIX_LIB="$COB_EXPORT_DYN"
	;;
  darwin* | rhapsody*)
	COB_SHARED_OPT="-bundle -flat_namespace -undefined suppress"
	;;
  hpux*)
	if test "`basename $CC`" != "gcc"
	then
		COB_SHARED_OPT="-b"
		AC_DEFINE([_XOPEN_SOURCE_EXTENDED],1)
		rm -f hptest*
		echo 'int main() { return 0; }' > hptest.c
		${CC} ${CFLAGS} +Olit=all -o hptest hptest.c > hptest.out 2>&1
		if test $? -ne 0 -o -s hptest.out
		then
			CFLAGS="$CFLAGS +ESlit"
			COB_CFLAGS="$COB_CFLAGS +ESlit -w"
		else
			CFLAGS="$CFLAGS +Olit=all"
			COB_CFLAGS="$COB_CFLAGS +Olit=all -w"
		fi
		rm -f hptest*
	fi
	;;
  aix*)
	COB_EXPORT_DYN="-Wl,-bexpfull"
	if test "`basename $CC`" != "gcc"
	then
		CFLAGS="$CFLAGS -O2 -Q -qro -qroconst"
		COB_SHARED_OPT="-G"
		COB_FIX_LIB=""
		# COB_FIX_LIB="-Wc,-G $COB_EXPORT_DYN"
		# COB_CFLAGS="$COB_CFLAGS -qchars=signed"
	else
		COB_FIX_LIB="$COB_EXPORT_DYN"
	fi
	;;
  solaris*)
	if test "`basename $CC`" != "gcc"
	then
		COB_SHARED_OPT="-G"
	fi
	;;
esac

if test "`basename $CC`" = "gcc"
then
	CFLAGS="$CFLAGS -fsigned-char -Wall -Wwrite-strings -Wmissing-prototypes -Wno-format-y2k"
fi

# MIGRATION (C→Python): Removed the lt_cv_dlopen_self → COB_NO_SELFOPEN test.
# Whether the C runtime could dlopen itself is irrelevant under the Python
# importlib loader (libcob_py.call); there is no self-dlopen requirement. See
# AAP 0.5.2.

# MIGRATION (C→Python): Removed the COB_HAS_ISAM computation. It reported
# whether any C ISAM/BDB handler was configured; all indexed I/O is now handled
# in pure Python by libcob_py.fileio (dbm/sqlite3), so the flag is obsolete and
# its AC_SUBST is also removed below. See AAP 0.5.2 / 0.6.3.

# Generate the output

AC_DEFINE_UNQUOTED(COB_EXPORT_DYN, "$COB_EXPORT_DYN")
AC_DEFINE_UNQUOTED(COB_PIC_FLAGS, "$COB_PIC_FLAGS")
AC_DEFINE_UNQUOTED(COB_SHARED_OPT, "$COB_SHARED_OPT")
AC_DEFINE_UNQUOTED(COB_EXEEXT, "$COB_EXEEXT")
AC_DEFINE_UNQUOTED(COB_EXTRA_FLAGS, "$COB_EXTRA_FLAGS")
if test "x$striplib" != "x"
then
  AC_DEFINE_UNQUOTED(COB_STRIP_CMD, "$striplib")
# AC_SUBST(COB_STRIP_CMD)
fi
# MIGRATION (C→Python): Substitute COB_PYTHON (the Python 3.11+ backend
# interpreter) in place of the former COB_CC (C backend compiler). The
# top-level Makefile.am emits @COB_PYTHON@ into defaults.h, and cobc.c reads it
# at compile time. COB_CFLAGS is removed entirely (no C compilation of
# generated code). See AAP 0.4.2 / 0.5.2 / 0.7.2.
AC_SUBST(COB_PYTHON)
AC_SUBST(COB_LDFLAGS)
AC_SUBST(COB_LIBS)
AC_SUBST(COB_CONFIG_DIR)
AC_SUBST(COB_COPY_DIR)
AC_SUBST(COB_LIBRARY_PATH)
AC_SUBST(COB_MODULE_EXT)
AC_SUBST(COBC_LIBS)
AC_SUBST(LIBCOB_CFLAGS)
AC_SUBST(LIBCOB_LIBS)
AC_SUBST(COB_EXPORT_DYN)
AC_SUBST(COB_PIC_FLAGS)
AC_SUBST(COB_SHARED_OPT)
COB_BIGENDIAN="$ac_cv_c_bigendian"
AC_SUBST(COB_BIGENDIAN)
AC_SUBST(COB_FIX_LIB)
AC_SUBST(COB_HAS_INLINE)
# MIGRATION (C→Python): Removed AC_SUBST(COB_HAS_ISAM) — the COB_HAS_ISAM flag
# was removed above (all indexed I/O is now handled by libcob_py.fileio via
# dbm/sqlite3). See AAP 0.5.2 / 0.6.3.

# MIGRATION (C→Python): Added libcob_py/Makefile so Automake recurses into the
# new pure-Python runtime package (libcob_py) added to the top-level Makefile.am
# SUBDIRS. It is placed adjacent to libcob/Makefile, the C runtime that is now
# retained only as a semantic reference. See AAP 0.2.1 / 0.4.1.
AC_CONFIG_FILES([bin/cob-config
		Makefile lib/Makefile libcob/Makefile libcob_py/Makefile
		cobc/Makefile bin/Makefile po/Makefile.in texi/Makefile config/Makefile
		copy/Makefile tests/atlocal tests/Makefile tests/cobol85/Makefile
		extras/Makefile])

AC_CONFIG_COMMANDS([chmod1], [chmod +x bin/cob-config])
AC_CONFIG_COMMANDS([chmod2], [chmod +x tests/atconfig])
AC_CONFIG_COMMANDS([chmod3], [chmod +x tests/atlocal])

# MIGRATION (C→Python): Build-time check that the libcob_py runtime is importable
# using only the Python standard library (AAP 0.7.2 stdlib-only requirement).
# We prepend $srcdir to sys.path so the in-tree, not-yet-installed package can
# be found. This is intentionally NON-fatal: libcob_py may not yet be present
# on sys.path during bootstrap, and the authoritative stdlib-only gate is the
# test suite. A failure is reported only as a warning. See AAP 0.5.1 / 0.7.2.
AC_MSG_CHECKING([for importable libcob_py (stdlib-only runtime)])
if $COB_PYTHON -c "import sys; sys.path.insert(0, '$srcdir'); import libcob_py" >/dev/null 2>&1
then
	AC_MSG_RESULT([yes])
else
	AC_MSG_RESULT([no (not yet importable; verified by the test suite)])
	AC_MSG_WARN([libcob_py is not importable from '$srcdir' yet; the stdlib-only runtime gate is enforced by the test suite.])
fi

AC_OUTPUT

echo ""
echo "GNU Cobol Configuration:"
echo ""
echo "  CC                   ${CC}"
# MIGRATION (C→Python): Report the Python 3.11+ backend (COB_PYTHON + detected
# version) in place of the former COB_CC / COB_CFLAGS lines. CC / CFLAGS are
# kept because they still build the cobc/cobcrun C driver binaries. See AAP
# 0.5.2 / 0.7.2.
echo "  COB_PYTHON           ${COB_PYTHON}"
echo "  Python version       ${cob_python_version}"
echo "  CFLAGS               ${CFLAGS}"
echo "  COB_EXTRA_FLAGS      ${COB_EXTRA_FLAGS}"
echo "  LDFLAGS              ${LDFLAGS}"
echo "  COB_LDFLAGS          ${COB_LDFLAGS}"
if test "x$COBC_LIBS" != "x"
then
  echo "  COBC_LIBS            ${COBC_LIBS}"
fi
echo "  COB_LIBS             ${COB_LIBS}"
echo "  COB_CONFIG_DIR       ${COB_CONFIG_DIR}"
echo "  COB_COPY_DIR         ${COB_COPY_DIR}"
echo "  COB_LIBRARY_PATH     ${COB_LIBRARY_PATH}"
echo "  COB_MODULE_EXT       ${COB_MODULE_EXT}"
echo "  COB_SHARED_OPT       ${COB_SHARED_OPT}"
echo "  COB_PIC_FLAGS        ${COB_PIC_FLAGS}"
echo "  COB_EXPORT_DYN       ${COB_EXPORT_DYN}"
if test "x$striplib" != "x"
then
  echo "  COB_STRIP_CMD        ${striplib}"
fi
# MIGRATION (C→Python): Dynamic loading is now performed by libcob_py.call via
# Python importlib + sys.path; the former DEFINE_DL System/Libtool distinction
# no longer applies. See AAP 0.3.2 / 0.5.2.
echo "  Dynamic loading      Python importlib"
echo ""
echo "  Use gettext for international messages:      ${USE_NLS}"
# MIGRATION (C→Python): Removed the Berkeley DB / external ISAM handler summary
# (with_db/with_cisam/with_disam/with_vbisam/with_index_extfh/with_seqra_extfh)
# and the ncurses/pdcurses/curses screen-I/O summary. Indexed/sequential I/O is
# now provided by libcob_py.fileio (Python stdlib dbm/sqlite3) and screen I/O by
# libcob_py.screenio (Python stdlib curses). See AAP 0.5.2 / 0.6.3 / 0.3.4.
echo "  Indexed/sequential I/O:                      Python (libcob_py.fileio: dbm/sqlite3)"
echo "  Use fcntl for file locking:                  ${ac_cv_func_fcntl}"
echo "  Screen I/O:                                  Python (libcob_py.screenio: curses)"
echo ""
