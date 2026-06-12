/*
   Copyright (C) 2004-2010 Roger While

   This file is part of GNU Cobol.

   The GNU Cobol compiler is free software; you can redistribute it
   and/or modify it under the terms of the GNU General Public License
   as published by the Free Software Foundation; either version 2 of the
   License, or (at your option) any later version.

   GNU Cobol is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License for more details.

   You should have received a copy of the GNU General Public License
   along with GNU Cobol; see the file COPYING. If not, write to
   the Free Software Foundation, 51 Franklin Street, Fifth Floor,
   Boston, MA 02110-1301 USA
*/


#include	"config.h"
#include	"defaults.h"

#include	<stdio.h>
#include	<string.h>
#include	<stdlib.h>	/* MIGRATION (C→Python): getenv() for COB_PYTHON */
#include	<unistd.h>	/* MIGRATION (C→Python): execvp() to spawn the interpreter */
/* MIGRATION (C→Python): removed #include "libcob.h". cobcrun no longer links
   the C runtime (cob_init/cob_resolve/cob_call_error/cob_stop_run are gone),
   and libcob.h pulls in <gmp.h>, which is removed under the CPython backend. */
#include	"tarstamp.h"

#ifdef	HAVE_KPATHSEA_GETOPT_H
#include <kpathsea/getopt.h>
#else
#ifdef	HAVE_GETOPT_H
#include <getopt.h>
#else
#include "lib/getopt.h"
#endif
#endif

#ifdef	HAVE_LOCALE_H
#include <locale.h>
#endif

static const char short_options[] = "hV";

static const struct option long_options[] = {
	{"help", no_argument, NULL, 'h'},
	{"version", no_argument, NULL, 'V'},
	{NULL, 0, NULL, 0}
};

static void
cobcrun_print_version (void)
{
	int	year;
	int	day;
	char	buff[64];
	char	month[64];

	memset (buff, 0, sizeof(buff));
	memset (month, 0, sizeof(month));
	day = 0;
	year = 0;
	sscanf (__DATE__, "%s %d %d", month, &day, &year);
	if (day && year) {
		sprintf (buff, "%s %2.2d %4.4d %s", month, day, year, __TIME__);
	} else {
		sprintf (buff, "%s %s", __DATE__, __TIME__);
	}
	printf ("cobcrun (%s) %s.%d\n",
		PACKAGE_NAME, PACKAGE_VERSION, PATCH_LEVEL);
	puts ("Copyright (C) 2004-2009 Roger While");
	printf ("Built    %s\nPackaged %s\n", buff, COB_TAR_DATE);
}

static void
cobcrun_print_usage (void)
{
	printf ("Usage: cobcrun PROGRAM [param ...]");
	printf ("\n\n");
	printf ("or   : cobcrun --help");
	printf ("\n");
	printf ("       Display this message");
	printf ("\n\n");
	printf ("or   : cobcrun --version, -V");
	printf ("\n");
	printf ("       Display runtime version");
	printf ("\n\n");
}

static int
process_command_line (int argc, char *argv[])
{
	int			c, idx;

	/* At least one option or module name needed */
	if (argc <= 1) {
		cobcrun_print_usage ();
		return 1;
	}

	/* Translate first command line argument from WIN to UNIX style */
	if (strrchr(argv[1], '/') == argv[1]) {
		argv[1][0] = '-';
	}

	/* Process first command line argument only if not a module */
	if (argv[1][0] != '-') {
		return 99;
	}

	c = getopt_long_only (argc, argv, short_options, long_options, &idx);
	if (c > 0) {
		switch (c) {
		case '?':
			return 1;
		case 'h':
			cobcrun_print_usage ();
			return 0;
		case 'V':
			cobcrun_print_version ();
			return 0;
		}
	}

	return 99;
}

int
main (int argc, char **argv)
{
	int pcl_return;
	const char *cob_python;	/* MIGRATION (C→Python): CPython interpreter to spawn */
	
#ifdef	HAVE_SETLOCALE
	setlocale (LC_ALL, "");
#endif

	pcl_return = process_command_line (argc, argv);

	if (pcl_return != 99) {
		return pcl_return;
	}

	if (strlen (argv[1]) > 31) {
		fprintf (stderr, "Invalid PROGRAM name\n");
		return 1;
	}
	/* MIGRATION (C→Python): the COBOL program is now a Python module emitted by
	   cobc, not a native object resolved via cob_resolve/dlopen. Instead of
	   cob_init + cob_resolve + native call + cob_stop_run, spawn the configured
	   CPython interpreter on the named program and let it run. */

	/* MIGRATION (C→Python): interpreter = $COB_PYTHON if set, else "python3".
	   COB_PYTHON is the env var that supersedes COB_CC/COB_CFLAGS and is the
	   same name cobc.c reads, keeping the backend interpreter selection in
	   lockstep across the driver and the runner. */
	cob_python = getenv ("COB_PYTHON");
	if (cob_python == NULL || cob_python[0] == '\0') {
		cob_python = "python3";
	}

	/* MIGRATION (C→Python): build the child argument vector by reusing argv.
	   Original argv = { "cobcrun", <program>, <param>... , NULL }. Overwriting
	   argv[0] with the interpreter yields { <python>, <program>, <param>... , NULL }
	   i.e. `python <program> [param ...]`. argv[argc] is guaranteed NULL by the C
	   standard, so argv is already a valid NULL-terminated vector for execvp. */
	argv[0] = (char *) cob_python;

	/* MIGRATION (C→Python): replace the process image so the Python program's exit
	   status becomes cobcrun's exit status (preserves the cob_stop_run(ret)
	   exit-status-propagation contract). execv* is preferred over system() for
	   exit-status fidelity per the bin/ requirements. */
	execvp (cob_python, argv);

	/* MIGRATION (C→Python): execvp only returns on failure (e.g. interpreter not
	   found). Emit a fatal diagnostic naming the interpreter and COB_PYTHON, then
	   exit non-zero — mirroring AAP §0.7.2's invocation-failure contract. */
	fprintf (stderr,
		 "cobcrun: Python interpreter '%s' not found. Set COB_PYTHON.\n",
		 cob_python);
	return 1;
}
