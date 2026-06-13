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
#include	<limits.h>	/* MIGRATION (C->Python): PATH_MAX for the ".pyz" module-path search */
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

	/* MIGRATION (C->Python): a COBOL program compiled with "cobc -m" is now a
	   self-contained "<PROGRAM><COB_MODULE_EXT>" (e.g. "caller.pyz") zip archive
	   that bundles BOTH the emitted program module AND the libcob_py runtime
	   package (see cobc_build_pyz in cobc/cobc.c).  The original C cobcrun did
	   cob_init + cob_resolve (dlopen/dlsym) + native call + cob_stop_run; here we
	   reproduce that sequence faithfully by re-invoking the interpreter as
	       <python> -m libcob_py <program> [param ...]
	   with the archive on PYTHONPATH.  The module-mode launcher
	   libcob_py/__main__.py then runs cob_init -> cob_resolve -> call ->
	   cob_stop_run from INSIDE the archive (zipimport), and EXTERNAL storage is
	   shared across the run unit because every imported module resolves the SAME
	   bundled libcob_py instance (just as the single C libcob.so shared EXTERNAL
	   storage across dlopen'd modules). */
	{
		const char	*module = argv[1];
		const char	*libpath_env;
		const char	*old_pythonpath;
		const char	*libpy_dir;
		char		candidate[PATH_MAX];
		char		*pyz_path = NULL;
		char		*libpy_parent = NULL;
		char		*new_pythonpath;
		char		**new_argv;
		size_t		len;
		int		i;

		/* (1) Locate "<module><COB_MODULE_EXT>" on the same resolve paths the C
		   resolver scanned (libcob/call.c resolve_path[]): the current directory
		   first, then each COB_LIBRARY_PATH entry.  The first hit is the
		   self-contained archive to place (first) on the child PYTHONPATH. */
		snprintf (candidate, sizeof (candidate), "./%s%s", module, COB_MODULE_EXT);
		if (access (candidate, R_OK) == 0) {
			pyz_path = strdup (candidate);
		}
		libpath_env = getenv ("COB_LIBRARY_PATH");
		if (pyz_path == NULL && libpath_env != NULL && libpath_env[0] != '\0') {
			char	*search = strdup (libpath_env);
			if (search != NULL) {
				char	*dir = strtok (search, ":");
				while (dir != NULL) {
					snprintf (candidate, sizeof (candidate),
						  "%s/%s%s", dir, module, COB_MODULE_EXT);
					if (access (candidate, R_OK) == 0) {
						pyz_path = strdup (candidate);
						break;
					}
					dir = strtok (NULL, ":");
				}
				free (search);
			}
		}

		/* (2) Parent directory of the libcob_py runtime package ($COB_LIBPY_DIR
		   if set, else the COB_LIBPY_DIR macro from defaults.h) so that
		   "-m libcob_py" / "import libcob_py" still resolves to the installed
		   runtime as a fallback when no bundled ".pyz" was found. */
		libpy_dir = getenv ("COB_LIBPY_DIR");
		if (libpy_dir == NULL || libpy_dir[0] == '\0') {
			libpy_dir = COB_LIBPY_DIR;
		}
		if (libpy_dir != NULL && libpy_dir[0] != '\0') {
			libpy_parent = strdup (libpy_dir);
			if (libpy_parent != NULL) {
				char	*slash = strrchr (libpy_parent, '/');
				if (slash != NULL) {
					*slash = '\0';		/* strip "/libcob_py" -> its parent dir */
				} else {
					libpy_parent[0] = '.';	/* bare name: parent is "." */
					libpy_parent[1] = '\0';
				}
			}
		}

		/* (3) Child PYTHONPATH = "<pyz>:<libpy_parent>:<old PYTHONPATH>".  The
		   ".pyz" is first so its bundled libcob_py (with the launcher) and the
		   program module import from inside the archive; libpy_parent is the
		   installed-runtime fallback; any pre-existing PYTHONPATH is preserved. */
		old_pythonpath = getenv ("PYTHONPATH");
		len = 1;
		if (pyz_path != NULL) {
			len += strlen (pyz_path) + 1;
		}
		if (libpy_parent != NULL) {
			len += strlen (libpy_parent) + 1;
		}
		if (old_pythonpath != NULL) {
			len += strlen (old_pythonpath) + 1;
		}
		new_pythonpath = malloc (len);
		if (new_pythonpath != NULL) {
			new_pythonpath[0] = '\0';
			if (pyz_path != NULL) {
				strcat (new_pythonpath, pyz_path);
			}
			if (libpy_parent != NULL) {
				if (new_pythonpath[0] != '\0') {
					strcat (new_pythonpath, ":");
				}
				strcat (new_pythonpath, libpy_parent);
			}
			if (old_pythonpath != NULL && old_pythonpath[0] != '\0') {
				if (new_pythonpath[0] != '\0') {
					strcat (new_pythonpath, ":");
				}
				strcat (new_pythonpath, old_pythonpath);
			}
			setenv ("PYTHONPATH", new_pythonpath, 1);
		}

		/* (4) Build the child argv: { <python>, "-m", "libcob_py", <module>,
		   <param>..., NULL }.  argv[1..argc-1] (the module name and its
		   parameters) shift to new_argv[3..]; argv[argc] is NULL per the C
		   standard, so the copied vector is NULL-terminated. */
		new_argv = malloc ((size_t) (argc + 3) * sizeof (char *));
		if (new_argv == NULL) {
			fprintf (stderr, "cobcrun: out of memory\n");
			return 1;
		}
		new_argv[0] = (char *) cob_python;
		new_argv[1] = (char *) "-m";
		new_argv[2] = (char *) "libcob_py";
		for (i = 1; i < argc; i++) {
			new_argv[i + 2] = argv[i];
		}
		new_argv[argc + 2] = NULL;

		/* (5) Replace the process image so the launcher's cob_stop_run(ret) exit
		   status becomes cobcrun's exit status (preserves the native
		   exit-status-propagation contract; execv* over system() for fidelity). */
		execvp (cob_python, new_argv);
	}

	/* MIGRATION (C→Python): execvp only returns on failure (e.g. interpreter not
	   found). Emit a fatal diagnostic naming the interpreter and COB_PYTHON, then
	   exit non-zero — mirroring AAP §0.7.2's invocation-failure contract. */
	fprintf (stderr,
		 "cobcrun: Python interpreter '%s' not found. Set COB_PYTHON.\n",
		 cob_python);
	return 1;
}
