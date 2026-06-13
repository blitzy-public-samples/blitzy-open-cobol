#
# Makefile gnucobol/bin
# Process this file with automake to produce Makefile.in
#
# Copyright (C) 2004-2011 Roger While
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

bin_SCRIPTS=cob-config
bin_PROGRAMS = cobcrun
cobcrun_SOURCES = cobcrun.c

# MIGRATION (C→Python): COB_EXPORT_DYN no longer required (cobcrun does not dlopen); retained as harmless
AM_LDFLAGS = $(COB_EXPORT_DYN)
cobcrun_CFLAGS = $(AM_CFLAGS) -I$(top_srcdir)
# MIGRATION (C→Python): dropped `cobcrun_LDADD = $(top_builddir)/libcob/libcob.la`.
# cobcrun.c no longer calls cob_init/cob_resolve/cob_stop_run; it spawns the
# CPython interpreter on the named module instead, so it must not link the C
# runtime libcob.la (which is no longer built — see libcob/Makefile.am).
