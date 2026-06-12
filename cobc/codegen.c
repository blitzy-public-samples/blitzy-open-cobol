/*
   Copyright (C) 2001,2002,2003,2004,2005,2006,2007 Keisuke Nishida
   Copyright (C) 2007-2010 Roger While

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

/*
   ============================================================================
   MIGRATION (C -> Python backend)
   ============================================================================
   This code generator has been re-targeted from emitting C source text to
   emitting Python 3.11+ source text that targets the "libcob_py" runtime
   package, instead of C source text that targeted "libcob.h".

   Contract summary:
     * One self-contained ".py" module is emitted per COBOL compilation unit
       (the former three-stream model -- ".c" + storage ".h" + local ".l.h" --
       is collapsed into a single Python module stream).
     * Every emitted "cob_*" call-site is emitted as a module-qualified
       "libcob_py.<module>.<fn>(...)" call.  The module is resolved by
       codegen_pymod() below, following the libcob source file each routine
       lives in (common/numeric/move/strings/intrinsic/fileio/call/screenio/
       termio/system).  Names whose module cannot be determined are routed
       through the "libcob_py" facade so no symbol is ever emitted bare.
     * Numeric byte-layout parity with the former C runtime is preserved: the
       immutable front-end field layout (field.c) is consumed unchanged and the
       identical cob_field_attr (type/digits/scale/flags/pic) and data
       offsets/sizes are emitted, so the libcob_py runtime can reproduce the
       same bytes for every USAGE/PICTURE.
     * Data storage becomes a Python "bytearray" backing store; a data
       reference becomes a "memoryview(b_N)[offset:]" sharing that buffer
       (mutable, byte-exact).  Fields/attrs become common.cob_field(...) /
       common.cob_field_attr(...) constructor calls.
     * Procedure-division control flow (paragraphs/sections, GO TO and PERFORM)
       is reproduced with a recursive segmented dispatch model -- see the
       MIGRATION note on output_internal_function().

   IMMUTABLE IDENTITY: this file REMAINS a C source compiled into the "cobc"
   binary (it is NOT itself Python).  The public entry point
   "void codegen (struct cb_program *prog, const int nested)" and the
   "#include \"libcob/system.def\"" system-routine table are preserved.
   ============================================================================
*/


#include "config.h"

#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <ctype.h>
#include <time.h>

#include "tarstamp.h"

#include "cobc.h"
#include "tree.h"

#ifdef	HAVE_ATTRIBUTE_ALIGNED
#define COB_ALIGN " __attribute__((aligned))"
#else
#define COB_ALIGN ""
#endif

#define COB_USE_SETJMP		0
#define COB_MAX_SUBSCRIPTS	16

/* MIGRATION (C -> Python): Python uses indentation (not braces) to delimit
   blocks, so every nesting level must add a UNIFORM number of leading spaces.
   COB_PY_INDENT is that fixed unit; output_block_open()/output_block_close()
   and the reworked output_indent() are the only places that change the
   emitted indentation level. */
#define COB_PY_INDENT		4

#define INITIALIZE_NONE		0
#define INITIALIZE_ONE		1
#define INITIALIZE_DEFAULT	2
#define INITIALIZE_COMPOUND	3
#define INITIALIZE_EXTERNAL	4

#ifndef __GNUC__
static int			inside_check = 0;
static int			inside_stack[64];
#endif
static int			param_id = 0;
static int			stack_id = 0;
static int			num_cob_fields = 0;
static int			loop_counter = 0;
static int			progid = 0;
/* MIGRATION (C->Python): set when a main program is generated so codegen()
   can emit the "if __name__ == \"__main__\":" trigger AFTER the module data is
   flushed (when every referenced name already exists). */
static int			gen_main_trigger = 0;
static int			last_line = 0;
static int			needs_exit_prog = 0;
/* MIGRATION (C -> Python): the former "need_double" double-cast flag was dead
   (always 0) and its only consumers in output_integer() have been removed. */
static int			gen_ebcdic = 0;
static int			gen_ebcdic_ascii = 0;
static int			gen_full_ebcdic = 0;
static int			gen_native = 0;
static int			gen_custom = 0;
static int			field_iteration = 0;
static int			screenptr = 0;

static int			i_counters[COB_MAX_SUBSCRIPTS];

static int			output_indent_level = 0;
static FILE			*output_target;

/* MIGRATION (C -> Python): empty-suite tracking.  Python forbids an empty
   block body -- a "def f():", "if x:", "for i in ...:", etc. MUST be followed
   by at least one indented statement, otherwise the source is a SyntaxError.
   The C backend had no such rule (an empty "{}" or a lone ";" was legal).  We
   therefore track, for each currently-open Python block, whether any body
   content has been emitted into it.  When a block is closed with no content,
   output_block_close() emits a single "pass" statement so the suite is valid.
   block_content_stack[d] is 1 once the block at depth d has received a line;
   block_depth is the count of currently-open blocks. */
#define COB_PY_MAX_BLOCK_DEPTH	256
static int			block_content_stack[COB_PY_MAX_BLOCK_DEPTH];
static int			block_depth = 0;

/* MIGRATION (C -> Python): control-flow segmentation state.  The C backend
   emitted procedure code as a flat instruction stream with C labels "l_N:"
   and goto.  Python has neither labels nor goto, so the procedure body is
   emitted as a recursive dispatch function _dispatch(_pc, _through) whose
   body is a sequence of "if _pc == <label-id>:" segments.  output_segment_open
   tracks whether a segment block is currently open so the next need_begin
   LABEL can first emit the fall-through ("_pc = <id>; continue") and close the
   prior segment before opening its own.  The dispatch skeleton, the _CobGoto /
   _CobExit / _CobPerformExit helper exceptions, and the program-exit cleanup
   are emitted by the function wrapper (output_internal_function). */
static int			output_segment_open = 0;

/* MIGRATION (C -> Python): single-module assembly buffers.
   The former C backend wrote THREE streams per compilation unit: the ".c"
   body (yyout), a storage header ".h" (cb_storage_file) and a per-program
   local header ".l.h" (local_storage_file).  The Python backend emits ONE
   self-contained ".py" module, so the storage and local declarations are
   accumulated into these in-memory streams during traversal and flushed into
   the single module stream (yyout) at finalization.  open_memstream() keeps
   the backing buffer/length updated on fflush; the separate cb_storage_file /
   local_storage_file temp files opened by cobc.c are simply left empty and
   removed by the existing cleanup there. */
static FILE			*storage_mem = NULL;
static char			*storage_mem_buf = NULL;
static size_t			storage_mem_len = 0;
static FILE			*local_mem = NULL;
static char			*local_mem_buf = NULL;
static size_t			local_mem_len = 0;

static const char		*excp_current_program_id = NULL;
static const char		*excp_current_section = NULL;
static const char		*excp_current_paragraph = NULL;
static struct cb_program	*current_prog;

static struct label_list {
	struct label_list	*next;
	int			id;
	int			call_num;
} *label_cache = NULL;

static struct attr_list {
	struct attr_list	*next;
	unsigned char		*pic;
	int			id;
	int			type;
	int			digits;
	int			scale;
	int			flags;
	int			lenstr;
} *attr_cache = NULL;

static struct literal_list {
	struct literal_list	*next;
	struct cb_literal	*literal;
	cb_tree			x;
	int			id;
} *literal_cache = NULL;

static struct field_list {
	struct field_list	*next;
	struct cb_field		*f;
	cb_tree			x;
	const char		*curr_prog;
	int			nulldata;
} *field_cache = NULL;

static struct call_list {
	struct call_list	*next;
	const char		*callname;
} *call_cache = NULL;

static struct base_list {
	struct base_list	*next;
	struct cb_field		*f;
	const char		*curr_prog;
} *base_cache = NULL;

static struct local_list {
	struct local_list	*next;
	struct cb_field		*f;
} *local_cache = NULL;

struct sort_list {
	struct sort_list	*next;
};

struct system_table {
	const char		*syst_name;
	const char		*syst_call;
};

static const struct system_table	system_tab[] = {
#undef	COB_SYSTEM_GEN
#define	COB_SYSTEM_GEN(x, y, z)	{ x, #z },
#include "libcob/system.def"
	{ NULL, NULL }
};

/* Globals */
int				has_external = 0;

#ifdef __GNUC__
static void output (const char *fmt, ...)
    __attribute__ ((__format__ (__printf__, 1, 2)));
static void output_line (const char *fmt, ...)
    __attribute__ ((__format__ (__printf__, 1, 2)));
static void output_storage (const char *fmt, ...)
    __attribute__ ((__format__ (__printf__, 1, 2)));
#else
static void output (const char *fmt, ...);
static void output_line (const char *fmt, ...);
static void output_storage (const char *fmt, ...);
#endif

static void output_stmt (cb_tree x);
static void output_integer (cb_tree x);
static void output_index (cb_tree x);
static void output_func_1 (const char *name, cb_tree x);
static void output_param (cb_tree x, int id);
/* MIGRATION (C -> Python): output_param now emits runtime bounds-check calls
   inline (as Python tuple elements), so it forward-references output_funcall,
   which is defined later in the file. */
static void output_funcall (cb_tree x);

static void
lookup_call (const char *p)
{
	struct call_list *clp;

	for (clp = call_cache; clp; clp = clp->next) {
		if (strcmp (p, clp->callname) == 0) {
			return;
		}
	}
	clp = cobc_malloc (sizeof (struct call_list));
	clp->callname = p;
	clp->next = call_cache;
	call_cache = clp;
}

static struct attr_list *
attr_list_reverse (struct attr_list *p)
{
	struct attr_list	*next;
	struct attr_list	*last = NULL;

	for (; p; p = next) {
		next = p->next;
		p->next = last;
		last = p;
	}
	return last;
}

static struct literal_list *
literal_list_reverse (struct literal_list *p)
{
	struct literal_list	*next;
	struct literal_list	*last = NULL;

	for (; p; p = next) {
		next = p->next;
		p->next = last;
		last = p;
	}
	return last;
}

static struct local_list *
local_list_reverse (struct local_list *p)
{
	struct local_list	*next;
	struct local_list	*last = NULL;

	for (; p; p = next) {
		next = p->next;
		p->next = last;
		last = p;
	}
	return last;
}

/*
 * Output routines
 */

static void
output (const char *fmt, ...)
{
	va_list		ap;

	if (output_target) {
		va_start (ap, fmt);
		vfprintf (output_target, fmt, ap);
		va_end (ap);
	}
}

static void
output_newline (void)
{
	if (output_target) {
		fputs ("\n", output_target);
	}
}

/* MIGRATION (C -> Python): mark the innermost currently-open Python block as
   having received body content, so output_block_close() will NOT inject a
   spurious "pass".  Called from output_prefix(), which begins every indented
   statement line (directly or via output_line()).  Tracking is independent of
   output_target so the logical block structure stays consistent even when no
   bytes are being written. */
static void
output_mark_content (void)
{
	if (block_depth > 0 && block_depth <= COB_PY_MAX_BLOCK_DEPTH) {
		block_content_stack[block_depth - 1] = 1;
	}
}

static void
output_prefix (void)
{
	int	i;

	/* MIGRATION (C -> Python): any prefixed line counts as body content for
	   the enclosing Python suite. */
	output_mark_content ();
	if (output_target) {
		for (i = 0; i < output_indent_level; i++) {
			fputc (' ', output_target);
		}
	}
}

static void
output_line (const char *fmt, ...)
{
	va_list		ap;

	if (output_target) {
		output_prefix ();
		va_start (ap, fmt);
		vfprintf (output_target, fmt, ap);
		va_end (ap);
		fputc ('\n', output_target);
	}
}

/* MIGRATION (C -> Python): emit an indented Python comment line "# ...".
   CRITICAL: unlike output_line(), this deliberately does NOT mark the
   enclosing block as having body content.  A Python comment is NOT a
   statement and cannot satisfy a block suite -- "if x:\n    # c" is a
   SyntaxError.  By not marking content, output_block_close() still injects a
   "pass" for a block that contains only comments, keeping the suite valid.
   The indentation spaces are written directly here (mirroring output_prefix
   without the content-marking side effect). */
static void
output_comment (const char *fmt, ...)
{
	va_list		ap;
	int		i;

	if (output_target) {
		for (i = 0; i < output_indent_level; i++) {
			fputc (' ', output_target);
		}
		fputs ("# ", output_target);
		va_start (ap, fmt);
		vfprintf (output_target, fmt, ap);
		va_end (ap);
		fputc ('\n', output_target);
	}
}

/* MIGRATION (C -> Python): open a Python block.  Unlike the C emitter, no
   "{" token is written -- the trailing ":" that introduces the block is
   emitted by the caller (e.g. "if cond:") and the body that follows is simply
   indented one COB_PY_INDENT unit deeper.  Emits nothing itself. */
static void
output_block_open (void)
{
	/* Opening a child block means the parent suite is non-empty. */
	output_mark_content ();
	output_indent_level += COB_PY_INDENT;
	/* Push a fresh, empty content slot for the newly opened block. */
	if (block_depth < COB_PY_MAX_BLOCK_DEPTH) {
		block_content_stack[block_depth] = 0;
	}
	block_depth++;
}

/* MIGRATION (C -> Python): close a Python block.  No "}" token is written;
   the block ends purely by dedenting.  If the block being closed never
   received any body content, a lone "pass" is emitted first so the Python
   suite is syntactically valid (Python forbids empty blocks). */
static void
output_block_close (void)
{
	int	had_content;

	if (block_depth > 0) {
		block_depth--;
		had_content = (block_depth < COB_PY_MAX_BLOCK_DEPTH)
				? block_content_stack[block_depth] : 1;
		if (!had_content) {
			/* Emit "pass" at the (still-indented) body level.  This
			   output_line() call re-enters output_prefix(), which marks
			   the PARENT block as non-empty -- correct, since a child
			   block (even an empty one) is parent content. */
			output_line ("pass");
		}
	}
	output_indent_level -= COB_PY_INDENT;
	if (output_indent_level < 0) {
		output_indent_level = 0;
	}
}

/* MIGRATION (C -> Python): the legacy output_indent() brace-shim has been
   removed now that every block delimiter is expressed directly through
   output_block_open()/output_block_close().  The Python indentation level is
   tracked by output_indent_level and advanced one uniform COB_PY_INDENT per
   nesting step. */

/* MIGRATION (C -> Python): emit a Python bytes literal b"..." in place of a C
   string literal.  COBOL data is raw bytes, so a Python bytes literal is the
   byte-exact representation and the safe choice for round-trip fidelity.
   Printable ASCII bytes are emitted verbatim (with '"' and '\\' escaped); all
   other bytes are emitted as \xNN hex escapes.  Python consumes exactly two
   hex digits after \x, so a hex escape immediately followed by literal text is
   unambiguous (e.g. b"\x41A" is the two bytes 0x41,0x41). */
static void
output_string (const unsigned char *s, int size)
{
	int	i;
	int	c;

	output ("b\"");
	for (i = 0; i < size; i++) {
		c = s[i];
		if (isprint (c) && c != '\"' && c != '\\') {
			output ("%c", c);
		} else {
			output ("\\x%02x", c);
		}
	}
	output ("\"");
}

/* MIGRATION (C -> Python): lazily open the in-memory storage stream. */
static void
output_storage_open (void)
{
	if (!storage_mem) {
		storage_mem = open_memstream (&storage_mem_buf, &storage_mem_len);
	}
}

/* MIGRATION (C -> Python): lazily open the in-memory local-declaration
   stream. */
static void
output_local_open (void)
{
	if (!local_mem) {
		local_mem = open_memstream (&local_mem_buf, &local_mem_len);
	}
}

/* MIGRATION (C -> Python): storage (the former ".h") now accumulates into an
   in-memory stream that is flushed into the single module at finalization,
   instead of a separate storage header file. */
static void
output_storage (const char *fmt, ...)
{
	va_list		ap;

	output_storage_open ();
	if (storage_mem) {
		va_start (ap, fmt);
		vfprintf (storage_mem, fmt, ap);
		va_end (ap);
	}
}

/* MIGRATION (C -> Python): local declarations (the former ".l.h") now
   accumulate into an in-memory stream that is flushed into the single module
   at finalization, instead of a separate per-program local header file. */
static void
output_local (const char *fmt, ...)
{
	va_list		ap;

	output_local_open ();
	if (local_mem) {
		va_start (ap, fmt);
		vfprintf (local_mem, fmt, ap);
		va_end (ap);
	}
}

/* MIGRATION (C -> Python): flush the in-memory storage + local streams into
   the single module stream (yyout) and release them.  Called once at the end
   of codegen() (outermost finalization).  ORDER MATTERS for Python: the storage
   data (b_N bytearrays, a_N attributes, f_N/c_N cob_field objects, collating
   tables) is emitted FIRST because the local declarations that follow reference
   it at module-load time -- the alphabet cob_field uses an a_N attribute and the
   screen objects use f_N fields.  Both buffers are written verbatim regardless
   of the current output_target so the single module is self-contained.  The
   program-body functions were already written directly to yyout above; they
   reference this module-level data only at call time, so emitting the data
   after the function defs is safe (the __main__ trigger is emitted after this
   flush, once every name exists). */
static void
output_flush_module_buffers (void)
{
	if (storage_mem) {
		fflush (storage_mem);
		if (yyout && storage_mem_buf && storage_mem_len) {
			fwrite (storage_mem_buf, 1, storage_mem_len, yyout);
		}
		fclose (storage_mem);
		free (storage_mem_buf);
		storage_mem = NULL;
		storage_mem_buf = NULL;
		storage_mem_len = 0;
	}
	if (local_mem) {
		fflush (local_mem);
		if (yyout && local_mem_buf && local_mem_len) {
			fwrite (local_mem_buf, 1, local_mem_len, yyout);
		}
		fclose (local_mem);
		free (local_mem_buf);
		local_mem = NULL;
		local_mem_buf = NULL;
		local_mem_len = 0;
	}
}

/*
 * MIGRATION (C -> Python): runtime-symbol module resolver.
 *
 * The former C backend emitted every runtime entry point as a bare C
 * identifier (e.g. "cob_move (...)") resolved at link time against the single
 * libcob shared object.  The Python backend instead emits each runtime symbol
 * module-qualified against the libcob_py package (e.g. "move.cob_move(...)").
 *
 * codegen_pymod() maps a "cob_*" runtime symbol to the libcob_py submodule
 * that provides it.  The assignment follows the libcob source file each
 * routine lives in (AAP 0.4.1 / 0.6.5), verified directly against the
 * libcob C sources:
 *
 *   common.c    -> "common"    numeric.c  -> "numeric"   move.c    -> "move"
 *   strings.c   -> "strings"   intrinsic.c-> "intrinsic" fileio.c  -> "fileio"
 *   call.c      -> "call"      screenio.c -> "screenio"  termio.c  -> "termio"
 *   system.def  -> "system"    codegen.h inline helpers  -> "numeric"/"common"
 *
 * The checks are ordered most-specific-first.  Any symbol that cannot be
 * classified is routed through the "libcob_py" facade (libcob_py/__init__.py
 * re-exports every cob_* name), so NO symbol is ever emitted bare/undefined.
 */
static const char *
codegen_pymod (const char *name)
{
	/* 0. Runtime globals/objects (attribute access, not calls) live on the
	      common module.  Must precede the cob_call_ prefix rule below so that
	      cob_call_params is classified here and not as a call.c routine. */
	if (!strcmp (name, "cob_exception_code") ||
	    !strcmp (name, "cob_initialized") ||
	    !strcmp (name, "cob_current_module") ||
	    !strcmp (name, "cob_call_params") ||
	    !strcmp (name, "cob_save_call_params") ||
	    !strcmp (name, "cob_user_parameters") ||
	    !strcmp (name, "cob_procedure_parameters")) {
		return "common";
	}

	/* 1. The ~128 fixed-width binary helpers from libcob/codegen.h all end in
	      "_binary" and belong to the numeric subsystem. */
	{
		size_t	len = strlen (name);
		if (len > 7 && !strcmp (name + len - 7, "_binary")) {
			return "numeric";
		}
	}

	/* 2. Packed-decimal helpers (numeric). */
	if (strstr (name, "_packed") != NULL) {
		return "numeric";
	}

	/* 3. Decimal arithmetic family (numeric). */
	if (!strncmp (name, "cob_decimal", 11)) {
		return "numeric";
	}

	/* 4. Named arithmetic helpers add/sub/mul/div (numeric). */
	if (!strncmp (name, "cob_add", 7) ||
	    !strncmp (name, "cob_sub", 7) ||
	    !strncmp (name, "cob_mul", 7) ||
	    !strncmp (name, "cob_div", 7)) {
		return "numeric";
	}

	/* 5. Specific compare helpers (numeric).  Note the trailing underscore:
	      the generic "cob_cmp" (no suffix) lives in common.c and is handled by
	      the default branch below. */
	if (!strncmp (name, "cob_cmp_", 8)) {
		return "numeric";
	}

	/* 6. INSPECT / STRING / UNSTRING (strings). */
	if (!strncmp (name, "cob_inspect_", 12) ||
	    !strncmp (name, "cob_string_", 11) ||
	    !strncmp (name, "cob_unstring_", 13)) {
		return "strings";
	}

	/* 7. Intrinsic FUNCTIONs (intrinsic). */
	if (!strncmp (name, "cob_intr_", 9)) {
		return "intrinsic";
	}

	/* 8. SCREEN SECTION I/O (screenio). */
	if (!strncmp (name, "cob_screen_", 11) ||
	    !strcmp (name, "cob_field_display") ||
	    !strcmp (name, "cob_field_accept")) {
		return "screenio";
	}

	/* 9. Data-movement family (move) -- the GAP module (AAP 0.6.5). */
	if (!strcmp (name, "cob_move") ||
	    !strcmp (name, "cob_set_int") ||
	    !strcmp (name, "cob_get_int")) {
		return "move";
	}

	/* 10. File I/O, SORT/MERGE, file status (fileio). */
	if (!strncmp (name, "cob_file_", 9) ||
	    !strcmp (name, "cob_open") ||
	    !strcmp (name, "cob_close") ||
	    !strcmp (name, "cob_read") ||
	    !strcmp (name, "cob_write") ||
	    !strcmp (name, "cob_rewrite") ||
	    !strcmp (name, "cob_delete") ||
	    !strcmp (name, "cob_start") ||
	    !strcmp (name, "cob_commit") ||
	    !strcmp (name, "cob_rollback") ||
	    !strcmp (name, "cob_unlock_file") ||
	    !strcmp (name, "cob_default_error_handle")) {
		return "fileio";
	}

	/* 11. Dynamic CALL / CANCEL machinery (call). */
	if (!strncmp (name, "cob_call_", 9) ||
	    !strncmp (name, "cob_resolve", 11) ||
	    !strcmp (name, "cob_set_cancel") ||
	    !strcmp (name, "cob_field_cancel")) {
		return "call";
	}

	/* 12. Plain terminal ACCEPT/DISPLAY fallback (termio).  The ACCEPT FROM
	      DATE/DAY/TIME and command-line/environment variants live in common.c
	      and fall through to the default branch. */
	if (!strcmp (name, "cob_accept") ||
	    !strcmp (name, "cob_display")) {
		return "termio";
	}

	/* 13. Raw byte-buffer helpers emitted as funcall names by typeck.c map to
	      the libcob_py.common byte-buffer helpers. */
	if (!strcmp (name, "memcpy") ||
	    !strcmp (name, "memcmp") ||
	    !strcmp (name, "memset") ||
	    !strcmp (name, "memmove")) {
		return "common";
	}

	/* 14. Default: every remaining cob_* routine is provided by common.c
	      (cob_cmp, cob_accept_*, cob_display_*, cob_check_*, cob_table_sort*,
	      cob_get_numdisp, cob_get_pointer, cob_get_prog_pointer, cob_get_switch,
	      cob_set_switch, cob_set_environment, cob_get_environment,
	      cob_external_addr, cob_init, cob_stop_run, cob_chain_setup,
	      cob_check_version, cob_fatal_error, cob_malloc, cob_allocate,
	      cob_free_alloc, cob_ready_trace, cob_reset_trace, cob_set_location). */
	if (!strncmp (name, "cob_", 4)) {
		return "common";
	}

	/* 15. Anything else: route through the libcob_py facade so the emitted
	      reference is always defined (libcob_py/__init__.py re-exports cob_*). */
	return "libcob_py";
}

/* MIGRATION (C -> Python): emit a runtime symbol as its module-qualified
   Python name, e.g. "move.cob_move".  Used by output_funcall() and by the
   direct call emissions scattered through this file. */
static void
output_pyfunc (const char *name)
{
	output ("%s.%s", codegen_pymod (name), name);
}

/*
 * Field
 */

static void
output_base (struct cb_field *f)
{
	struct cb_field		*f01;
	struct base_list	*bl;
	char			*nmp;
	char			name[COB_MINI_BUFF];

	f01 = cb_field_founder (f);

	if (f->flag_item_78) {
		fprintf (stderr, "Unexpected CONSTANT item\n");
		ABORT ();
	}

	if (f01->redefines) {
		f01 = f01->redefines;
	}

	/* Base name */
	if (f01->flag_external) {
		strcpy (name, f01->name);
		for (nmp = name; *nmp; nmp++) {
			if (*nmp == '-') {
				*nmp = '_';
			}
		}
	} else {
		sprintf (name, "%d", f01->id);
	}

	if (!f01->flag_base) {
		if (!f01->flag_external) {
			if (!f01->flag_local || f01->flag_is_global) {
				bl = cobc_malloc (sizeof (struct base_list));
				bl->f = f01;
				bl->curr_prog = excp_current_program_id;
				bl->next = base_cache;
				base_cache = bl;
			} else {
				/* MIGRATION (C->Python): a LOCAL-STORAGE BASED item becomes a
				   Python name initialised to None (re-bound when its based
				   storage is allocated).  For a global-use program a save_
				   shadow name preserves the binding across recursive entry. */
				if (current_prog->flag_global_use) {
					output_local ("%s%s = None",
							CB_PREFIX_BASE, name);
					output_local ("\t# %s\n", f01->name);
					output_local ("save_%s%s = None\n",
							CB_PREFIX_BASE, name);
				} else {
					output_local ("%s%s = None",
							CB_PREFIX_BASE, name);
					output_local ("\t# %s\n", f01->name);
				}
			}
		}
		f01->flag_base = 1;
	}
	/* MIGRATION (C -> Python): emit ONLY the bytearray name here (e.g. "b_5",
	   or "b_WS_FOO" for an external item).  The byte offset within the buffer
	   is emitted separately by output_base_offset() so the whole reference can
	   be wrapped by output_data() as memoryview(b_N)[offset:]. */
	output ("%s%s", CB_PREFIX_BASE, name);
}

/* MIGRATION (C -> Python): emit the byte offset of a field within its base
   buffer as a Python int expression, each term prefixed with " + " so it can
   follow a leading "0" inside a memoryview slice (memoryview(b)[0 + ...:]).
   Emits nothing when the field sits at offset 0 with no variable address. */
static void
output_base_offset (struct cb_field *f)
{
	struct cb_field		*p;
	struct cb_field		*v;

	if (cb_field_variable_address (f)) {
		for (p = f->parent; p; f = f->parent, p = f->parent) {
			for (p = p->children; p != f; p = p->sister) {
				v = cb_field_variable_size (p);
				if (v) {
					output (" + %d + ", v->offset - p->offset);
					if (v->size != 1) {
						output ("%d * ", v->size);
					}
					output_integer (v->occurs_depending);
				} else {
					output (" + %d", p->size * p->occurs_max);
				}
			}
		}
	} else if (f->offset > 0) {
		output (" + %d", f->offset);
	}
}

static void
output_data (cb_tree x)
{
	struct cb_literal	*l;
	struct cb_reference	*r;
	struct cb_field		*f;
	cb_tree			lsub;

	/* MIGRATION (C -> Python): a data reference becomes a mutable
	   memoryview(b_N)[offset:] over the backing bytearray (byte-exact with the
	   former "unsigned char *" pointer), or a bytes literal for literal data. */
	switch (CB_TREE_TAG (x)) {
	case CB_TAG_LITERAL:
		l = CB_LITERAL (x);
		if (CB_TREE_CLASS (x) == CB_CLASS_NUMERIC) {
			/* Numeric literal: digit bytes followed by an optional sign
			   byte, emitted as a Python bytes literal. */
			output ("b\"%s%s\"", l->data,
				(l->sign < 0) ? "-" : (l->sign > 0) ? "+" : "");
		} else {
			output_string (l->data, (int) l->size);
		}
		break;
	case CB_TAG_REFERENCE:
		r = CB_REFERENCE (x);
		f = CB_FIELD (r->value);

		/* MIGRATION (C -> Python): build memoryview(b_N)[ 0 + <offsets> : ].
		   The leading "0" cleanly absorbs the " + " prefix of every offset
		   term (base offset, subscripts, reference-modification offset). */
		output ("memoryview(");
		output_base (f);
		output (")[0");

		/* Base byte offset / variable-address arithmetic */
		output_base_offset (f);

		/* Subscripts */
		if (r->subs) {
			lsub = r->subs;
			for (; f; f = f->parent) {
				if (f->flag_occurs) {
					output (" + ");
					if (f->size != 1) {
						output ("%d * ", f->size);
					}
					output_index (CB_VALUE (lsub));
					lsub = CB_CHAIN (lsub);
				}
			}
		}

		/* Offset (reference modification) */
		if (r->offset) {
			output (" + ");
			output_index (r->offset);
		}

		output (":]");
		break;
	case CB_TAG_CAST:
		/* MIGRATION (C -> Python): the address-of decoration is dropped;
		   output_param yields the object reference directly. */
		output_param (x, 0);
		break;
	case CB_TAG_INTRINSIC:
		/* MIGRATION (C -> Python): "->" member access becomes "." */
		output ("module.cob_procedure_parameters[%d].data", field_iteration);
		break;
	case CB_TAG_CONST:
		if (x == cb_null) {
			output ("None");
			return;
		}
		/* Fall through */
	default:
		fprintf (stderr, "Unexpected tree tag %d\n", CB_TREE_TAG (x));
		ABORT ();
	}
}

static void
output_size (cb_tree x)
{
	struct cb_literal	*l;
	struct cb_reference	*r;
	struct cb_field		*f;
	struct cb_field		*p;
	struct cb_field		*q;

	/* MIGRATION (C -> Python): output_size emits the byte size of a field as an
	   integer expression.  The original C already emitted only plain integer
	   arithmetic (integer literals plus '+', '-', '*' and calls to output_index /
	   output_integer); that arithmetic is valid Python verbatim, and output_index
	   / output_integer now emit Python integer expressions, so no surface change
	   is required here.  Reference-modification size keeps the COBOL semantics
	   "f->size - (offset - 1)" -> "f->size - offset + 1". */
	switch (CB_TREE_TAG (x)) {
	case CB_TAG_CONST:
		output ("1");
		break;
	case CB_TAG_LITERAL:
		l = CB_LITERAL (x);
		output ("%d", (int)(l->size + ((l->sign != 0) ? 1 : 0)));
		break;
	case CB_TAG_REFERENCE:
		r = CB_REFERENCE (x);
		f = CB_FIELD (r->value);
		if (r->length) {
			output_integer (r->length);
		} else if (r->offset) {
			output ("%d - ", f->size);
			output_index (r->offset);
		} else {
			p = cb_field_variable_size (f);
			q = f;

again:
			if (p && (r->type == CB_SENDING_OPERAND
			    || !cb_field_subordinate (cb_field (p->occurs_depending), q))) {
				if (p->offset - q->offset > 0) {
					output ("%d + ", p->offset - q->offset);
				}
				if (p->size != 1) {
					output ("%d * ", p->size);
				}
				output_integer (p->occurs_depending);
				q = p;
			} else {
				output ("%d", q->size);
			}

			for (; q != f; q = q->parent) {
				if (q->sister && !q->sister->redefines) {
					q = q->sister;
					p = q->occurs_depending ? q : cb_field_variable_size (q);
					output (" + ");
					goto again;
				}
			}
		}
		break;
	default:
		fprintf (stderr, "Unexpected tree tag %d\n", CB_TREE_TAG (x));
		ABORT ();
	}
}

static int
lookup_attr (int type, int digits, int scale, int flags, unsigned char *pic, int lenstr)
{
	struct attr_list *l;

	/* Search attribute cache */
	for (l = attr_cache; l; l = l->next) {
		if (type == l->type
		    && digits == l->digits
		    && scale == l->scale && flags == l->flags
		    && ((pic == l->pic) || (pic && l->pic && lenstr == l->lenstr
		    && memcmp ((char *)pic, (char *)(l->pic), (size_t)lenstr) == 0))) {
			return l->id;
		}
	}

	/* Output new attribute */

	/* Cache it */
	l = cobc_malloc (sizeof (struct attr_list));
	l->id = cb_attr_id;
	l->type = type;
	l->digits = digits;
	l->scale = scale;
	l->flags = flags;
	l->pic = pic;
	l->lenstr = lenstr;
	l->next = attr_cache;
	attr_cache = l;

	return cb_attr_id++;
}

static void
output_attr (cb_tree x)
{
	struct cb_literal	*l;
	struct cb_reference	*r;
	struct cb_field		*f;
	int			id;
	int			type;
	int			flags;

	switch (CB_TREE_TAG (x)) {
	case CB_TAG_LITERAL:
		l = CB_LITERAL (x);
		if (CB_TREE_CLASS (x) == CB_CLASS_NUMERIC) {
			flags = 0;
			if (l->sign != 0) {
				flags = COB_FLAG_HAVE_SIGN | COB_FLAG_SIGN_SEPARATE;
			}
			id = lookup_attr (COB_TYPE_NUMERIC_DISPLAY,
					  (int) l->size, l->scale, flags, NULL, 0);
		} else {
			if (l->all) {
				id = lookup_attr (COB_TYPE_ALPHANUMERIC_ALL, 0, 0, 0, NULL, 0);
			} else {
				id = lookup_attr (COB_TYPE_ALPHANUMERIC, 0, 0, 0, NULL, 0);
			}
		}
		break;
	case CB_TAG_REFERENCE:
		type = cb_tree_type (x);
		r = CB_REFERENCE (x);
		f = CB_FIELD (r->value);
		flags = 0;
		if (r->offset) {
			id = lookup_attr (COB_TYPE_ALPHANUMERIC, 0, 0, 0, NULL, 0);
		} else {
			switch (type) {
			case COB_TYPE_GROUP:
			case COB_TYPE_ALPHANUMERIC:
				if (f->flag_justified) {
					id = lookup_attr (type, 0, 0, COB_FLAG_JUSTIFIED, NULL, 0);
				} else {
					id = lookup_attr (type, 0, 0, 0, NULL, 0);
				}
				break;
			default:
				if (f->pic->have_sign) {
					flags |= COB_FLAG_HAVE_SIGN;
					if (f->flag_sign_separate) {
						flags |= COB_FLAG_SIGN_SEPARATE;
					}
					if (f->flag_sign_leading) {
						flags |= COB_FLAG_SIGN_LEADING;
					}
				}
				if (f->flag_blank_zero) {
					flags |= COB_FLAG_BLANK_ZERO;
				}
				if (f->flag_justified) {
					flags |= COB_FLAG_JUSTIFIED;
				}
				if (f->flag_binary_swap) {
					flags |= COB_FLAG_BINARY_SWAP;
				}
				if (f->flag_real_binary) {
					flags |= COB_FLAG_REAL_BINARY;
				}
				if (f->flag_is_pointer) {
					flags |= COB_FLAG_IS_POINTER;
				}

				id = lookup_attr (type, f->pic->digits, f->pic->scale,
						  flags, (ucharptr) f->pic->str, f->pic->lenstr);
				break;
			}
		}
		break;
	case CB_TAG_ALPHABET_NAME:
		id = lookup_attr (COB_TYPE_ALPHANUMERIC, 0, 0, 0, NULL, 0);
		break;
	default:
		fprintf (stderr, "Unexpected tree tag %d\n", CB_TREE_TAG (x));
		ABORT ();
	}

	/* MIGRATION (C -> Python): emit the attribute object reference "a_N"
	   (a common.cob_field_attr instance defined in the storage section);
	   the C address-of decoration is dropped. */
	output ("%s%d", CB_PREFIX_ATTR, id);
}

/* MIGRATION (C -> Python): a field literal becomes a common.cob_field(...)
   constructor call -- common.cob_field(size, data, attr) -- instead of the C
   aggregate "{size, data, attr}". */
static void
output_field (cb_tree x)
{
	output ("common.cob_field(");
	output_size (x);
	output (", ");
	output_data (x);
	output (", ");
	output_attr (x);
	output (")");
}

/*
 * Literal
 */

static int
lookup_literal (cb_tree x)
{

	struct cb_literal	*literal;
	struct literal_list	*l;
	FILE			*savetarget;

	literal = CB_LITERAL (x);
	/* Search literal cache */
	for (l = literal_cache; l; l = l->next) {
		if (CB_TREE_CLASS (literal) == CB_TREE_CLASS (l->literal)
		    && literal->size == l->literal->size
		    && literal->all == l->literal->all
		    && literal->sign == l->literal->sign
		    && literal->scale == l->literal->scale
		    && memcmp (literal->data, l->literal->data, literal->size) == 0) {
			return l->id;
		}
	}

	/* Output new literal */
	savetarget = output_target;
	output_target = NULL;
	output_field (x);

	output_target = savetarget;

	/* Cache it */
	l = cobc_malloc (sizeof (struct literal_list));
	l->id = cb_literal_id;
	l->literal = literal;
	l->x = x;
	l->next = literal_cache;
	literal_cache = l;

	return cb_literal_id++;
}

/*
 * Integer
 */

static void
output_integer (cb_tree x)
{
	struct cb_binary_op	*p;
	struct cb_cast		*cp;
	struct cb_field		*f;

	switch (CB_TREE_TAG (x)) {
	case CB_TAG_CONST:
		/* MIGRATION (C -> Python): integer-context figurative constants.  ZERO
		   folds to the integer 0 and NULL to None.  The remaining constants in
		   this context carry a literal integer string ("1"/"0" for TRUE/FALSE),
		   which is valid Python verbatim.  As a defensive measure -- so a bare C
		   "&cob_<x>" address-of can never leak into the emitted Python -- a val
		   beginning with '&' is rewritten to the module-qualified runtime object
		   (common.cob_<x>), matching output_param's CONST handling. */
		if (x == cb_zero) {
			output ("0");
		} else if (x == cb_null) {
			output ("None");
		} else {
			const char	*cval = CB_CONST (x)->val;

			if (cval && cval[0] == '&') {
				output ("%s.%s", codegen_pymod (cval + 1), cval + 1);
			} else {
				output ("%s", cval ? cval : "None");
			}
		}
		break;
	case CB_TAG_INTEGER:
		output ("%d", CB_INTEGER (x)->val);
		break;
	case CB_TAG_LITERAL:
		output ("%d", cb_get_int (x));
		break;
	case CB_TAG_BINARY_OP:
		/* MIGRATION (C -> Python): integer arithmetic re-expressed with Python
		   operators.
		     '^' (exponentiation) -> int(x ** y): with integer operands x ** y is
		         an exact Python int, and int() truncates toward zero exactly as
		         the former "(int) pow()" did.
		     '/' (division) -> common.cob_trunc_div(x, y): this is the one operator
		         that is NOT a direct Python translation.  C integer division
		         truncates toward zero, whereas Python's '//' floors toward
		         negative infinity, so the two DISAGREE whenever the operands have
		         opposite signs (e.g. C: -7 / 2 == -3, Python: -7 // 2 == -4).  A
		         '/' tree can reach this integer path through a computed subscript
		         or an integer cast (parser.c builds cb_build_binary_op(.,'/',.)),
		         and a variable operand's sign is unknown at compile time, so a
		         bare '//' would break byte-for-byte parity.  We therefore emit a
		         runtime helper that performs C-style truncate-toward-zero integer
		         division for all signs and magnitudes (arbitrary precision, no
		         float).  [CONTRACT for libcob_py.common: cob_trunc_div(a, b)
		         returns int(a / b) truncated toward zero, i.e. C "a / b".]
		     '+', '-', '*' -> the identical Python operators.
		   The former need_double double-cast path was dead code (need_double was
		   always 0) and is dropped. */
		p = CB_BINARY_OP (x);
		if (p->op == '^') {
			output ("int(");
			output_integer (p->x);
			output (" ** ");
			output_integer (p->y);
			output (")");
		} else if (p->op == '/') {
			output ("common.cob_trunc_div (");
			output_integer (p->x);
			output (", ");
			output_integer (p->y);
			output (")");
		} else {
			output ("(");
			output_integer (p->x);
			output (" %c ", p->op);
			output_integer (p->y);
			output (")");
		}
		break;
	case CB_TAG_CAST:
		cp = CB_CAST (x);
		switch (cp->type) {
		case CB_CAST_ADDRESS:
			output ("(");
			output_data (cp->val);
			output (")");
			break;
		case CB_CAST_PROGRAM_POINTER:
			output_func_1 ("cob_call_resolve", x);
			break;
		default:
			fprintf (stderr, "Unexpected cast type %d\n", cp->type);
			ABORT ();
		}
		break;
	case CB_TAG_REFERENCE:
		/* MIGRATION (C -> Python): the C emitter had many inline fast paths
		   (native pointer casts, COB_BSWAP_* byte swaps, zoned/packed
		   decoders) for reading a field as an integer.  Every one of those
		   produced the SAME value the general cob_get_int() accessor returns --
		   they were pure speed optimizations.  Python cannot express raw
		   pointer casts or byte swaps inline, so every read collapses to a
		   module-qualified runtime accessor that decodes the field per its
		   cob_field_attr, preserving byte-for-byte results.  Only the POINTER
		   usages yield a pointer object (not an int) and therefore use the
		   dedicated pointer accessors in libcob_py.common. */
		f = cb_field (x);
		switch (f->usage) {
		case CB_USAGE_POINTER:
			output_func_1 ("cob_get_pointer", x);
			return;
		case CB_USAGE_PROGRAM_POINTER:
			output_func_1 ("cob_get_prog_pointer", x);
			return;
		default:
			break;
		}
		output_func_1 ("cob_get_int", x);
		break;
	case CB_TAG_INTRINSIC:
		/* MIGRATION (C -> Python): module-qualified integer accessor. */
		output ("move.cob_get_int (");
		output_param (x, -1);
		output (")");
		break;
	default:
		fprintf (stderr, "Unexpected tree tag %d\n", CB_TREE_TAG (x));
		ABORT ();
	}
}

static void
output_index (cb_tree x)
{
	/* MIGRATION (C -> Python): a subscript / offset is emitted as a zero-based
	   integer expression (COBOL is 1-based, so 1 is subtracted).  Constant
	   subscripts fold to an integer literal; a computed subscript becomes
	   "(<int-expr> - 1)" where <int-expr> is produced by output_integer (now a
	   Python integer expression).  All forms are valid Python verbatim. */
	switch (CB_TREE_TAG (x)) {
	case CB_TAG_INTEGER:
		output ("%d", CB_INTEGER (x)->val - 1);
		break;
	case CB_TAG_LITERAL:
		output ("%d", cb_get_int (x) - 1);
		break;
	default:
		output ("(");
		output_integer (x);
		output (" - 1)");
		break;
	}
}

/*
 * Parameter
 */

static void
output_param (cb_tree x, int id)
{
	struct cb_reference	*r;
	struct cb_field		*f;
	struct cb_field		*pechk;
	struct cb_cast		*cp;
	struct cb_binary_op	*bp;
	struct field_list	*fl;
	FILE			*savetarget;
	struct cb_intrinsic	*ip;
	struct cb_alphabet_name	*abp;
	struct cb_alphabet_name	*rbp;
	cb_tree			l;
	int			n;
	/* MIGRATION (C -> Python): the local "extrefs" tracker was write-only in
	   the original C (its value was never read); the EXTERNAL/BASED state is
	   carried entirely by the f->flag_* fields, which are still set below.  The
	   dead tracker is removed.  "fname" named the reusable C temp cob_field for
	   dynamic references; Python constructs each dynamic field inline via
	   common.cob_field(...) and needs no temp, but the name is retained to keep
	   the non-GNUC comma-tracking path intact. */
	int			sav_stack_id;
	char			fname[12];

	param_id = id;

	if (x == NULL) {
		/* MIGRATION (C -> Python): a NULL field pointer -> None */
		output ("None");
		return;
	}

	switch (CB_TREE_TAG (x)) {
	case CB_TAG_CONST:
		/* MIGRATION (C -> Python): figurative-constant runtime fields were
		   emitted as "&cob_zero", "&cob_space", ... in C.  In Python these are
		   module-qualified runtime objects (common.cob_zero, ...); a NULL data
		   pointer becomes None. */
		if (x == cb_null) {
			output ("None");
		} else {
			const char	*cval = CB_CONST (x)->val;

			if (cval && cval[0] == '&') {
				output ("%s.%s", codegen_pymod (cval + 1), cval + 1);
			} else {
				output ("%s", cval ? cval : "None");
			}
		}
		break;
	case CB_TAG_INTEGER:
		output_integer (x);
		break;
	case CB_TAG_STRING:
		output_string (CB_STRING (x)->data, (int) CB_STRING (x)->size);
		break;
	case CB_TAG_LOCALE_NAME:
		output_param (CB_LOCALE_NAME(x)->list, id);
		break;
	case CB_TAG_ALPHABET_NAME:
		/* MIGRATION (C -> Python): translation tables (cob_a2e / cob_ebcdic_ascii
		   / s_<name>) are module-level "bytes" objects in the emitted module, so
		   they are referenced bare; the NATIVE table (C "NULL") becomes None. */
		abp = CB_ALPHABET_NAME (x);
		switch (abp->type) {
		case CB_ALPHABET_STANDARD_1:
		case CB_ALPHABET_STANDARD_2:
#ifdef	COB_EBCDIC_MACHINE
			gen_ebcdic_ascii = 1;
			output ("cob_ebcdic_ascii");
			break;
#endif
		case CB_ALPHABET_NATIVE:
			gen_native = 1;
			output ("None");
			break;
		case CB_ALPHABET_EBCDIC:
#ifdef	COB_EBCDIC_MACHINE
			gen_native = 1;
			output ("None");
#else
			gen_ebcdic = 1;
			output ("cob_a2e");
#endif
			break;
		case CB_ALPHABET_CUSTOM:
			gen_custom = 1;
			output ("%s%s", CB_PREFIX_SEQUENCE, abp->cname);
			break;
		}
		break;
	case CB_TAG_CAST:
		/* MIGRATION (C -> Python): address-of decorations are dropped; the
		   referenced object (memoryview / field / int / size) is passed
		   directly.  ADDRESS-OF-ADDRESS becomes common.cob_addr_of(data), a
		   runtime pointer-to-pointer wrapper. */
		cp = CB_CAST (x);
		switch (cp->type) {
		case CB_CAST_INTEGER:
			output_integer (cp->val);
			break;
		case CB_CAST_ADDRESS:
			output_data (cp->val);
			break;
		case CB_CAST_ADDR_OF_ADDR:
			output ("common.cob_addr_of(");
			output_data (cp->val);
			output (")");
			break;
		case CB_CAST_LENGTH:
			output_size (cp->val);
			break;
		case CB_CAST_PROGRAM_POINTER:
			output_param (cp->val, id);
			break;
		}
		break;
	case CB_TAG_DECIMAL:
		/* MIGRATION (C -> Python): decimal temp object d<N> (a
		   numeric.cob_decimal); address-of dropped. */
		output ("d%d", CB_DECIMAL (x)->id);
		break;
	case CB_TAG_FILE:
		output ("%s%s", CB_PREFIX_FILE, CB_FILE (x)->cname);
		break;
	case CB_TAG_LITERAL:
		/* MIGRATION (C -> Python): constant field object c_<N>; address-of
		   dropped. */
		output ("%s%d", CB_PREFIX_CONST, lookup_literal (x));
		break;
	case CB_TAG_FIELD:
		/* MIGRATION (C -> Python): normalise a raw CB_FIELD operand into a
		   field REFERENCE before emission.  output_param's emission logic is
		   driven by cb_reference nodes (they carry the subscript/ref-mod and
		   bounds-check chain), so a bare field is wrapped via
		   cb_build_field_reference and re-dispatched through the
		   CB_TAG_REFERENCE arm below.  (Inherited verbatim from the C emitter,
		   where this arm was tagged with a "remove me" note; the wrap is still
		   required because some callers pass unwrapped fields.) */
		output_param (cb_build_field_reference (CB_FIELD (x), NULL), id);
		break;
	case CB_TAG_REFERENCE:
		r = CB_REFERENCE (x);
		/* MIGRATION (C -> Python): the C backend wrapped the runtime bounds
		   checks for a subscripted / reference-modified item in a GCC statement-
		   expression "({ check; check; field; })" whose value is the field.
		   Python has no statement-expression, so this is emitted as a tuple whose
		   final element is the field reference and whose leading elements are the
		   checks, then sub-scripted with [-1]:  "(check, check, field)[-1]".  Tuple
		   elements evaluate left-to-right, so the checks (which raise on a bounds
		   violation) run before the field view is built -- identical to C.  Every
		   value on r->check is a FUNCALL (cob_check_subscript / cob_check_odo /
		   cob_check_ref_mod, built by typeck.c via cb_build_funcall_4); each is
		   emitted as a module-qualified call expression followed by ", ".  The
		   closing "(...)[-1]" is emitted after the field expression below.  The
		   __GNUC__ path is the live/authoritative path for the Python backend. */
		if (r->check) {
#ifdef __GNUC__
			output (" (");
#else
			inside_stack[inside_check] = 0;
			++inside_check;
			output (" (\n");
#endif
			for (l = r->check; l; l = CB_CHAIN (l)) {
				sav_stack_id = stack_id;
				if (CB_FUNCALL_P (CB_VALUE (l))) {
					output_funcall (CB_VALUE (l));
					output (", ");
				} else {
					/* Defensive: all checks are FUNCALLs today, so this
					   branch is unreachable; keep a safe fallback. */
					output_stmt (CB_VALUE (l));
				}
				stack_id = sav_stack_id;
			}
		}

		if (CB_FILE_P (r->value)) {
			/* MIGRATION (C -> Python): a FILE reference is the module-level
			   file object h_<cname> (no address-of). */
			output ("%s%s", CB_PREFIX_FILE, CB_FILE (r->value)->cname);
			if (r->check) {
#ifdef __GNUC__
				output (")[-1]");
#else
				--inside_check;
				output (" )");
#endif
			}
			break;
		}
		if (CB_ALPHABET_NAME_P (r->value)) {
			/* MIGRATION (C -> Python): an alphabet reference resolves to a
			   module-level cob_field object (f_ebcdic_ascii / f_native /
			   f_ebcdic / f_<cname>); the C address-of is dropped. */
			rbp = CB_ALPHABET_NAME (r->value);
			switch (rbp->type) {
			case CB_ALPHABET_STANDARD_1:
			case CB_ALPHABET_STANDARD_2:
#ifdef	COB_EBCDIC_MACHINE
				gen_ebcdic_ascii = 1;
				output ("f_ebcdic_ascii");
				break;
#endif
			case CB_ALPHABET_NATIVE:
				gen_native = 1;
				output ("f_native");
				break;
			case CB_ALPHABET_EBCDIC:
#ifdef	COB_EBCDIC_MACHINE
				gen_native = 1;
				output ("f_native");
#else
				gen_full_ebcdic = 1;
				output ("f_ebcdic");
#endif
				break;
			case CB_ALPHABET_CUSTOM:
				gen_custom = 1;
				output ("f_%s", rbp->cname);
				break;
			}
			if (r->check) {
#ifdef __GNUC__
				output (")[-1]");
#else
				--inside_check;
				output (" )");
#endif
			}
			break;
		}
		f = CB_FIELD (r->value);
		if (f->redefines && f->redefines->flag_external) {
			f->flag_item_external = 1;
			f->flag_external = 1;
		}
		if (f->redefines && f->redefines->flag_item_based) {
			f->flag_local = 1;
		}
		for (pechk = f->parent; pechk; pechk = pechk->parent) {
			if (pechk->flag_external) {
				f->flag_item_external = 1;
				break;
			}
			if (pechk->redefines && pechk->redefines->flag_external) {
				f->flag_item_external = 1;
				f->flag_external = 1;
				break;
			}
			if (pechk->flag_item_based) {
				f->flag_local = 1;
				break;
			}
			if (pechk->redefines && pechk->redefines->flag_item_based) {
				f->flag_local = 1;
				break;
			}
		}
		if (f->flag_external) {
			f->flag_item_external = 1;
		}
		if (!r->subs && !r->offset && f->count > 0
		    && !cb_field_variable_size (f) &&
		       !cb_field_variable_address (f)) {
			if (!f->flag_field) {
				savetarget = output_target;
				output_target = NULL;
				output_field (x);

				fl = cobc_malloc (sizeof (struct field_list));
				fl->x = x;
				fl->f = f;
				fl->curr_prog = excp_current_program_id;
				fl->nulldata = (r->subs != NULL);
				fl->next = field_cache;
				field_cache = fl;

				f->flag_field = 1;
				output_target = savetarget;
			}
			if (f->flag_local) {
				/* MIGRATION (C -> Python): a LOCAL / BASED / LINKAGE or ANY
				   LENGTH item is a cached cob_field whose backing .data must be
				   re-pointed at run time.  The C comma-expression
				   "(f_N.data = <data>, &f_N)" becomes the runtime setter
				   common.cob_field_set_data(f_N, <data>), which assigns the new
				   data view and returns the field object so it can be passed as
				   an argument.  An ANY LENGTH item already fixed up earlier is
				   referenced by name only (address-of dropped). */
				if (f->flag_any_length && f->flag_anylen_done) {
					output ("%s%d", CB_PREFIX_FIELD, f->id);
				} else {
					output ("common.cob_field_set_data (%s%d, ", CB_PREFIX_FIELD, f->id);
					output_data (x);
					output (")");
					if (f->flag_any_length) {
						f->flag_anylen_done = 1;
					}
				}
			} else {
				/* MIGRATION (C -> Python): a fixed, cached field is referenced by
				   its module-level object name (s_<id> for SCREEN items, f_<id>
				   otherwise); the C address-of is dropped. */
				if (screenptr && f->storage == CB_STORAGE_SCREEN) {
					output ("s_%d", f->id);
				} else {
					output ("%s%d", CB_PREFIX_FIELD, f->id);
				}
			}
		} else {
			/* MIGRATION (C -> Python): a dynamic field reference (subscripted,
			   reference-modified, or of variable size / address) was emitted in C
			   as a comma-expression that mutated a reusable temp "fN" and returned
			   its address: "(fN.size = <size>, fN.data = <data>, fN.attr = <attr>,
			   &fN)".  In Python each such reference is a freshly constructed
			   common.cob_field(<size>, <data>, <attr>); no temp is reused and
			   there is no pointer aliasing.  The C temp counter (stack_id /
			   num_cob_fields) and the sprintf into fname are retained here only to
			   keep the dead non-GNUC comma-tracking path syntactically consistent;
			   the temp-field declaration block at storage finalization (which
			   would otherwise emit "cob_field fN;") is converted to emit nothing
			   under the single-module Python model. */
			if (stack_id >= num_cob_fields) {
				num_cob_fields = stack_id + 1;
			}
			sprintf (fname, "f%d", stack_id++);
#ifndef __GNUC__
			if (inside_check != 0) {
				if (inside_stack[inside_check-1] != 0) {
					inside_stack[inside_check-1] = 0;
					output (",\n");
				}
			}
#endif
			output ("common.cob_field (");
			output_size (x);
			output (", ");
			output_data (x);
			output (", ");
			output_attr (x);
			output (")");
		}

		if (r->check) {
#ifdef __GNUC__
			output (")[-1]");
#else
			--inside_check;
			output (" )");
#endif
		}
		break;
	case CB_TAG_BINARY_OP:
		/* MIGRATION (C -> Python): the COBOL binary-operation helper is module-
		   qualified to the intrinsic runtime (intrinsic.cob_intr_binop); both
		   operands are passed as field / value objects, the operator stays an
		   integer opcode. */
		bp = CB_BINARY_OP (x);
		output_pyfunc ("cob_intr_binop");
		output (" (");
		output_param (bp->x, id);
		output (", ");
		output ("%d", bp->op);
		output (", ");
		output_param (bp->y, id);
		output (")");
		break;
	case CB_TAG_INTRINSIC:
		/* MIGRATION (C -> Python): the intrinsic-function runtime routine is
		   module-qualified (every intr_routine is a cob_intr_* name, so it
		   resolves to the "intrinsic" module).  A NULL field argument -> None. */
		n = 0;
		ip = CB_INTRINSIC (x);
		output_pyfunc (ip->intr_tab->intr_routine);
		output (" (");
		if (ip->intr_tab->refmod) {
			if (ip->offset) {
				output_integer (ip->offset);
				output (", ");
			} else {
				output ("0, ");
			}
			if (ip->length) {
				output_integer (ip->length);
			} else {
				output ("0");
			}
			if (ip->intr_field || ip->args) {
				output (", ");
			}
		}
		if (ip->intr_field) {
			if (ip->intr_field == cb_int0) {
				output ("None");
			} else if (ip->intr_field == cb_int1) {
				for (l = ip->args; l; l = CB_CHAIN (l)) {
					n++;
				}
				output ("%d", n);
			} else {
				output_param (ip->intr_field, id);
			}
			if (ip->args) {
				output (", ");
			}
		}
		for (l = ip->args; l; l = CB_CHAIN (l)) {
			output_param (CB_VALUE (l), id);
			id++;
			param_id++;
			if (CB_CHAIN (l)) {
				output (", ");
			}
		}
		output (")");
		break;
	default:
		fprintf (stderr, "Unexpected tree tag %d\n", CB_TREE_TAG (x));
		ABORT ();
	}
}

/*
 * Function call
 */

static void
output_funcall (cb_tree x)
{
	struct cb_funcall	*p;
	cb_tree			l;
	int			i;

	p = CB_FUNCALL (x);
	if (p->name[0] == '$') {
		/* MIGRATION (C -> Python): the three "$" pseudo-funcalls are inline
		   single-byte operations the C emitter wrote with raw pointer
		   dereferences ("*(ptr)").  output_data now yields a writable
		   memoryview slice ("memoryview(b_N)[off:]"), so the first byte is
		   addressed with "[0]" (memoryview item access reads/writes the
		   underlying bytearray and returns/accepts a plain int 0..255).  The C
		   "(int)" cast is unnecessary because memoryview indexing already yields
		   an int.  The set operation masks with "& 0xFF" to reproduce C's silent
		   truncation of an out-of-range value into an unsigned char (and to avoid
		   a Python ValueError), which is byte-identical to "*(unsigned char*) =
		   val".  Result values match the C byte arithmetic exactly. */
		switch (p->name[1]) {
		case 'E':
			/* Set of one character: <data>[0] = (<val>) & 0xFF */
			output_data (p->argv[0]);
			output ("[0] = (");
			output_param (p->argv[1], 1);
			output (") & 0xFF");
			break;
		case 'F':
			/* Move of one character: <dst>[0] = <src>[0] */
			output_data (p->argv[0]);
			output ("[0] = ");
			output_data (p->argv[1]);
			output ("[0]");
			break;
		case 'G':
			/* Test of one character: (<data>[0] - <ref-byte>) */
			output ("(");
			output_data (p->argv[0]);
			output ("[0]");
			if (p->argv[1] == cb_space) {
				output (" - 0x20)");
			} else if (p->argv[1] == cb_zero) {
				output (" - 0x30)");
			} else if (p->argv[1] == cb_low) {
				/* LOW-VALUE is byte 0x00, so nothing is subtracted */
				output (")");
			} else if (p->argv[1] == cb_high) {
				output (" - 0xFF)");
			} else if (CB_LITERAL_P (p->argv[1])) {
				output (" - %d)", *(CB_LITERAL (p->argv[1])->data));
			} else {
				output (" - ");
				output_data (p->argv[1]);
				output ("[0])");
			}
			break;
		default:
			ABORT ();
		}
		return;
	}
	/* MIGRATION (C -> Python): a normal runtime call.  The C runtime function
	   name (chosen by the immutable typeck.c front-end) is emitted module-
	   qualified as libcob_py.<module>.<name>(...) via output_pyfunc /
	   codegen_pymod; address-of / cast decorations are dropped by output_param /
	   output_data.  The variadic convention is preserved verbatim: a variable
	   funcall emits the leading argument count (p->varcnt) followed by the
	   flattened argument list, exactly as the C ABI the runtime mirrors. */
	screenptr = p->screenptr;
	output_pyfunc (p->name);
	output (" (");
	for (i = 0; i < p->argc; i++) {
		if (p->varcnt && i + 1 == p->argc) {
			output ("%d, ", p->varcnt);
			for (l = p->argv[i]; l; l = CB_CHAIN (l)) {
				output_param (CB_VALUE (l), i);
				i++;
				if (CB_CHAIN (l)) {
					output (", ");
				}
			}
		} else {
			output_param (p->argv[i], i);
			if (i + 1 < p->argc) {
				output (", ");
			}
		}
	}
	output (")");
	screenptr = 0;
}

static void
output_func_1 (const char *name, cb_tree x)
{
	/* MIGRATION (C -> Python): emit a module-qualified single-argument runtime
	   call, e.g. "move.cob_get_int(<field>)" or "common.cob_get_pointer(...)". */
	output_pyfunc (name);
	output (" (");
	output_param (x, param_id);
	output (")");
}

/*
 * Condition
 */

static void
output_cond (cb_tree x, int save_flag)
{
	struct cb_binary_op	*p;

	/* MIGRATION (C -> Python): a COBOL condition is lowered to a numeric value
	   that is compared against 0 (e.g. cob_cmp(a,b) <op> 0).  The C emitter
	   produced C boolean operators and an "(int)" cast; this re-expresses the
	   same tree as a Python boolean expression:
	     - logical NOT/AND/OR  ->  not / and / or
	     - the redundant "(int)" cast is dropped (the inner value is already an
	       int in Python: a memoryview byte, a cob_cmp* result, etc.)
	     - a condition that needs intermediate statements (the decimal-comparison
	       path builds a LIST of FUNCALLs ending in cob_decimal_cmp) was a GCC
	       statement-expression "({ s1; s2; sN; })"; it becomes the Python tuple
	       "(s1, s2, ..., sN)[-1]" (every list element is a FUNCALL, verified
	       against typeck.c's decimal_expand/dpush)
	     - when save_flag is set the C emitter captured the value with
	       "(ret = <expr>)"; in Python this is the walrus assignment
	       "(ret := <expr>)". */
	switch (CB_TREE_TAG (x)) {
	case CB_TAG_CONST:
		if (x == cb_true) {
			output ("True");
		} else if (x == cb_false) {
			output ("False");
		} else {
			ABORT ();
		}
		break;
	case CB_TAG_BINARY_OP:
		p = CB_BINARY_OP (x);
		switch (p->op) {
		case '!':
			output ("not ");
			output_cond (p->x, save_flag);
			break;

		case '&':
		case '|':
			output ("(");
			output_cond (p->x, save_flag);
			output (p->op == '&' ? " and " : " or ");
			output_cond (p->y, save_flag);
			output (")");
			break;

		case '=':
		case '<':
		case '[':
		case '>':
		case ']':
		case '~':
			output ("(");
			output_cond (p->x, save_flag);
			switch (p->op) {
			case '=':
				output (" == 0");
				break;
			case '<':
				output (" < 0");
				break;
			case '[':
				output (" <= 0");
				break;
			case '>':
				output (" > 0");
				break;
			case ']':
				output (" >= 0");
				break;
			case '~':
				output (" != 0");
				break;
			}
			output (")");
			break;

		default:
			output_integer (x);
			break;
		}
		break;
	case CB_TAG_FUNCALL:
		if (save_flag) {
			output ("(ret := ");
		}
		output_funcall (x);
		if (save_flag) {
			output (")");
		}
		break;
	case CB_TAG_LIST:
		if (save_flag) {
			output ("(ret := ");
		}
		output ("(");
		for (; x; x = CB_CHAIN (x)) {
			if (CB_FUNCALL_P (CB_VALUE (x))) {
				output_funcall (CB_VALUE (x));
			} else {
				/* Defensive: condition lists hold only FUNCALLs today
				   (decimal_expand/dpush), so this is unreachable. */
				output_funcall (CB_VALUE (x));
			}
			output (", ");
		}
		output (")[-1]");
		if (save_flag) {
			output (")");
		}
		break;
	default:
		fprintf (stderr, "Unexpected tree tag %d\n", CB_TREE_TAG (x));
		ABORT ();
	}
}

/*
 * MOVE
 */

static void
output_move (cb_tree src, cb_tree dst)
{
	/* MIGRATION (C -> Python): unchanged.  This helper builds a MOVE funcall
	   tree and routes it through output_stmt -> output_funcall, where the
	   selected cob_move / data-movement family name is module-qualified to the
	   "move" runtime module (codegen_pymod rule for cob_move / cob_set_int /
	   cob_get_int) -- the GAP module per AAP 0.6.5.  No surface change here. */
	/* suppress warnings */
	suppress_warn = 1;
	output_stmt (cb_build_move (src, dst));
	suppress_warn = 0;
}

/*
 * INITIALIZE
 */

static int
initialize_type (struct cb_initialize *p, struct cb_field *f, int topfield)
{
	cb_tree		l;
	int		type;

	if (f->flag_item_78) {
		fprintf (stderr, "Unexpected CONSTANT item\n");
		ABORT ();
	}

	if (f->flag_chained) {
		return INITIALIZE_ONE;
	}

	if (f->flag_external) {
		return INITIALIZE_EXTERNAL;
	}

	if (f->redefines && (!topfield || !p->flag_statement)) {
		return INITIALIZE_NONE;
	}

	if (p->val && f->values) {
		if(topfield && f->flag_occurs && !p->flag_statement) {
			return INITIALIZE_COMPOUND;
		} else {
			return INITIALIZE_ONE;
		}
	}

	if (p->flag_statement && !f->children) {
		if (strlen (f->name) > 4 && f->name[4] == '$') {
			return INITIALIZE_NONE;
		}
	}

	if (f->children) {
		type = initialize_type (p, f->children, 0);
		if (type == INITIALIZE_ONE) {
			return INITIALIZE_COMPOUND;
		}
		for (f = f->children->sister; f; f = f->sister) {
			if (type != initialize_type (p, f, 0)) {
				return INITIALIZE_COMPOUND;
			}
		}
		return type;
	} else {
		for (l = p->rep; l; l = CB_CHAIN (l)) {
			if ((int)CB_PURPOSE_INT (l) == (int)CB_TREE_CATEGORY (f)) {
				return INITIALIZE_ONE;
			}
		}
	}

	if (p->def) {
		if (f->usage == CB_USAGE_FLOAT || f->usage == CB_USAGE_DOUBLE) {
			return INITIALIZE_ONE;
		}
		switch (CB_TREE_CATEGORY (f)) {
		case CB_CATEGORY_NUMERIC_EDITED:
		case CB_CATEGORY_ALPHANUMERIC_EDITED:
		case CB_CATEGORY_NATIONAL_EDITED:
			return INITIALIZE_ONE;
		default:
			if (cb_tree_type (CB_TREE (f)) == COB_TYPE_NUMERIC_PACKED) {
				return INITIALIZE_ONE;
			} else {
				return INITIALIZE_DEFAULT;
			}
		}
	}

	return INITIALIZE_NONE;
}

static int
initialize_uniform_char (struct cb_field *f)
{
	int	c;

	if (f->children) {
		c = initialize_uniform_char (f->children);
		for (f = f->children->sister; f; f = f->sister) {
			if (!f->redefines) {
				if (c != initialize_uniform_char (f)) {
					return -1;
				}
			}
		}
		return c;
	} else {
		switch (cb_tree_type (CB_TREE (f))) {
		case COB_TYPE_NUMERIC_BINARY:
			return 0;
		case COB_TYPE_NUMERIC_DISPLAY:
			return '0';
		case COB_TYPE_ALPHANUMERIC:
			return ' ';
		default:
			return -1;
		}
	}
}

static void
output_figurative (cb_tree x, struct cb_field *f, const int value)
{
	/* MIGRATION (C -> Python): fill a field with a single figurative byte.  A
	   one-byte field is set via memoryview item assignment ("<data>[0] = v");
	   wider fields use the module-qualified common.memset.  "value" is a
	   compile-time figurative byte (0..255), so no masking is required. */
	output_prefix ();
	if (f->size == 1) {
		output_data (x);
		output ("[0] = %d\n", value);
	} else {
		output ("common.memset (");
		output_data (x);
		if (CB_REFERENCE_P(x) && CB_REFERENCE(x)->length) {
			output (", %d, ", value);
			output_size (x);
			output (")\n");
		} else {
			output (", %d, %d)\n", value, f->size);
		}
	}
}

static void
output_initialize_literal (cb_tree x, struct cb_field *f, struct cb_literal *l)
{
	size_t	i;
	size_t	n;

	/* MIGRATION (C -> Python): memset/memcpy are module-qualified (common.*);
	   the data pointer arithmetic "<data> + (i0 * sz)" becomes a memoryview
	   slice "<data>[i0 * sz:]"; the C "for (i0=0; i0<N; i0++)" loop becomes a
	   Python "for i0 in range(N):" block.  IMPORTANT byte-parity detail: in C
	   the loop counter equals N after the loop, so the trailing remainder copy
	   "<data> + (i0 * sz)" addresses offset N*sz; a Python "for" leaves i0 at
	   N-1, so the remainder copy emits the EXPLICIT literal offset (i * l->size,
	   i.e. f->size - n) instead of reusing i0 -- preserving the exact target
	   offset. */
	if (l->size == 1) {
		output_prefix ();
		output ("common.memset (");
		output_data (x);
		if (CB_REFERENCE_P(x) && CB_REFERENCE(x)->length) {
			output (", %d, ", l->data[0]);
			output_size (x);
			output (")\n");
		} else {
			output (", %d, %d)\n", l->data[0], f->size);
		}
		return;
	}
	if (l->size >= f->size) {
		output_prefix ();
		output ("common.memcpy (");
		output_data (x);
		output (", ");
		output_string (l->data, f->size);
		output (", %d)\n", f->size);
		return;
	}
	i = f->size / l->size;
	i_counters[0] = 1;
	output_line ("for i0 in range(%u):", (unsigned int)i);
	output_block_open ();
	output_prefix ();
	output ("common.memcpy (");
	output_data (x);
	output ("[i0 * %u:], ", (unsigned int)l->size);
	output_string (l->data, l->size);
	output (", %u)\n", (unsigned int)l->size);
	output_block_close ();
	n = f->size % l->size;
	if (n) {
		output_prefix ();
		output ("common.memcpy (");
		output_data (x);
		output ("[%u:], ", (unsigned int)(i * l->size));
		output_string (l->data, n);
		output (", %u)\n", (unsigned int)n);
	}
}

static void
output_initialize_fp (cb_tree x, struct cb_field *f)
{
	/* MIGRATION (C -> Python): zero a floating-point field.  The C code copied
	   the bytes of a 0.0 float/double into the field.  IEEE-754 positive zero is
	   an all-zero byte pattern, so this is byte-identical to a zero-fill of the
	   field's 4 (COMP-1) or 8 (COMP-2) bytes via common.memset. */
	output_prefix ();
	output ("common.memset (");
	output_data (x);
	if (f->usage == CB_USAGE_FLOAT) {
		output (", 0, 4)\n");
	} else {
		output (", 0, 8)\n");
	}
}

static void
output_initialize_external (cb_tree x, struct cb_field *f)
{
	unsigned char	*p;
	char		name[COB_MINI_BUFF];

	/* MIGRATION (C -> Python): an EXTERNAL data item is backed by named shared
	   storage.  The C emitter assigned the external address to the field's base
	   pointer.  In Python the module-level base object (b_N) is rebound to the
	   shared buffer returned by common.cob_external_addr(name, size); output_base
	   names the assignable base (output_data would yield a non-assignable
	   memoryview slice).  [CONTRACT for libcob_py.common: cob_external_addr
	   returns a persistent writable buffer keyed by name+size, shared across
	   compilation units, that memoryview() can wrap.] */
	output_prefix ();
	output_base (f);
	if (f->ename) {
		output (" = common.cob_external_addr (\"%s\", %d)\n", f->ename, f->size);
	} else {
		strcpy (name, f->name);
		for (p = (unsigned char *)name; *p; p++) {
			if (islower (*p)) {
				*p = (unsigned char)toupper (*p);
			}
		}
		output (" = common.cob_external_addr (\"%s\", %d)\n", name, f->size);
	}
}

static void
output_initialize_uniform (cb_tree x, int c, int size)
{
	/* MIGRATION (C -> Python): fill "size" bytes with the uniform byte "c".  A
	   one-byte target uses memoryview item assignment ("<data>[0] = c"); wider
	   targets use module-qualified common.memset.  "c" is a compile-time byte
	   (0..255). */
	output_prefix ();
	if (size == 1) {
		output_data (x);
		output ("[0] = %d\n", c);
	} else {
		output ("common.memset (");
		output_data (x);
		if (CB_REFERENCE_P(x) && CB_REFERENCE(x)->length) {
			output (", %d, ", c);
			output_size (x);
			output (")\n");
		} else {
			output (", %d, %d)\n", c, size);
		}
	}
}

static void
output_initialize_one (struct cb_initialize *p, cb_tree x)
{
	struct cb_field		*f;
	cb_tree			value;
	cb_tree			lrp;
	struct cb_literal	*l;
	int			i;
	int			n;
	int			buffchar;

	static char		*buff = NULL;
	static int		lastsize = 0;

	f = cb_field (x);

	/* CHAINING */
	if (f->flag_chained) {
		/* MIGRATION (C -> Python): module-qualified call; drop ';'. */
		output_prefix ();
		output_pyfunc ("cob_chain_setup");
		output (" (");
		output_data (x);
		output (", %d, %d)\n", f->param_num, f->size);
		return;
	}
	/* Initialize by value */
	if (p->val && f->values) {
		value = CB_VALUE (f->values);
		if (value == cb_space) {
			/* Fixme: This is to avoid an error when a
			   numeric-edited item has VALUE SPACE because
			   cob_build_move doubly checks the value.
			   We should instead check the value only once.  */
			output_figurative (x, f, ' ');
		} else if (value == cb_low) {
			output_figurative (x, f, 0);
		} else if (value == cb_high) {
			output_figurative (x, f, 255);
		} else if (value == cb_quote) {
			output_figurative (x, f, '"');
		} else if (value == cb_zero && f->usage == CB_USAGE_DISPLAY) {
			output_figurative (x, f, '0');
		} else if (value == cb_null && f->usage == CB_USAGE_DISPLAY) {
			output_figurative (x, f, 0);
		} else if (CB_LITERAL_P (value) && CB_LITERAL (value)->all) {
			/* ALL literal */
			output_initialize_literal (x, f, CB_LITERAL (value));
		} else if (CB_CONST_P (value)
			   || CB_TREE_CLASS (value) == CB_CLASS_NUMERIC) {
			/* Figurative literal, numeric literal */
			output_move (value, x);
		} else {
			/* Alphanumeric literal */
			/* We do not use output_move here because
			   we do not want to have the value be edited. */
			l = CB_LITERAL (value);
			if (!buff) {
				if (f->size <= COB_SMALL_BUFF) {
					buff = cobc_malloc (COB_SMALL_BUFF);
					lastsize = COB_SMALL_BUFF;
				} else {
					buff = cobc_malloc ((size_t)f->size);
					lastsize = f->size;
				}
			} else {
				if (f->size > lastsize) {
					free (buff);
					buff = cobc_malloc ((size_t)f->size);
					lastsize = f->size;
				}
			}
			l = CB_LITERAL (value);
			if ((int)l->size >= (int)f->size) {
				memcpy (buff, l->data, (size_t)f->size);
			} else {
				memcpy (buff, l->data, l->size);
				memset (buff + l->size, ' ', f->size - l->size);
			}
			/* MIGRATION (C -> Python): the run-length analysis of "buff"
			   is a compile-time optimization that is preserved verbatim; only
			   the EMITTED primitives change -- memset/memcpy are module-
			   qualified (common.*), a one-byte store uses memoryview item
			   assignment, the C "<data> + off" pointer arithmetic becomes the
			   memoryview slice "<data>[off:]", and trailing ';' are dropped. */
			output_prefix ();
			if (f->size == 1) {
				output_data (x);
				output ("[0] = %d\n", *(unsigned char *)buff);
			} else {
				buffchar = *buff;
				for (i = 0; i < f->size; i++) {
					if (*(buff + i) != buffchar) {
						break;
					}
				}
				if (i == f->size) {
					output ("common.memset (");
					output_data (x);
					output (", %d, %d)\n", buffchar, f->size);
				} else {
					if (f->size >= 8) {
						buffchar = *(buff + f->size - 1);
						n = 0;
						for (i = f->size - 1; i >= 0; i--, n++) {
							if (*(buff + i) != buffchar) {
								break;
							}
						}
						if (n > 2) {
							output ("common.memcpy (");
							output_data (x);
							output (", ");
							output_string ((ucharptr) buff,
								       f->size - n);
							output (", %d)\n", f->size - n);
							output_prefix ();
							output ("common.memset (");
							output_data (x);
							output ("[%d:], %d, %d)\n",
								f->size - n, buffchar, n);
							return;
						}
					}
					output ("common.memcpy (");
					output_data (x);
					output (", ");
					output_string ((ucharptr) buff, f->size);
					output (", %d)\n", f->size);
				}
			}
		}
		return;
	}

	/* Initialize replacing */
	if (!f->children) {
		for (lrp = p->rep; lrp; lrp = CB_CHAIN (lrp)) {
			if ((int)CB_PURPOSE_INT (lrp) == (int)CB_TREE_CATEGORY (x)) {
				output_move (CB_VALUE (lrp), x);
				return;
			}
		}
	}

	/* Initialize by default */
	if (p->def) {
		if (f->usage == CB_USAGE_FLOAT || f->usage == CB_USAGE_DOUBLE) {
			output_initialize_fp (x, f);
			return;
		}
		switch (CB_TREE_CATEGORY (x)) {
		case CB_CATEGORY_NUMERIC:
		case CB_CATEGORY_NUMERIC_EDITED:
			output_move (cb_zero, x);
			break;
		case CB_CATEGORY_ALPHANUMERIC_EDITED:
		case CB_CATEGORY_NATIONAL_EDITED:
			output_move (cb_space, x);
			break;
		default:
			fprintf (stderr, "Unexpected tree category %d\n", CB_TREE_CATEGORY (x));
			ABORT ();
		}
	}
}

static void
output_initialize_compound (struct cb_initialize *p, cb_tree x)
{
	struct cb_field	*ff;
	struct cb_field	*f;
	struct cb_field	*last_field;
	cb_tree		c;
	int		type;
	int		last_char;
	int		i;
	size_t		size;
	static int recurs_level = 0;

	ff = cb_field (x);
	if (!recurs_level  && !p->flag_statement && ff->parent == NULL && ff->flag_occurs) {
		f = ff;
	} else {
		f = ff->children;
	}
	++recurs_level;
	while(f) {
		type = initialize_type (p, f, 0);
		c = cb_build_field_reference (f, x);

		switch (type) {
		case INITIALIZE_NONE:
			break;
		case INITIALIZE_DEFAULT:
		{
			last_field = f;
			last_char = initialize_uniform_char (f);

			if (last_char != -1) {
				if (f->flag_occurs) {
					CB_REFERENCE (c)->subs =
					    cb_cons (cb_int1, CB_REFERENCE (c)->subs);
				}

				for (; f->sister; f = f->sister) {
					if (!f->sister->redefines) {
						if (initialize_type (p, f->sister, 0) != INITIALIZE_DEFAULT
						    || initialize_uniform_char (f->sister) != last_char) {
							break;
						}
					}
				}

				if (f->sister) {
					size = f->sister->offset - last_field->offset;
				} else {
					size = ff->offset + ff->size - last_field->offset;
				}

				output_initialize_uniform (c, last_char, (int) size);
				break;
			}
			/* Fall through */
		}
		default:
			if (f->flag_occurs) {
				/* Begin occurs loop */
				/* MIGRATION (C -> Python): the C 1-based inclusive loop
				   "for (iN = 1; iN <= max; iN++)" becomes the Python
				   "for iN in range (1, max + 1):" form (range stop is
				   exclusive, hence the "+ 1").  The C brace block becomes
				   an indented Python suite opened by output_block_open(). */
				i = f->indexes;
				i_counters[i] = 1;
				output_line ("for i%d in range (1, %d + 1):",
					     i, f->occurs_max);
				output_block_open ();
				CB_REFERENCE (c)->subs =
				    cb_cons (cb_i[i], CB_REFERENCE (c)->subs);
			}

			if (type == INITIALIZE_ONE) {
				output_initialize_one (p, c);
			} else {
				output_initialize_compound (p, c);
			}

			if (f->flag_occurs) {
				/* Close loop */
				/* MIGRATION (C -> Python): close the occurs-loop suite by
				   dedenting; output_block_close() injects "pass" if the
				   loop body emitted nothing. */
				CB_REFERENCE (c)->subs = CB_CHAIN (CB_REFERENCE (c)->subs);
				output_block_close ();
			}
		}
		if(f == ff) break;
		f = f->sister;
	}
	--recurs_level;
}

static void
output_initialize (struct cb_initialize *p)
{
	struct cb_field *f;
	int		c;

	f = cb_field (p->var);
	switch (initialize_type (p, f, 1)) {
	case INITIALIZE_NONE:
		break;
	case INITIALIZE_ONE:
		output_initialize_one (p, p->var);
		break;
	case INITIALIZE_EXTERNAL:
		output_initialize_external (p->var, f);
		break;
	case INITIALIZE_DEFAULT:
		c = initialize_uniform_char (f);
		if (c != -1) {
			output_initialize_uniform (p->var, c, f->size);
		} else {
			output_initialize_compound (p, p->var);
		}
		break;
	case INITIALIZE_COMPOUND:
		output_initialize_compound (p, p->var);
		break;
	}
}

/*
 * SEARCH
 */

/* MIGRATION (C -> Python): emits a Python integer expression for the number
   of occurrences -- either the OCCURS DEPENDING ON count (via output_integer)
   or the literal maximum.  Already valid Python; used as a sub-expression. */
static void
output_occurs (struct cb_field *p)
{
	if (p->occurs_depending) {
		output_integer (p->occurs_depending);
	} else {
		output ("%d", p->occurs_max);
	}
}

static void
output_search_whens (cb_tree table, cb_tree var, cb_tree stmt, cb_tree whens)
{
	cb_tree		l;
	struct cb_field *p;
	cb_tree		idx = NULL;

	p = cb_field (table);
	/* Determine the index to use */
	if (var) {
		for (l = p->index_list; l; l = CB_CHAIN (l)) {
			if (cb_ref (CB_VALUE (l)) == cb_ref (var)) {
				idx = var;
			}
		}
	}
	if (!idx) {
		idx = CB_VALUE (p->index_list);
	}

	/* Start loop */
	/* MIGRATION (C -> Python): "for (;;) { ... }" -> "while True:" suite. */
	output_line ("while True:");
	output_block_open ();

	/* End test */
	output_prefix ();
	output ("if (");
	output_integer (idx);
	output (" > ");
	output_occurs (p);
	output ("):\n");
	output_block_open ();
	if (stmt) {
		output_stmt (stmt);
	}
	output_line ("break");
	output_block_close ();

	/* WHEN test */
	/* MIGRATION (C -> Python): output_stmt(whens) emits the WHEN tests as a
	   flat Python if/elif chain (see output_stmt IF handling); the "else:"
	   appended here therefore binds to that chain and runs when no WHEN
	   matched -- bumping the index and continuing the search loop. */
	output_stmt (whens);
	output_line ("else:");
	output_block_open ();
	/* MIGRATION (C -> Python): "idx++" has no Python form on a COBOL index;
	   emit the runtime SETTER move.cob_set_int(field, current + 1) (the
	   current value is read by output_integer via move.cob_get_int). */
	output_prefix ();
	output ("move.cob_set_int (");
	output_param (idx, -1);
	output (", ");
	output_integer (idx);
	output (" + 1)\n");
	if (var && var != idx) {
		output_move (idx, var);
	}
	output_line ("continue");
	output_block_close ();
	output_line ("break");
	output_block_close ();
}

static void
output_search_all (cb_tree table, cb_tree stmt, cb_tree cond, cb_tree when)
{
	struct cb_field *p;
	cb_tree		idx;

	p = cb_field (table);
	idx = CB_VALUE (p->index_list);
	/* Header */
	/* MIGRATION (C -> Python): the C bare scope "{ int ret; int head;
	   int tail; ... }" becomes plain Python locals -- Python has no block
	   scope, so no brace/indent is emitted for the scope itself.  "ret" is
	   created by the walrus in output_cond(cond, 1); "head"/"tail" are plain
	   Python ints.  The binary-search midpoint divides two provably
	   non-negative operands, so Python floor "//" equals C truncation. */
	output_line ("head = %d - 1", p->occurs_min);
	output_prefix ();
	output ("tail = ");
	output_occurs (p);
	output (" + 1\n");

	/* Start loop */
	output_line ("while True:");
	output_block_open ();

	/* End test */
	output_line ("if (head >= tail - 1):");
	output_block_open ();
	if (stmt) {
		output_stmt (stmt);
	}
	output_line ("break");
	output_block_close ();

	/* Next index */
	output_prefix ();
	output ("move.cob_set_int (");
	output_param (idx, -1);
	output (", (head + tail) // 2)\n");

	/* WHEN test */
	output_prefix ();
	output ("if (");
	output_cond (cond, 1);
	output ("):\n");
	output_block_open ();
	output_stmt (when);
	output_block_close ();
	output_line ("else:");
	output_block_open ();
	output_line ("if (ret < 0):");
	output_block_open ();
	output_prefix ();
	output ("head = ");
	output_integer (idx);
	output ("\n");
	output_block_close ();
	output_line ("else:");
	output_block_open ();
	output_prefix ();
	output ("tail = ");
	output_integer (idx);
	output ("\n");
	output_block_close ();
	output_line ("continue");
	output_block_close ();
	output_line ("break");
	output_block_close ();
}

static void
output_search (struct cb_search *p)
{
	if (p->flag_all) {
		output_search_all (p->table, p->end_stmt,
				   CB_IF (p->whens)->test, CB_IF (p->whens)->stmt1);
	} else {
		output_search_whens (p->table, p->var, p->end_stmt, p->whens);
	}
}

/*
 * CALL
 */

static void
output_call (struct cb_call *p)
{
	cb_tree			x;
	cb_tree			l;
	struct cb_literal	*lp;
	char			*callp;
	struct cb_field		*f;
	char			*system_call = NULL;
	struct system_table	*psyst;
	size_t			n;
	size_t			parmnum;
	size_t			retptr;
	int			dynamic_link = 1;
	int			sizes;
	/* MIGRATION (C -> Python): BY VALUE width/signedness, formerly expressed
	   as a C cast "(unsigned short)(...)", are now passed to the runtime
	   helper call.cob_value_int(value, nbytes, unsigned_flag). */
	int			vbytes;
	int			vunsigned;

	retptr = 0;
	if (p->returning && CB_TREE_CLASS(p->returning) == CB_CLASS_POINTER) {
		retptr = 1;
	}
	/* System routine entry points */
	if (p->is_system) {
		lp = CB_LITERAL (p->name);
		psyst = (struct system_table *)&system_tab[0];
		for (; psyst->syst_name; psyst++) {
			if (!strcmp((const char *)lp->data,
			    (const char *)psyst->syst_name)) {
				system_call = (char *)psyst->syst_call;
				dynamic_link = 0;
				break;
			}
		}
	}

	if (cb_flag_static_call && CB_LITERAL_P (p->name)) {
		dynamic_link = 0;
	}

	/* Local variables */
	/* MIGRATION (C -> Python): the former C bare scope "{ ... }" that
	   declared content_N unions / ptr_N pointers / temptr is dropped --
	   Python has no block scope and needs no pre-declaration; the temporary
	   argument copies below become ordinary Python locals created on first
	   assignment. */

	if (CB_REFERENCE_P (p->name)
	    && CB_FIELD_P (CB_REFERENCE (p->name)->value)
	    && CB_FIELD (CB_REFERENCE (p->name)->value)->usage == CB_USAGE_PROGRAM_POINTER) {
		dynamic_link = 0;
	}

	/* Setup arguments */
	/* MIGRATION (C -> Python): the C declaration pass is skipped entirely;
	   only the value-building assignments are emitted.  BY CONTENT / BY
	   REFERENCE temporaries become Python objects:
	     - a numeric literal or expression passed by reference/content is
	       materialised into a writable native-endian buffer via
	       call.cob_content_int(value, fits_int);
	     - a data item passed BY CONTENT is copied into a fresh writable
	       buffer via call.cob_content_buffer(data, size) so the callee
	       cannot mutate the caller's storage (correct BY CONTENT semantics);
	     - a CAST (e.g. ADDRESS OF) yields a pointer value stored in ptr_N. */
	for (l = p->args, n = 1; l; l = CB_CHAIN (l), n++) {
		x = CB_VALUE (l);
		switch (CB_PURPOSE_INT (l)) {
		case CB_CALL_BY_REFERENCE:
			if (CB_NUMERIC_LITERAL_P (x)) {
				output_prefix ();
				output ("content_%d = call.cob_content_int (", (int)n);
				if (cb_fits_int (x)) {
					output ("%d, 1)\n", cb_get_int (x));
				} else {
					output ("%lldLL, 0)\n", cb_get_long_long (x));
				}
			} else if (CB_BINARY_OP_P (x)) {
				output_prefix ();
				output ("content_%d = call.cob_content_int (", (int)n);
				output_integer (x);
				output (", 1)\n");
			} else if (CB_CAST_P (x)) {
				output_prefix ();
				output ("ptr_%d = ", (int)n);
				output_integer (x);
				output ("\n");
			}
			break;
		case CB_CALL_BY_CONTENT:
			if (CB_CAST_P (x)) {
				output_prefix ();
				output ("ptr_%d = ", (int)n);
				output_integer (x);
				output ("\n");
			} else if (CB_TREE_TAG (x) != CB_TAG_INTRINSIC &&
			    x != cb_null && !(CB_CAST_P (x))) {
				if (CB_NUMERIC_LITERAL_P (x)) {
					output_prefix ();
					output ("content_%d = call.cob_content_int (", (int)n);
					if (cb_fits_int (x)) {
						output ("%d, 1)\n", cb_get_int (x));
					} else {
						output ("%lldLL, 0)\n", cb_get_long_long (x));
					}
				} else if (CB_REF_OR_FIELD_P (x) &&
				    CB_TREE_CATEGORY (x) == CB_CATEGORY_NUMERIC &&
				    cb_field (x)->usage == CB_USAGE_LENGTH) {
					output_prefix ();
					output ("content_%d = call.cob_content_int (", (int)n);
					output_integer (x);
					output (", 1)\n");
				} else {
					output_prefix ();
					output ("content_%d = call.cob_content_buffer (", (int)n);
					output_data (x);
					output (", ");
					output_size (x);
					output (")\n");
				}
			}
			break;
		}
	}

	/* Function name / parameter table */
	/* MIGRATION (C -> Python): "module.cob_procedure_parameters[N]" is a
	   member of the per-program cob_module object (NOT the runtime global),
	   so it keeps its "module." qualifier verbatim; the global cob_call_params
	   is exposed by the runtime facade as common.cob_call_params.  NULL
	   becomes Python None. */
	n = 0;
	for (l = p->args; l; l = CB_CHAIN (l), n++) {
		x = CB_VALUE (l);
		field_iteration = (int) n;
		output_prefix ();
		output ("module.cob_procedure_parameters[%d] = ", (int)n);
		switch (CB_TREE_TAG (x)) {
		case CB_TAG_LITERAL:
		case CB_TAG_FIELD:
		case CB_TAG_INTRINSIC:
			output_param (x, -1);
			break;
		case CB_TAG_REFERENCE:
			switch (CB_TREE_TAG (CB_REFERENCE(x)->value)) {
			case CB_TAG_LITERAL:
			case CB_TAG_FIELD:
			case CB_TAG_INTRINSIC:
				output_param (x, -1);
				break;
			default:
				output ("None");
				break;
			}
			break;
		default:
			output ("None");
			break;
		}
		output ("\n");
	}
	for (parmnum = n; parmnum < n + 4; parmnum++) {
		output_line ("module.cob_procedure_parameters[%d] = None", (int)parmnum);
	}
	parmnum = n;
	output_line ("common.cob_call_params = %d", (int)n);

	/* Resolve the target into a single Python callable "_unifunc". */
	/* MIGRATION (C -> Python): the C union machinery (cob_unifunc /
	   call_<name> with overlapping funcptr/funcint/func_void members)
	   collapses to one Python variable holding a callable -- a Python
	   COBOL program / system routine returns its int return-code (or a
	   pointer) directly, so no type-punning union is needed.  Python has
	   no static linking, so even a "static" literal CALL is resolved
	   dynamically through call.cob_resolve (importlib's module cache makes
	   this inexpensive); the *_1 resolver variants abort on failure (used
	   when there is no ON OVERFLOW/EXCEPTION handler) while the non-_1
	   variants return None (used so the handler can test for it). */
	if (!dynamic_link) {
		if (CB_REFERENCE_P (p->name) &&
		    CB_FIELD_P (CB_REFERENCE (p->name)->value) &&
		    CB_FIELD (CB_REFERENCE (p->name)->value)->usage ==
		    CB_USAGE_PROGRAM_POINTER) {
			/* PROGRAM POINTER: the field already holds a callable. */
			output_prefix ();
			output ("_unifunc = ");
			output_integer (p->name);
			output ("\n");
		} else if (system_call) {
			/* System routine: a function in libcob_py.system. */
			output_line ("_unifunc = system.%s", system_call);
		} else {
			/* Static literal CALL -> resolved dynamically in Python. */
			output_line ("_unifunc = call.cob_resolve (\"%s\")",
				     (char *)(CB_LITERAL (p->name)->data));
		}
	} else {
		/* Dynamic link */
		if (CB_LITERAL_P (p->name)) {
			callp = cb_encode_program_id ((char *)(CB_LITERAL (p->name)->data));
			/* Registration retained for the call cache (its module-level
			   declaration is a no-op under the Python backend). */
			lookup_call (callp);
			if (!p->stmt1) {
				output_line ("_unifunc = call.cob_resolve_1 (\"%s\")",
					     (char *)(CB_LITERAL (p->name)->data));
			} else {
				output_line ("_unifunc = call.cob_resolve (\"%s\")",
					     (char *)(CB_LITERAL (p->name)->data));
			}
		} else {
			callp = NULL;
			output_prefix ();
			output ("_unifunc = ");
			if (!p->stmt1) {
				output_funcall (cb_build_funcall_1 (
						"cob_call_resolve_1", p->name));
			} else {
				output_funcall (cb_build_funcall_1 (
						"cob_call_resolve", p->name));
			}
			output ("\n");
		}
	}

	/* ON OVERFLOW / ON EXCEPTION handler (dynamic link only). */
	if (p->stmt1) {
		output_line ("if (_unifunc is None):");
		output_block_open ();
		output_stmt (p->stmt1);
		output_block_close ();
		output_line ("else:");
		output_block_open ();
	}

	/* The call itself + return-code / RETURNING-pointer capture. */
	/* MIGRATION (C -> Python): "<retcode> = funcptr (args)" becomes the
	   runtime SETTER move.cob_set_int / move.cob_set_pointer applied to the
	   call result.  The callee contract is that every emitted COBOL program
	   (and system routine) returns an int return-code (default 0). */
	output_prefix ();
	if (retptr) {
		output ("move.cob_set_pointer (");
		output_param (p->returning, -1);
		output (", _unifunc (");
	} else {
		output ("move.cob_set_int (");
		output_param (current_prog->cb_return_code, -1);
		output (", _unifunc (");
	}

	/* Arguments */
	for (l = p->args, n = 1; l; l = CB_CHAIN (l), n++) {
		x = CB_VALUE (l);
		switch (CB_PURPOSE_INT (l)) {
		case CB_CALL_BY_REFERENCE:
			if (CB_NUMERIC_LITERAL_P (x) || CB_BINARY_OP_P (x)) {
				output ("content_%d", (int)n);
			} else if (CB_REFERENCE_P (x) && CB_FILE_P (cb_ref (x))) {
				output_param (cb_ref (x), -1);
			} else if (CB_CAST_P (x)) {
				/* MIGRATION (C -> Python): the C "&ptr_N" passed a
				   pointer-to-pointer so the callee could write the
				   pointer back; Python passes the pointer value
				   ptr_N directly (pointer write-back through a CALL
				   argument is not modeled -- a documented edge case
				   left unchanged per the minimal-change clause). */
				output ("ptr_%d", (int)n);
			} else {
				output_data (x);
			}
			break;
		case CB_CALL_BY_CONTENT:
			if (CB_TREE_TAG (x) != CB_TAG_INTRINSIC && x != cb_null) {
				if (CB_CAST_P (x)) {
					output ("ptr_%d", (int)n);
				} else {
					output ("content_%d", (int)n);
				}
			} else {
				output_data (x);
			}
			break;
		case CB_CALL_BY_VALUE:
			if (CB_TREE_TAG (x) != CB_TAG_INTRINSIC) {
				switch (CB_TREE_TAG (x)) {
				case CB_TAG_CAST:
					output_integer (x);
					break;
				case CB_TAG_LITERAL:
					if (CB_TREE_CLASS (x) == CB_CLASS_NUMERIC) {
						output ("%d", cb_get_int (x));
					} else {
						output ("%d", CB_LITERAL (x)->data[0]);
					}
					break;
				default:
/* RXWRXW
					if (CB_TREE_CLASS (x) == CB_CLASS_NUMERIC) {
						output_integer (x);
					} else {
						output ("*(");
						output_data (x);
						output (")");
					}
*/
					f = cb_field (x);
					switch (f->usage) {
					case CB_USAGE_BINARY:
					case CB_USAGE_COMP_5:
					case CB_USAGE_COMP_X:
					/* RXWRXW */
					case CB_USAGE_PACKED:
					case CB_USAGE_DISPLAY:
						sizes = CB_SIZES_INT (l);
						if (sizes == CB_SIZE_AUTO) {
							/* MIGRATION (C -> Python): the original
							   C selects an UNSIGNED cast when the
							   PICTURE has a sign here; this looks
							   counter-intuitive but is preserved
							   verbatim for byte-for-byte parity with
							   the C toolchain. */
							if (f->pic->have_sign) {
								vunsigned = 1;
							} else {
								vunsigned = 0;
							}
							if (f->usage == CB_USAGE_PACKED ||
							    f->usage == CB_USAGE_DISPLAY) {
								sizes = f->pic->digits - f->pic->scale;
							} else {
								sizes = f->size;
							}
							switch (sizes) {
							case 0:
								sizes = CB_SIZE_4;
								break;
							case 1:
								sizes = CB_SIZE_1;
								break;
							case 2:
								sizes = CB_SIZE_2;
								break;
							case 3:
								sizes = CB_SIZE_4;
								break;
							case 4:
								sizes = CB_SIZE_4;
								break;
							case 5:
								sizes = CB_SIZE_8;
								break;
							case 6:
								sizes = CB_SIZE_8;
								break;
							case 7:
								sizes = CB_SIZE_8;
								break;
							default:
								sizes = CB_SIZE_8;
								break;
							}
						} else {
							if (CB_SIZES_INT_UNSIGNED(l)) {
								vunsigned = 1;
							} else {
								vunsigned = 0;
							}
						}
						switch (sizes) {
						case CB_SIZE_1:
							vbytes = 1;
							break;
						case CB_SIZE_2:
							vbytes = 2;
							break;
						case CB_SIZE_4:
							vbytes = 4;
							break;
						case CB_SIZE_8:
							vbytes = 8;
							break;
						default:
							vbytes = 4;
							break;
						}
						output ("call.cob_value_int (");
						output_integer (x);
						output (", %d, %d)", vbytes, vunsigned);
						break;
					case CB_USAGE_INDEX:
					case CB_USAGE_LENGTH:
					case CB_USAGE_POINTER:
					case CB_USAGE_PROGRAM_POINTER:
						output_integer (x);
						break;
					default:
						/* MIGRATION (C -> Python): C dereferenced
						   the first data byte "*(data)"; Python
						   reads element 0 of the memoryview. */
						output_data (x);
						output ("[0]");
						break;
					}
					break;
				}
			} else {
				output_data (x);
			}
			break;
		}
		if (CB_CHAIN (l)) {
			output (", ");
		}
	}
	if (!system_call) {
		if (cb_sticky_linkage || cb_flag_null_param) {
			for (n = 0; n < 4; n++) {
				if (n != 0 || parmnum != 0) {
					output (", ");
				}
				output ("None");
			}
		}
	}
	/* Close "_unifunc (" and the enclosing move.cob_set_* (". */
	output ("))\n");
	if (p->returning) {
		if (!retptr) {
			/* suppress warnings */
			suppress_warn = 1;
			output_stmt (cb_build_move (current_prog->cb_return_code,
						    p->returning));
			suppress_warn = 0;
#ifdef	COB_NON_ALIGNED
		} else {
			output_prefix ();
			output ("common.memcpy (");
			output_data (p->returning);
			output (", common.cob_addr_of (temptr), %d)\n", sizeof (void *));
#endif
		}
	}
	if (p->stmt2) {
		output_stmt (p->stmt2);
	}
	if (dynamic_link && p->stmt1) {
		/* MIGRATION (C -> Python): close the ON OVERFLOW "else:" suite. */
		output_block_close ();
	}
	/* MIGRATION (C -> Python): the former bare-scope close is dropped (no
	   Python block was opened for it). */
}

/*
 * GO TO
 */

static void
output_goto_1 (cb_tree x)
{
	/* MIGRATION (C -> Python): "goto l_N;" becomes "raise _CobGoto(<id>)".
	   A Python exception is used instead of "_pc = <id>; continue" because a
	   GO TO may appear nested inside an inline PERFORM for/while loop, where a
	   bare "continue" would target that inner loop rather than the outer
	   _dispatch loop.  The exception escapes any nesting and is caught by the
	   _dispatch "except _CobGoto" handler, which resumes at the target. */
	output_line ("raise _CobGoto (%d)", CB_LABEL (cb_ref (x))->id);
}

static void
output_goto (struct cb_goto *p)
{
	cb_tree l;
	int	i = 1;

	if (p->depending) {
		/* MIGRATION (C -> Python): GO TO ... DEPENDING ON n.  The C "switch"
		   over the 1-based selector becomes an if/elif chain that raises
		   _CobGoto for the chosen target; an out-of-range selector matches no
		   branch and falls through, matching COBOL semantics. */
		output_prefix ();
		output ("_dep%d = ", loop_counter);
		output_param (cb_build_cast_integer (p->depending), 0);
		output ("\n");
		for (l = p->target; l; l = CB_CHAIN (l)) {
			if (i == 1) {
				output_line ("if _dep%d == %d:", loop_counter, i);
			} else {
				output_line ("elif _dep%d == %d:", loop_counter, i);
			}
			output_block_open ();
			output_goto_1 (CB_VALUE (l));
			output_block_close ();
			i++;
		}
		loop_counter++;
	} else if (p->target == NULL) {
		/* MIGRATION (C -> Python): implicit GOBACK at end / GO TO with no
		   target.  "goto exit_program" becomes "raise _CobExit()", which
		   propagates through every nested _dispatch level (the per-level
		   _CobGoto handler does NOT catch it) up to the program-function
		   wrapper that performs module-pop cleanup and returns the
		   return-code.  When not implicit-init, a main program (module.next is
		   None) falls through instead of exiting, as in the C original. */
		needs_exit_prog = 1;
		if (cb_flag_implicit_init) {
			output_line ("raise _CobExit ()");
		} else {
			output_line ("if module.next is not None:");
			output_block_open ();
			output_line ("raise _CobExit ()");
			output_block_close ();
		}
	} else if (p->target == cb_int1) {
		/* MIGRATION (C -> Python): EXIT PROGRAM -> raise _CobExit(). */
		needs_exit_prog = 1;
		output_line ("raise _CobExit ()");
	} else {
		output_goto_1 (p->target);
	}
}

/*
 * PERFORM
 */

static void
output_perform_call (struct cb_label *lb, struct cb_label *le)
{
	/* MIGRATION (C -> Python): PERFORM <lb> [THRU <le>] becomes a synchronous
	   recursive dispatch call "_dispatch(<lb.id>, <le.id>)".  The Python call
	   stack supplies the frame stack the C backend maintained explicitly
	   (frame_ptr++/--, return_address, perform_through): the call runs the
	   paragraph range and returns when the range's THRU paragraph reaches its
	   exit (see output_perform_exit).  No frame push/pop, no return address,
	   and no synthetic resume label are required.  The former cb_flag_stack_check
	   overflow guard is superseded by CPython's own recursion limit (raised in
	   the emitted module preamble), which raises RecursionError on overflow. */
	if (lb == le) {
		output_comment ("PERFORM %s", lb->name);
	} else {
		output_comment ("PERFORM %s THRU %s", lb->name, le->name);
	}
	output_line ("_dispatch (%d, %d)", lb->id, le->id);
}

static void
output_perform_exit (struct cb_label *l)
{
	if (l->is_global) {
		/* MIGRATION (C -> Python): a GLOBAL (declaratives) paragraph's exit
		   checks the active entry point and, on match, pops the module and
		   returns from the current dispatch.  "_entry" is established by the
		   program-function wrapper. */
		output_newline ();
		output_line ("if _entry == %d:", l->id);
		output_block_open ();
		if (cb_flag_traceall) {
			output_line ("common.cob_reset_trace ()");
		}
		/* Fixme - Check module push/pop */
		output_line ("common.cob_current_module = common.cob_current_module.next");
		output_line ("return 0");
		output_block_close ();
	}
	if (!cb_perform_osvs) {
		/* MIGRATION (C -> Python): non-OS/VS perform return.  When this
		   paragraph is the THRU target of the active PERFORM (_through), return
		   from the current _dispatch invocation, which unwinds to the PERFORM
		   call site.  "return" is robust at any nesting depth, replacing the C
		   "goto *frame_ptr->return_address". */
		output_newline ();
		output_line ("if _through == %d:", l->id);
		output_block_open ();
		output_line ("return 0");
		output_block_close ();
	} else {
		/* MIGRATION (C -> Python): OS/VS perform return.  The C backend
		   searched the whole frame stack for a matching perform_through and
		   unwound to it.  The recursive model reproduces multi-level unwinding
		   by raising _CobPerformExit(<id>); each _dispatch level consumes it
		   when its _through matches and otherwise re-raises (handler emitted by
		   the program-function wrapper). */
		output_line ("raise _CobPerformExit (%d)", l->id);
	}
}

static void
output_perform_once (struct cb_perform *p)
{
	if (p->body && CB_PAIR_P (p->body)) {
		output_perform_call (CB_LABEL (cb_ref (CB_PAIR_X (p->body))),
				     CB_LABEL (cb_ref (CB_PAIR_Y (p->body))));
	} else {
		output_stmt (p->body);
	}
	if (p->cycle_label) {
		output_stmt (cb_ref (p->cycle_label));
	}
}

static void
output_perform_until (struct cb_perform *p, cb_tree l)
{
	struct cb_perform_varying	*v;
	cb_tree				next;

	if (l == NULL) {
		/* Perform body at the end */
		output_perform_once (p);
		return;
	}

	v = CB_PERFORM_VARYING (CB_VALUE (l));
	next = CB_CHAIN (l);

	/* MIGRATION (C -> Python): "for (;;) { ... }" -> "while True:" suite. */
	output_line ("while True:");
	output_block_open ();

	if (next && CB_PERFORM_VARYING (CB_VALUE (next))->name) {
		output_move (CB_PERFORM_VARYING (CB_VALUE (next))->from,
			     CB_PERFORM_VARYING (CB_VALUE (next))->name);
	}

	if (p->test == CB_AFTER) {
		output_perform_until (p, next);
	}

	/* MIGRATION (C -> Python): "if (cond) break;" -> "if cond:" suite with
	   "break".  output_cond emits a complete Python boolean expression. */
	output_prefix ();
	output ("if ");
	output_cond (v->until, 0);
	output (":\n");
	output_block_open ();
	output_line ("break");
	output_block_close ();

	if (p->test == CB_BEFORE) {
		output_perform_until (p, next);
	}

	if (v->step) {
		output_stmt (v->step);
	}

	output_block_close ();
}

static void
output_perform (struct cb_perform *p)
{
	struct cb_perform_varying *v;

	switch (p->type) {
	case CB_PERFORM_EXIT:
		if (CB_LABEL (p->data)->need_return) {
			output_perform_exit (CB_LABEL (p->data));
		}
		break;
	case CB_PERFORM_ONCE:
		output_perform_once (p);
		break;
	case CB_PERFORM_TIMES:
		/* MIGRATION (C -> Python): "for (n = COUNT; n > 0; n--)" runs COUNT
		   times -> "for _nN in range(COUNT):" (same iteration count, and a
		   non-positive COUNT yields zero iterations exactly as the C did).
		   The loop variable is unused; only the repetition count matters. */
		output_prefix ();
		output ("for _n%d in range (", loop_counter);
		output_param (cb_build_cast_integer (p->data), 0);
		output ("):\n");
		loop_counter++;
		output_block_open ();
		output_perform_once (p);
		output_block_close ();
		break;
	case CB_PERFORM_UNTIL:
		v = CB_PERFORM_VARYING (CB_VALUE (p->varying));
		if (v->name) {
			output_move (v->from, v->name);
		}
		output_perform_until (p, p->varying);
		break;
	case CB_PERFORM_FOREVER:
		/* MIGRATION (C -> Python): "for (;;) { ... }" -> "while True:". */
		output_line ("while True:");
		output_block_open ();
		output_perform_once (p);
		output_block_close ();
		break;
	}
	if (p->exit_label) {
		output_stmt (cb_ref (p->exit_label));
	}
}

static void
output_file_error (struct cb_file *pfile)
{
	struct cb_file		*fl;
	cb_tree			l;

	for (l =  current_prog->local_file_list; l; l = CB_CHAIN (l)) {
		fl = CB_FILE(CB_VALUE (l));
		if (!strcmp (pfile->name, fl->name)) {
			output_perform_call (fl->handler,
					     fl->handler);
			return;
		}
	}
	for (l =  current_prog->global_file_list; l; l = CB_CHAIN (l)) {
		fl = CB_FILE(CB_VALUE (l));
		if (!strcmp (pfile->name, fl->name)) {
			if (fl->handler_prog == current_prog) {
				output_perform_call (fl->handler,
						     fl->handler);
			} else {
				/* MIGRATION (C -> Python): a GLOBAL error handler that
				   resides in ANOTHER program is invoked by calling that
				   program's internal function.  In the Python dispatch
				   model the internal function's entry argument IS the
				   starting label id (_pc), so the handler's label id is
				   passed directly -- the same value the C convention used
				   ("%s_ (%d)").  The trailing C ";" is dropped; the trace
				   toggles become module-qualified runtime calls. */
				if (cb_flag_traceall) {
					output_line ("common.cob_reset_trace ()");
				}
				output_line ("%s_ (%d)",
					fl->handler_prog->program_id,
					fl->handler->id);
				if (cb_flag_traceall) {
					output_line ("common.cob_ready_trace ()");
				}
			}
			return;
		}
	}
	output_perform_call (pfile->handler, pfile->handler);
}

/*
 * Output statement
 */

static void
output_ferror_stmt (struct cb_statement *p, int code)
{
	/* MIGRATION (C -> Python): the file-I/O exception dispatch.  The C form
	   "if (unlikely(cob_exception_code != 0)) { [if <code>: h1 else: file_err]
	   | file_err } else { h3; h2 }" is re-expressed with the indent-block
	   model: unlikely() is dropped, the global exception code is read as
	   common.cob_exception_code, and the C braces become Python suites.  When
	   a specific handler1 is present the inner code-match selects between
	   handler1 and the file's error routine; otherwise the error routine runs
	   unconditionally.  handler2/handler3 (the NOT.../no-exception path) form
	   the outer "else:". */
	output_line ("if (common.cob_exception_code != 0):");
	output_block_open ();
	if (p->handler1) {
		if ((code & 0x00ff) == 0) {
			output_line ("if ((common.cob_exception_code & 0xff00) == 0x%04x):",
			     code);
		} else {
			output_line ("if (common.cob_exception_code == 0x%04x):", code);
		}
		output_block_open ();
		output_stmt (p->handler1);
		output_block_close ();
		output_line ("else:");
		output_block_open ();
		output_file_error (CB_FILE (p->file));
		output_block_close ();
	} else {
		output_file_error (CB_FILE (p->file));
	}
	output_block_close ();
	if (p->handler2 || p->handler3) {
		output_line ("else:");
		output_block_open ();
		if (p->handler3) {
			output_stmt (p->handler3);
		}
		if (p->handler2) {
			output_stmt (p->handler2);
		}
		output_block_close ();
	}
}

static void
output_stmt (cb_tree x)
{
	struct cb_statement	*p;
	struct cb_label		*lp;
	struct cb_assign	*ap;
	struct cb_if		*ip;
	/* MIGRATION (C -> Python): cp and f are now used unconditionally by the
	   rewritten ASSIGN case (SET ADDRESS / pointer / numeric store dispatch),
	   so they are no longer guarded by COB_NON_ALIGNED. */
	struct cb_cast		*cp;
	struct cb_field		*f;
	int			code;

	stack_id = 0;
	if (x == NULL) {
		/* MIGRATION (C -> Python): an empty C statement ";" becomes Python
		   "pass" so the enclosing suite is never empty. */
		output_line ("pass");
		return;
	}
#ifndef __GNUC__
	if (inside_check != 0) {
		if (inside_stack[inside_check - 1] != 0) {
			inside_stack[inside_check -1] = 0;
			output (",\n");
		}
	}
#endif

	switch (CB_TREE_TAG (x)) {
	case CB_TAG_STATEMENT:
		p = CB_STATEMENT (x);
		/* MIGRATION (C -> Python): the C source-provenance comment carrying
		   "file:line: name" is emitted as a Python "# file:line: name"
		   comment via output_comment
		   (which does NOT mark block content, so a statement that emits only
		   this comment still yields a valid suite). */
		if (p->name) {
			output_comment ("%s:%d: %s",
					x->source_file, x->source_line, p->name);
		}
		/* Output source location as a runtime call */
		if (x->source_file && last_line != x->source_line) {
			if (cb_flag_source_location) {
				/* MIGRATION (C -> Python): cob_set_location ->
				   common.cob_set_location; C NULL -> Python None; no
				   trailing ';'. */
				output_prefix ();
				output ("common.cob_set_location (\"%s\", \"%s\", %d, ",
					excp_current_program_id, x->source_file,
					x->source_line);
				if (excp_current_section) {
					output ("\"%s\", ", excp_current_section);
				} else {
					output ("None, ");
				}
				if (excp_current_paragraph) {
					output ("\"%s\", ", excp_current_paragraph);
				} else {
					output ("None, ");
				}
				if (p->name) {
					output ("\"%s\")\n", p->name);
				} else {
					output ("None)\n");
				}
			}
			last_line = x->source_line;
		}

		if (p->handler1 || p->handler2 || (p->file && CB_EXCEPTION_ENABLE (COB_EC_I_O))) {
			/* MIGRATION (C -> Python): clear the runtime exception register
			   (a module-level global on the common facade). */
			output_line ("common.cob_exception_code = 0");
		}

		if (p->null_check) {
			output_stmt (p->null_check);
		}

		if (p->body) {
			output_stmt (p->body);
		}

		if (p->handler1 || p->handler2 || (p->file && CB_EXCEPTION_ENABLE (COB_EC_I_O))) {
			code = CB_EXCEPTION_CODE (p->handler_id);
			if (p->file) {
				output_ferror_stmt (p, code);
			} else {
				/* MIGRATION (C -> Python): exception handlers test the
				   common.cob_exception_code register; the C "unlikely()"
				   branch hint is dropped, the masked/exact comparisons map
				   directly, and blocks use Python suites. */
				if (p->handler1) {
					if ((code & 0x00ff) == 0) {
						output_line ("if (common.cob_exception_code & 0xff00) == 0x%04x:",
						     code);
					} else {
						output_line ("if common.cob_exception_code == 0x%04x:", code);
					}
					output_block_open ();
					output_stmt (p->handler1);
					output_block_close ();
					if (p->handler2) {
						output_line ("else:");
						output_block_open ();
					}
				}
				if (p->handler2) {
					if (p->handler1 == NULL) {
						output_line ("if not common.cob_exception_code:");
						output_block_open ();
					}
					output_stmt (p->handler2);
					output_block_close ();
				}
			}
		}
		break;
	case CB_TAG_LABEL:
		/* MIGRATION (C -> Python): a paragraph/section LABEL becomes a segment
		   of the recursive dispatch loop emitted by output_internal_function.
		   The C emitter laid paragraphs out as a flat instruction stream with
		   "l_<id>:;" labels and relied on natural fall-through plus computed
		   "goto".  Python has neither labels nor goto, so a need_begin label
		   (i.e. a PERFORM/GO TO target) instead starts a new
		   "if _pc == <id>:" dispatch segment.  Before opening it, any segment
		   already open is terminated with a fall-through "_pc = <id>" +
		   "continue" so control flows into this label exactly as the C
		   fall-through past "l_<id>:;" did.  A non-need_begin label is not a
		   jump target, so it emits only a provenance comment and stays inside
		   the current segment (statements before and after it share the same
		   fall-through region, matching the C behaviour).  Provenance is
		   emitted with output_comment(), which does NOT mark block content, so
		   a segment that ends up holding only a comment still receives an
		   auto-"pass" from output_block_close(). */
		lp = CB_LABEL (x);
		/* Close the previously open segment with a fall-through into this
		   label when the label is a jump target. */
		if (lp->need_begin && output_segment_open) {
			output_line ("_pc = %d", lp->id);
			output_line ("continue");
			output_block_close ();
			output_segment_open = 0;
		}
		output_newline ();
		/* Provenance comment (does not mark block content) + exception
		   bookkeeping, identical in spirit to the C provenance lines. */
		if (lp->is_section) {
			if (strcmp ((const char *)(lp->name) , "MAIN SECTION")) {
				output_comment ("%s SECTION", lp->name);
			} else {
				output_comment ("%s", lp->name);
			}
			excp_current_section = (const char *)lp->name;
			excp_current_paragraph = NULL;
		} else {
			if (lp->is_entry) {
				output_comment ("Entry %s", lp->orig_name);
			} else {
				output_comment ("%s", lp->name);
			}
			excp_current_paragraph = (const char *)lp->name;
		}
		/* Open a new dispatch segment for jump targets (need_begin). */
		if (lp->need_begin) {
			output_line ("if _pc == %d:", lp->id);
			output_block_open ();
			output_segment_open = 1;
		}
		if (cb_flag_trace) {
			/* Trace runs when the label is reached -> inside the segment.
			   C "fputs (..., stderr); fflush (stderr);" becomes
			   "sys.stderr.write (...); sys.stderr.flush ()". */
			if (lp->is_section) {
				if (strcmp ((const char *)(lp->name) , "MAIN SECTION")) {
					output_line ("sys.stderr.write (\"PROGRAM-ID: %s: %s SECTION\\n\")", excp_current_program_id, lp->orig_name);
				} else {
					output_line ("sys.stderr.write (\"PROGRAM-ID: %s: %s\\n\")", excp_current_program_id, lp->orig_name);
				}
			} else if (lp->is_entry) {
				output_line ("sys.stderr.write (\"PROGRAM-ID: %s: ENTRY %s\\n\")", excp_current_program_id, lp->orig_name);
			} else {
				output_line ("sys.stderr.write (\"PROGRAM-ID: %s: %s\\n\")", excp_current_program_id, lp->orig_name);
			}
			output_line ("sys.stderr.flush ()");
		}
		break;
	case CB_TAG_FUNCALL:
		/* MIGRATION (C -> Python): a top-level funcall statement is emitted on
		   its own line as a module-qualified libcob_py call (output_funcall),
		   with the C statement terminator ";" replaced by a bare newline.  The
		   non-__GNUC__ inside_check path (used when a funcall is emitted as an
		   inline check element rather than a standalone statement) is preserved
		   structurally; only the emitted terminator changes. */
		output_prefix ();
		output_funcall (x);
#ifdef __GNUC__
		output ("\n");
#else
		if (inside_check == 0) {
			output ("\n");
		} else {
			inside_stack[inside_check -1] = 1;
		}
#endif
		break;
	case CB_TAG_ASSIGN:
		/* MIGRATION (C -> Python): the C emitter stored an integer via an
		   lvalue assignment "output_integer(var) = output_integer(val);"
		   (output_integer produced a C lvalue -- a dereferenced pointer or
		   cast).  Python has no assignable accessor expressions, so the store
		   is re-expressed as a runtime setter chosen to MIRROR output_integer's
		   read dispatch, so the same storage is written with the same value
		   (byte-for-byte):
		     SET ADDRESS OF x (CAST_ADDRESS) -> common.cob_set_addr (data, val)
		     POINTER usage                   -> common.cob_set_pointer (fld, val)
		     PROGRAM-POINTER usage           -> common.cob_set_prog_pointer (fld, val)
		     numeric / index (default)       -> move.cob_set_int (fld, val)
		   The value expression is produced by output_integer (a Python int /
		   pointer expression).  The former COB_NON_ALIGNED temp-pointer memcpy
		   dance is unnecessary in Python -- the pointer setters handle address
		   assignment directly -- so the two C branches collapse into this one
		   path.  The trailing C ";" becomes a bare newline (the non-__GNUC__
		   inside_check deferral path is preserved structurally). */
		ap = CB_ASSIGN (x);
		output_prefix ();
		if (CB_TREE_TAG (ap->var) == CB_TAG_CAST) {
			/* SET ADDRESS OF var TO val */
			cp = CB_CAST (ap->var);
			if (cp->type != CB_CAST_ADDRESS) {
				fprintf (stderr, "Unexpected tree type %d\n", cp->type);
				ABORT ();
			}
			output ("common.cob_set_addr (");
			output_data (cp->val);
			output (", ");
			output_integer (ap->val);
			output (")");
		} else {
			f = cb_field (ap->var);
			if (f->usage == CB_USAGE_POINTER) {
				output ("common.cob_set_pointer (");
				output_param (ap->var, -1);
				output (", ");
				output_integer (ap->val);
				output (")");
			} else if (f->usage == CB_USAGE_PROGRAM_POINTER) {
				output ("common.cob_set_prog_pointer (");
				output_param (ap->var, -1);
				output (", ");
				output_integer (ap->val);
				output (")");
			} else {
				output ("move.cob_set_int (");
				output_param (ap->var, -1);
				output (", ");
				output_integer (ap->val);
				output (")");
			}
		}
#ifdef __GNUC__
		output ("\n");
#else
		if (inside_check == 0) {
			output ("\n");
		} else {
			inside_stack[inside_check -1] = 1;
		}
#endif
		break;
	case CB_TAG_INITIALIZE:
		output_initialize (CB_INITIALIZE (x));
		break;
	case CB_TAG_SEARCH:
		output_search (CB_SEARCH (x));
		break;
	case CB_TAG_CALL:
		output_call (CB_CALL (x));
		break;
	case CB_TAG_GOTO:
		output_goto (CB_GOTO (x));
		break;
	case CB_TAG_IF:
		/* MIGRATION (C -> Python): "if (cond)\n {s1} else {s2}" becomes the
		   indent-block form "if (cond):\n <s1>\n else:\n <s2>".  The condition
		   keeps its surrounding parentheses ("if (cond):") because output_cond
		   may emit a walrus assignment, which requires parentheses in an
		   if-header.  An empty then-branch emits "pass" (Python forbids an
		   empty suite).
		   CRITICAL: an else-branch that is ITSELF an IF (COBOL "ELSE IF" /
		   chained WHENs) is FLATTENED into an "elif" chain rather than a nested
		   "else: if", because output_search_whens() appends its own "else:"
		   after output_stmt(whens) at the chain's top indent and relies on a
		   flat if/elif structure for that "else:" to bind correctly (a nested
		   "else: if" would already own the else and yield a double-else
		   SyntaxError). */
		ip = CB_IF (x);
		output_prefix ();
		output ("if (");
		output_cond (ip->test, 0);
		output ("):\n");
		output_block_open ();
		if (ip->stmt1) {
			output_stmt (ip->stmt1);
		} else {
			output_line ("pass");
		}
		output_block_close ();
		/* Flatten chained "ELSE IF" into Python "elif". */
		while (ip->stmt2 && CB_TREE_TAG (ip->stmt2) == CB_TAG_IF) {
			ip = CB_IF (ip->stmt2);
			output_prefix ();
			output ("elif (");
			output_cond (ip->test, 0);
			output ("):\n");
			output_block_open ();
			if (ip->stmt1) {
				output_stmt (ip->stmt1);
			} else {
				output_line ("pass");
			}
			output_block_close ();
		}
		if (ip->stmt2) {
			output_line ("else:");
			output_block_open ();
			output_stmt (ip->stmt2);
			output_block_close ();
		}
		break;
	case CB_TAG_PERFORM:
		output_perform (CB_PERFORM (x));
		break;
	case CB_TAG_CONTINUE:
		/* MIGRATION (C -> Python): the COBOL CONTINUE no-op (C empty ";")
		   becomes Python "pass". */
		output_line ("pass");
		break;
	case CB_TAG_LIST:
		/* MIGRATION (C -> Python): a statement LIST is a flat sequence, not a
		   Python suite.  The C bare scope "{ ... }" supplied only C block
		   scope, which is irrelevant in Python, so NO indent block is emitted
		   -- the child statements are emitted in order at the CURRENT indent.
		   If the LIST is empty and is the sole body of an enclosing suite,
		   output_block_close() (called by that enclosing construct) injects the
		   required "pass". */
		for (; x; x = CB_CHAIN (x)) {
			output_stmt (CB_VALUE (x));
		}
		break;
	default:
		fprintf (stderr, "Unexpected tree tag %d\n", CB_TREE_TAG (x));
		ABORT ();
	}
}

/*
 * File definition
 */

static int
output_file_allocation (struct cb_file *f)
{
	/* MIGRATION (C->Python): emit module-level Python file-container declarations.
	   C emitted "static cob_file *h_X = NULL;", a 4-byte status buffer and an
	   optional "static struct cob_file_key *k_X = NULL;".  Python emits late-bound
	   module globals: the file-object placeholder (None until built at run time by
	   output_file_initialization), a 4-byte status bytearray and (for
	   RELATIVE/INDEXED) a key-array placeholder.  Global vs local file scope still
	   selects the storage vs local module buffer; both flush into the one .py. */
	if (f->global) {
		output_storage ("# Global file %s\n", f->name);
	} else {
		output_local ("# File %s\n", f->name);
	}
	/* Output RELATIVE/RECORD KEY's */
	if (f->organization == COB_ORG_RELATIVE || f->organization == COB_ORG_INDEXED) {
		if (f->global) {
			output_storage ("%s%s = None\n", CB_PREFIX_KEYS, f->cname);
		} else {
			output_local ("%s%s = None\n", CB_PREFIX_KEYS, f->cname);
		}
	}
	if (f->global) {
		output_storage ("%s%s = None\n", CB_PREFIX_FILE, f->cname);
		output_storage ("%s%s_status = bytearray (4)\n", CB_PREFIX_FILE, f->cname);
	} else {
		output_local ("%s%s = None\n", CB_PREFIX_FILE, f->cname);
		output_local ("%s%s_status = bytearray (4)\n", CB_PREFIX_FILE, f->cname);
	}
	if (f->linage) {
		return 1;
	}
	return 0;
}
static void
output_file_initialization (struct cb_file *f)
{
	int			nkeys = 1;
	struct cb_alt_key	*l;

	/* MIGRATION (C->Python): emit run-time statements that build the cob_file
	   object and its key array through the libcob_py fileio runtime, replacing
	   the C cob_malloc / pointer-attribute sequence.  The "global" declarations
	   for the h_/k_ handles are emitted once at the top of the enclosing program
	   function, so none are emitted here.  For EXTERNAL files the whole attribute
	   initialization stays guarded by "if common.cob_initial_external:" exactly as
	   the C code guarded it with "if (cob_initial_external) { ... }". */
	if (f->external) {
		output_line ("%s%s = fileio.cob_file_external (\"%s\")",
			     CB_PREFIX_FILE, f->cname, f->cname);
		output_line ("if common.cob_initial_external:");
		output_block_open ();
		if (f->linage) {
			output_line ("%s%s.linorkeyptr = fileio.cob_linage_struct ()", CB_PREFIX_FILE, f->cname);
		}
	} else {
		output_line ("if %s%s is None:", CB_PREFIX_FILE, f->cname);
		output_block_open ();
		output_line ("%s%s = fileio.cob_file ()", CB_PREFIX_FILE, f->cname);
		if (f->linage) {
			output_line ("%s%s.linorkeyptr = fileio.cob_linage_struct ()", CB_PREFIX_FILE, f->cname);
		}
		output_block_close ();
	}
	/* Output RELATIVE/RECORD KEY's */
	if (f->organization == COB_ORG_RELATIVE
	 || f->organization == COB_ORG_INDEXED) {
		for (l = f->alt_key_list; l; l = l->next) {
			nkeys++;
		}
		output_line ("if %s%s is None:", CB_PREFIX_KEYS, f->cname);
		output_block_open ();
		output_line ("%s%s = fileio.cob_file_key_array (%d)",
			     CB_PREFIX_KEYS, f->cname, nkeys);
		output_block_close ();
		nkeys = 1;
		output_prefix ();
		output ("%s%s[0].field = ", CB_PREFIX_KEYS, f->cname);
		output_param (f->key, -1);
		output ("\n");
		output_line ("%s%s[0].flag = 0", CB_PREFIX_KEYS, f->cname);
		if (f->key) {
			output_line ("%s%s[0].offset = %d", CB_PREFIX_KEYS, f->cname,
				cb_field (f->key)->offset);
		} else {
			output_line ("%s%s[0].offset = 0", CB_PREFIX_KEYS, f->cname);
		}
		for (l = f->alt_key_list; l; l = l->next) {
			output_prefix ();
			output ("%s%s[%d].field = ", CB_PREFIX_KEYS, f->cname, nkeys);
			output_param (l->key, -1);
			output ("\n");
			output_line ("%s%s[%d].flag = %d", CB_PREFIX_KEYS, f->cname,
				nkeys, l->duplicates);
			output_line ("%s%s[%d].offset = %d", CB_PREFIX_KEYS, f->cname,
				nkeys, cb_field (l->key)->offset);
			nkeys++;
		}
	}

	output_line ("%s%s.select_name = \"%s\"", CB_PREFIX_FILE, f->cname, f->name);
	if (f->external && !f->file_status) {
		output_line ("%s%s.file_status = common.cob_external_addr (\"%s%s_status\", 4)",
			     CB_PREFIX_FILE, f->cname, CB_PREFIX_FILE, f->cname);
	} else {
		output_line ("%s%s.file_status = %s%s_status", CB_PREFIX_FILE, f->cname,
			     CB_PREFIX_FILE, f->cname);
		output_line ("%s%s_status[0:2] = b\"00\"", CB_PREFIX_FILE, f->cname);
	}
	output_prefix ();
	output ("%s%s.assign = ", CB_PREFIX_FILE, f->cname);
	if (f->special) {
		output ("None");
	} else {
		output_param (f->assign, -1);
	}
	output ("\n");
	output_prefix ();
	output ("%s%s.record = ", CB_PREFIX_FILE, f->cname);
	output_param (CB_TREE (f->record), -1);
	output ("\n");
	output_prefix ();
	output ("%s%s.record_size = ", CB_PREFIX_FILE, f->cname);
	if (f->record_depending) {
		output_param (f->record_depending, -1);
	} else {
		output ("None");
	}
	output ("\n");
	output_line ("%s%s.record_min = %d", CB_PREFIX_FILE, f->cname, f->record_min);
	output_line ("%s%s.record_max = %d", CB_PREFIX_FILE, f->cname, f->record_max);
	if (f->organization == COB_ORG_RELATIVE
	 || f->organization == COB_ORG_INDEXED) {
		output_line ("%s%s.nkeys = %d", CB_PREFIX_FILE, f->cname, nkeys);
		output_line ("%s%s.keys = %s%s", CB_PREFIX_FILE, f->cname, CB_PREFIX_KEYS,
			     f->cname);
	} else {
		output_line ("%s%s.nkeys = 0", CB_PREFIX_FILE, f->cname);
		output_line ("%s%s.keys = None", CB_PREFIX_FILE, f->cname);
	}
	output_line ("%s%s.file = None", CB_PREFIX_FILE, f->cname);

	if (f->linage) {
		output_line ("lingptr = %s%s.linorkeyptr",
				CB_PREFIX_FILE, f->cname);
		output_prefix ();
		output ("lingptr.linage = ");
		output_param (f->linage, -1);
		output ("\n");
		output_prefix ();
		output ("lingptr.linage_ctr = ");
		output_param (f->linage_ctr, -1);
		output ("\n");
		if (f->latfoot) {
			output_prefix ();
			output ("lingptr.latfoot = ");
			output_param (f->latfoot, -1);
			output ("\n");
		} else {
			output_line ("lingptr.latfoot = None");
		}
		if (f->lattop) {
			output_prefix ();
			output ("lingptr.lattop = ");
			output_param (f->lattop, -1);
			output ("\n");
		} else {
			output_line ("lingptr.lattop = None");
		}
		if (f->latbot) {
			output_prefix ();
			output ("lingptr.latbot = ");
			output_param (f->latbot, -1);
			output ("\n");
		} else {
			output_line ("lingptr.latbot = None");
		}
		output_line ("lingptr.lin_lines = 0");
		output_line ("lingptr.lin_foot = 0");
		output_line ("lingptr.lin_top = 0");
		output_line ("lingptr.lin_bot = 0");
	}

	output_line ("%s%s.organization = %d", CB_PREFIX_FILE, f->cname, f->organization);
	output_line ("%s%s.access_mode = %d", CB_PREFIX_FILE, f->cname, f->access_mode);
	output_line ("%s%s.lock_mode = %d", CB_PREFIX_FILE, f->cname, f->lock_mode);
	output_line ("%s%s.open_mode = 0", CB_PREFIX_FILE, f->cname);
	output_line ("%s%s.flag_optional = %d", CB_PREFIX_FILE, f->cname, f->optional);
	output_line ("%s%s.last_open_mode = 0", CB_PREFIX_FILE, f->cname);
	output_line ("%s%s.special = %d", CB_PREFIX_FILE, f->cname, f->special);
	output_line ("%s%s.flag_nonexistent = 0", CB_PREFIX_FILE, f->cname);
	output_line ("%s%s.flag_end_of_file = 0", CB_PREFIX_FILE, f->cname);
	output_line ("%s%s.flag_begin_of_file = 0", CB_PREFIX_FILE, f->cname);
	output_line ("%s%s.flag_first_read = 0", CB_PREFIX_FILE, f->cname);
	output_line ("%s%s.flag_read_done = 0", CB_PREFIX_FILE, f->cname);
	output_line ("%s%s.flag_select_features = %d", CB_PREFIX_FILE, f->cname,
		((f->file_status ? COB_SELECT_FILE_STATUS : 0) |
		(f->linage ? COB_SELECT_LINAGE : 0) |
		(f->external_assign ? COB_SELECT_EXTERNAL : 0)));
	output_line ("%s%s.flag_needs_nl = 0", CB_PREFIX_FILE, f->cname);
	output_line ("%s%s.flag_needs_top = 0", CB_PREFIX_FILE, f->cname);
	output_line ("%s%s.file_version = %d", CB_PREFIX_FILE, f->cname, COB_FILE_VERSION);
	if (f->external) {
		output_block_close ();
	}
}

/*
 * Screen definition
 */

static void
output_screen_definition (struct cb_field *p)
{
	int	type;

	if (p->sister) {
		output_screen_definition (p->sister);
	}
	if (p->children) {
		output_screen_definition (p->children);
	}

	type = (p->children ? COB_SCREEN_TYPE_GROUP :
		p->values ? COB_SCREEN_TYPE_VALUE :
		(p->size > 0) ? COB_SCREEN_TYPE_FIELD : COB_SCREEN_TYPE_ATTRIBUTE);
	/* MIGRATION (C->Python): emit a screenio.cob_screen(...) constructor instead of
	   a C "static cob_screen s_N = { ... };" aggregate.  Argument order mirrors the
	   C struct layout exactly: next, child, field, value, line, column, foreg,
	   backg, type, occurs_min, screen_flag.  Sisters/children are emitted first by
	   the recursive calls above, so their names already exist. */
	output ("s_%d = screenio.cob_screen (", p->id);

	if (p->sister) {
		output ("s_%d, ", p->sister->id);
	} else {
		output ("None, ");
	}
	if (type == COB_SCREEN_TYPE_GROUP) {
		output ("s_%d, ", p->children->id);
	} else {
		output ("None, ");
	}
	if (type == COB_SCREEN_TYPE_FIELD) {
		p->count++;
		output_param (cb_build_field_reference (p, NULL), -1);
		output (", ");
	} else {
		output ("None, ");
	}
	if (type == COB_SCREEN_TYPE_VALUE) {
		output_param (CB_VALUE(p->values), p->id);
		output (", ");
	} else {
		output ("None, ");
	}

	if (p->screen_line) {
		output_param (p->screen_line, 0);
		output (", ");
	} else {
		output ("None, ");
	}
	if (p->screen_column) {
		output_param (p->screen_column, 0);
		output (", ");
	} else {
		output ("None, ");
	}
	if (p->screen_foreg) {
		output_param (p->screen_foreg, 0);
		output (", ");
	} else {
		output ("None, ");
	}
	if (p->screen_backg) {
		output_param (p->screen_backg, 0);
		output (", ");
	} else {
		output ("None, ");
	}
	output ("%d, %d, %d)\n", type, p->occurs_min, p->screen_flag);
}

/*
 * Alphabet-name
 */

static int
literal_value (cb_tree x)
{
	if (x == cb_space) {
		return ' ';
	} else if (x == cb_zero) {
		return '0';
	} else if (x == cb_quote) {
		return '"';
	} else if (x == cb_norm_low) {
		return 0;
	} else if (x == cb_norm_high) {
		return 255;
	} else if (x == cb_null) {
		return 0;
	} else if (CB_TREE_CLASS (x) == CB_CLASS_NUMERIC) {
		return cb_get_int (x) - 1;
	} else {
		return CB_LITERAL (x)->data[0];
	}
}

static void
output_alphabet_name_definition (struct cb_alphabet_name *p)
{
	cb_tree		l;
	cb_tree		ls;
	cb_tree		x;
	unsigned char	*data;
	int		i;
	int		n = 0;
	int		size;
	int		upper;
	int		lower;
	int		table[256];

	/* Reset to -1 */
	for (i = 0; i < 256; i++) {
		table[i] = -1;
	}

	for (l = p->custom_list; l; l = CB_CHAIN (l)) {
		x = CB_VALUE (l);
		if (CB_PAIR_P (x)) {
			/* X THRU Y */
			lower = literal_value (CB_PAIR_X (x));
			upper = literal_value (CB_PAIR_Y (x));
			if (lower <= upper) {
				for (i = lower; i <= upper; i++) {
					table[i] = n++;
				}
			} else {
				for (i = upper; i >= lower; i--) {
					table[i] = n++;
				}
			}
		} else if (CB_LIST_P (x)) {
			/* X ALSO Y ... */
			for (ls = x; ls; ls = CB_CHAIN (ls)) {
				table[literal_value (CB_VALUE (ls))] = n;
			}
			n++;
		} else {
			/* Literal */
			if (CB_TREE_CLASS (x) == CB_CLASS_NUMERIC) {
				table[literal_value (x)] = n++;
			} else if (CB_LITERAL_P (x)) {
				size = (int)CB_LITERAL (x)->size;
				data = CB_LITERAL (x)->data;
				for (i = 0; i < size; i++) {
					table[data[i]] = n++;
				}
			} else {
				table[literal_value (x)] = n++;
			}
		}
	}

	/* Fill the rest of characters */
	for (i = 0; i < 256; i++) {
		if (table[i] == -1) {
			table[i] = n++;
		}
	}

	/* Output the table */
	/* MIGRATION (C->Python): emit the 256-byte collating table as a Python bytes
	   object and the alphabet cob_field via the common.cob_field(...) constructor,
	   preserving every table byte exactly (collating order affects emitted bytes). */
	output_local ("%s%s = bytes ((\n", CB_PREFIX_SEQUENCE, p->cname);
	for (i = 0; i < 256; i++) {
		output_local (" %d,", table[i]);
		if (i % 16 == 15) {
			output_local ("\n");
		}
	}
	output_local ("))\n");
	i = lookup_attr (COB_TYPE_ALPHANUMERIC, 0, 0, 0, NULL, 0);
	output_local ("f_%s = common.cob_field (256, %s%s, %s%d)\n",
		p->cname, CB_PREFIX_SEQUENCE, p->cname, CB_PREFIX_ATTR, i);
	output_local ("\n");
}

/*
 * Class definition
 */

static void
output_class_name_definition (struct cb_class_name *p)
{
	cb_tree		l;
	cb_tree		x;
	unsigned char	*data;
	size_t		i;
	size_t		size;
	int		lower;
	int		upper;

	/* MIGRATION (C->Python): emit a Python class-check "def <cname>(f):" returning
	   0/1, mirroring the C "static int <cname>(cob_field *f)" per-byte range test.
	   f.data[i] is the i-th data byte (int) and f.size is the field size.  C "||"
	   becomes Python "or"; the multi-line condition stays inside parentheses so
	   Python line-continuation applies. */
	output_line ("def %s (f):", p->cname);
	output_block_open ();
	output_line ("for i in range (f.size):");
	output_block_open ();
	output_prefix ();
	output ("if not (    ");
	for (l = p->list; l; l = CB_CHAIN (l)) {
		x = CB_VALUE (l);
		if (CB_PAIR_P (x)) {
			lower = literal_value (CB_PAIR_X (x));
			upper = literal_value (CB_PAIR_Y (x));
			if (!lower) {
				output ("f.data[i] <= %d", upper);
			} else {
				output ("(%d <= f.data[i] and f.data[i] <= %d)", lower, upper);
			}
		} else {
			if (CB_TREE_CLASS (x) == CB_CLASS_NUMERIC) {
				output ("f.data[i] == %d", literal_value(x));
			} else if (x == cb_space) {
				output ("f.data[i] == %d", ' ');
			} else if (x == cb_zero) {
				output ("f.data[i] == %d", '0');
			} else if (x == cb_quote) {
				output ("f.data[i] == %d", '"');
			} else if (x == cb_null) {
				output ("f.data[i] == 0");
			} else {
				size = CB_LITERAL (x)->size;
				data = CB_LITERAL (x)->data;
				for (i = 0; i < size; i++) {
					output ("f.data[i] == %d", data[i]);
					if (i + 1 < size) {
						output (" or ");
					}
				}
			}
		}
		if (CB_CHAIN (l)) {
			output ("\n");
			output_prefix ();
			output ("         or ");
		}
	}
	output (" ):\n");
	output_block_open ();
	output_line ("return 0");
	output_block_close ();
	output_block_close ();
	output_line ("return 1");
	output_block_close ();
	output_newline ();
}

static void
output_initial_values (struct cb_field *p)
{
	cb_tree	x;
	cb_tree	def;

	def = cb_auto_initialize ? cb_true : NULL;
	for (; p; p = p->sister) {
		x = cb_build_field_reference (p, NULL);
		if (p->flag_item_based) {
			continue;
		}
		/* For special registers */
		if (p->flag_no_init && !p->count) {
			continue;
		}
		output_stmt (cb_build_initialize (x, cb_true, NULL, def, 0));
	}
}

/* MIGRATION (C->Python): helper that inlines the EXTERNAL data-item field
   re-pointing that the C emitter performed via "goto L_initextern".  After an
   EXTERNAL base (b_<name>) has been bound to its shared buffer by
   common.cob_external_addr, every cob_field that lives in that buffer must
   have its .data re-pointed at the (now bound) base.  Python has no goto, so
   the assignments are emitted inline at each initialisation point. */
static void
output_external_data_init (void)
{
	struct field_list	*k;

	for (k = field_cache; k; k = k->next) {
		if (k->f->flag_item_external) {
			output_prefix ();
			output ("%s%d.data = ", CB_PREFIX_FIELD, k->f->id);
			output_data (k->x);
			output ("\n");
		}
	}
}

/* MIGRATION (C->Python): the "normal entry" prologue (everything the C emitter
   placed between the global-entry dispatch and the entry-dispatch switch).  It
   runs only for ordinary (non GLOBAL-USE-reentry) entries: re-initialises an
   INITIAL program, allocates and initialises LOCAL-STORAGE, publishes the CALL
   parameter count, primes ANY LENGTH parameters, and saves parameters for
   GLOBAL USE.  Factored into a helper so it can be emitted either directly
   (no GLOBAL declaratives) or inside the "else" arm of the global-entry
   dispatch without duplicating the body. */
static void
output_internal_normal_prologue (struct cb_program *prog, cb_tree parameter_list,
				 int anyseen)
{
	cb_tree			l;
	struct cb_field		*f;
	struct local_list	*locptr;
	int			i;
	char			*p;
	char			name[COB_MINI_BUFF];

	/* INITIAL program: re-establish WORKING-STORAGE on every entry */
	if (prog->flag_initial) {
		for (l = prog->file_list; l; l = CB_CHAIN (l)) {
			f = CB_FILE (CB_VALUE (l))->record;
			if (f->flag_external) {
				strcpy (name, f->name);
				for (p = name; *p; p++) {
					if (*p == '-') {
						*p = '_';
					}
				}
				output_line ("%s%s = common.cob_external_addr (\"%s\", %d)",
					     CB_PREFIX_BASE, name, name,
					     CB_FILE (CB_VALUE (l))->record_max);
			}
		}
		output_initial_values (prog->working_storage);
		if (has_external) {
			output_external_data_init ();
		}
		output_newline ();
		for (l = prog->file_list; l; l = CB_CHAIN (l)) {
			output_file_initialization (CB_FILE (CB_VALUE (l)));
		}
		output_newline ();
	}

	/* LOCAL-STORAGE: a fresh bytearray per invocation (the C runtime used
	   cob_malloc; in Python a per-call bytearray gives identical
	   per-invocation lifetime and byte layout) */
	if (prog->local_storage) {
		if (local_cache) {
			output_comment ("Allocate LOCAL storage");
		}
		for (locptr = local_cache; locptr; locptr = locptr->next) {
			output_line ("%s%d = bytearray (%d)", CB_PREFIX_BASE,
				     locptr->f->id, locptr->f->memory_size);
			if (current_prog->flag_global_use) {
				output_line ("save_%s%d = %s%d",
					     CB_PREFIX_BASE, locptr->f->id,
					     CB_PREFIX_BASE, locptr->f->id);
			}
		}
		output_newline ();
		output_comment ("Initialize LOCAL storage");
		output_initial_values (prog->local_storage);
		output_newline ();
	}

	/* Publish the number of CALL parameters into the program's special
	   register (an LVALUE store -> the move.cob_set_int setter) */
	if (cb_field (current_prog->cb_call_params)->count) {
		output_comment ("Initialize number of call params");
		output_prefix ();
		output ("move.cob_set_int (");
		output_param (current_prog->cb_call_params, -1);
		output (", common.cob_call_params)\n");
	}
	output_line ("common.cob_save_call_params = common.cob_call_params");
	output_newline ();
	if (cb_flag_traceall) {
		output_line ("common.cob_ready_trace ()");
		output_newline ();
	}

	/* ANY LENGTH parameters: bind the caller-supplied field and adopt its
	   actual length from the caller's parameter table */
	i = 0;
	if (anyseen) {
		output_comment ("Initialize ANY LENGTH parameters");
	}
	for (l = parameter_list; l; l = CB_CHAIN (l), i++) {
		f = cb_field (CB_VALUE (l));
		if (f->flag_any_length) {
			output_prefix ();
			output ("anylen_%d = ", i);
			output_param (CB_VALUE (l), i);
			output ("\n");
			if (prog->flag_global_use) {
				output_line ("save_anylen_%d = anylen_%d", i, i);
			}
			output_line ("if common.cob_call_params > %d and module.next.cob_procedure_parameters[%d] is not None:",
				     i, i);
			output_block_open ();
			output_line ("anylen_%d.size = module.next.cob_procedure_parameters[%d].size",
				     i, i);
			output_block_close ();
		}
	}
	if (anyseen) {
		output_newline ();
	}

	/* Parameter save for GLOBAL USE re-entry */
	if (prog->flag_global_use && parameter_list) {
		output_comment ("Parameter save");
		for (l = parameter_list; l; l = CB_CHAIN (l)) {
			f = cb_field (CB_VALUE (l));
			output_line ("save_%s%d = %s%d",
				     CB_PREFIX_BASE, f->id, CB_PREFIX_BASE, f->id);
		}
		output_newline ();
	}
	/* MIGRATION (C->Python): the C entry-dispatch "switch (entry) { case i:
	   goto l_<id>; }" is removed.  _entry already carries the starting label
	   id (see ENTRY CONVENTION) and the nested _dispatch(_entry, 0) call
	   begins execution directly at that segment. */
}

/* MIGRATION (C->Python): a COBOL program unit is emitted as a Python function
   "def <pid>_ (_entry, <bases>):".  The flat C instruction stream with C
   labels and computed gotos becomes a nested "def _dispatch (_pc, _through):"
   that re-enters a "while True:" loop; each need_begin paragraph/section is a
   "if _pc == <id>:" segment, GO TO raises _CobGoto, and PERFORM is a
   synchronous recursive _dispatch() call (the Python call stack replaces the
   C perform frame stack).  Program termination (STOP RUN / GOBACK / fall off
   the end) raises _CobExit, caught just outside _dispatch so the module-pop
   and RETURN-CODE handling run exactly once. */
static void
output_internal_function (struct cb_program *prog, cb_tree parameter_list)
{
	cb_tree			l;
	cb_tree			l2;
	struct cb_field		*f;
	struct cb_field		*ff;
	struct local_list	*locptr;
	struct cb_file		*fl;
	char			*p;
	struct handler_struct	*hstr;
	/* MIGRATION (C -> Python): the non-GCC "struct label_list *pl" cursor that
	   drove the perform-frame jump table is removed -- the Python backend has
	   no jump table (control flow is the _dispatch loop plus _CobGoto/_CobExit),
	   so the cursor is dead on every compiler build.  See the matching removal
	   at the end of this function. */
	int			i;
	int			n;
	int			parmnum = 0;
	int			seen = 0;
	int			anyseen;
	int			first;
	char			name[COB_MINI_BUFF];

	/* Program function header: def <pid>_ (_entry, <base params>): */
	output ("def %s_ (_entry", prog->program_id);
	if (!prog->flag_chained) {
		for (l = parameter_list; l; l = CB_CHAIN (l)) {
			output (", %s%d=None",
				CB_PREFIX_BASE, cb_field (CB_VALUE (l))->id);
			parmnum++;
		}
	}
	output ("):\n");
	output_block_open ();

	/* MIGRATION (C->Python): declare as global every module-level name this
	   function REBINDS so the assignment updates the shared object rather
	   than creating a shadow local.  Names that are only mutated in place
	   (ordinary WORKING-STORAGE bytearrays / cob_field objects) are reached
	   through normal global lookup and need no declaration. */
	for (f = prog->working_storage; f; f = f->sister) {
		if (f->flag_external) {
			strcpy (name, f->name);
			for (p = name; *p; p++) {
				if (*p == '-') {
					*p = '_';
				}
			}
			output_line ("global %s%s", CB_PREFIX_BASE, name);
		}
	}
	for (l = prog->file_list; l; l = CB_CHAIN (l)) {
		f = CB_FILE (CB_VALUE (l))->record;
		if (f->flag_external) {
			strcpy (name, f->name);
			for (p = name; *p; p++) {
				if (*p == '-') {
					*p = '_';
				}
			}
			output_line ("global %s%s", CB_PREFIX_BASE, name);
		}
		fl = CB_FILE (CB_VALUE (l));
		output_line ("global %s%s", CB_PREFIX_FILE, fl->cname);
		if (fl->organization == COB_ORG_RELATIVE
		    || fl->organization == COB_ORG_INDEXED) {
			output_line ("global %s%s", CB_PREFIX_KEYS, fl->cname);
		}
	}
	if (cb_sticky_linkage && parmnum) {
		for (i = 0; i < parmnum; i++) {
			output_line ("global cob_parm_%d", i);
		}
	}
	for (f = prog->working_storage; f; f = f->sister) {
		if (f->flag_item_based) {
			output_line ("global %s%d", CB_PREFIX_BASE, f->id);
		}
	}

	/* MIGRATION (C->Python): CALL-parameter table and the cob_module record
	   are rebuilt fresh on every entry (the C runtime used file-scope
	   statics; per-call objects are semantically identical for COBOL and are
	   nested-program safe).  The module is pushed onto the run-unit stack
	   below. */
	output_line ("cob_user_parameters = [None] * %d", COB_MAX_FIELD_PARAMS);
	output_prefix ();
	output ("module = common.cob_module (");
	if (prog->collating_sequence) {
		output_param (cb_ref (prog->collating_sequence), -1);
	} else {
		output ("None");
	}
	output (", ");
	if (prog->crt_status && cb_field (prog->crt_status)->count) {
		output_param (cb_ref (prog->crt_status), -1);
	} else {
		output ("None");
	}
	output (", ");
	if (prog->cursor_pos) {
		output_param (cb_ref (prog->cursor_pos), -1);
	} else {
		output ("None");
	}
	output (", cob_user_parameters, %d, %d, %d, %d, %d, %d, %d)\n",
		cb_display_sign, (int)prog->decimal_point,
		(int)prog->currency_symbol, (int)prog->numeric_separator,
		cb_filename_mapping, cb_binary_truncate, cb_pretty_display);

	/* Decimal scratch registers: re-created per call (always assigned before
	   use, so a fresh object per invocation matches the C semantics while
	   avoiding any static/global collision across nested programs) */
	if (prog->decimal_index_max) {
		output_comment ("Decimal scratch registers");
		for (i = 0; i < prog->decimal_index_max; i++) {
			output_line ("d%d = numeric.cob_decimal ()", i);
		}
	}

	/* EXTERNAL items and dangling LINKAGE items: module-level placeholders
	   are emitted in storage finalisation; here we only need the per-call
	   ANY LENGTH and BASED LOCAL-STORAGE locals to exist as names. */
	anyseen = 0;
	i = 0;
	for (l = parameter_list; l; l = CB_CHAIN (l), i++) {
		f = cb_field (CB_VALUE (l));
		if (f->flag_any_length) {
			if (!anyseen) {
				anyseen = 1;
				output_comment ("ANY LENGTH parameters");
			}
			output_line ("anylen_%d = None", i);
			if (prog->flag_global_use) {
				output_line ("save_anylen_%d = None", i);
			}
		}
	}
	first = 0;
	for (f = prog->local_storage; f; f = f->sister) {
		if (f->flag_item_based) {
			if (!first) {
				first = 1;
				output_comment ("BASED LOCAL-STORAGE");
			}
			output_line ("%s%d = None", CB_PREFIX_BASE, f->id);
		}
	}

	/* Alphabet-names are module-level data (emitted into the module stream) */
	if (prog->alphabet_name_list) {
		for (l = prog->alphabet_name_list; l; l = CB_CHAIN (l)) {
			output_alphabet_name_definition
				(CB_ALPHABET_NAME (CB_VALUE (l)));
		}
	}

	/* Screens are module-level data as well */
	if (prog->screen_storage) {
		output_target = current_prog->local_storage_file;
		output_screen_definition (prog->screen_storage);
		output_target = yyout;
	}

	/* Files: emit the module-level file-handle / key-array placeholders
	   (output_file_allocation now produces "h_<name> = None" etc. at module
	   scope; the names were declared global above so the per-call file
	   initialisation can rebind them). */
	if (prog->file_list) {
		for (l = prog->file_list; l; l = CB_CHAIN (l)) {
			(void) output_file_allocation (CB_FILE (CB_VALUE (l)));
		}
	}

	/* ---- Start of function code ---- */
	output_comment ("Start of function code");
	output_newline ();

	/* CANCEL callback handling (entry < 0) */
	output_comment ("CANCEL callback handling");
	output_line ("if _entry < 0:");
	output_block_open ();
	output_line ("if not getattr (%s_, '_initialized', 0):", prog->program_id);
	output_block_open ();
	output_line ("return 0");
	output_block_close ();
	for (l = prog->file_list; l; l = CB_CHAIN (l)) {
		fl = CB_FILE (CB_VALUE (l));
		if (fl->organization != COB_ORG_SORT) {
			output_line ("fileio.cob_close (%s%s, 0, None)",
				     CB_PREFIX_FILE, fl->cname);
		}
	}
	/* Decimal registers are garbage-collected; no explicit clear needed. */
	output_line ("%s_._initialized = 0", prog->program_id);
	output_line ("return 0");
	output_block_close ();
	output_newline ();

	/* Sticky linkage: restore omitted trailing parameters from the previous
	   call and remember the supplied ones.  The C fall-through switch
	   (case K runs cases K..end) becomes "restore param i iff cob_call_params
	   <= i". */
	if (cb_sticky_linkage && parmnum) {
		output_line ("_ccp = common.cob_call_params");
		output_line ("if _ccp < %d:", parmnum);
		output_block_open ();
		for (i = 0, l = parameter_list; l; l = CB_CHAIN (l), i++) {
			output_line ("if _ccp <= %d and cob_parm_%d is not None:",
				     i, i);
			output_block_open ();
			output_line ("%s%d = cob_parm_%d", CB_PREFIX_BASE,
				     cb_field (CB_VALUE (l))->id, i);
			output_block_close ();
		}
		output_block_close ();
		for (i = 0, l = parameter_list; l; l = CB_CHAIN (l), i++) {
			output_line ("if %s%d is not None:", CB_PREFIX_BASE,
				     cb_field (CB_VALUE (l))->id);
			output_block_open ();
			output_line ("cob_parm_%d = %s%d", i, CB_PREFIX_BASE,
				     cb_field (CB_VALUE (l))->id);
			output_block_close ();
		}
		output_newline ();
	}

	/* MIGRATION (C->Python): the C perform frame stack (frame_ptr /
	   frame_stack / frame_overflow / temp_index) is gone; PERFORM uses the
	   Python call stack via recursive _dispatch() calls. */

	/* Push module stack */
	output_comment ("Push module stack");
	output_line ("module.next = common.cob_current_module");
	output_line ("common.cob_current_module = module");
	output_newline ();

	/* One-time initialisation (guarded by a per-function attribute so the
	   guard is private to each nested program) */
	output_comment ("Initialize program");
	output_line ("if not getattr (%s_, '_initialized', 0):", prog->program_id);
	output_block_open ();
	output_line ("if not common.cob_initialized:");
	output_block_open ();
	if (cb_flag_implicit_init) {
		output_line ("common.cob_init (0, None)");
	} else {
		output_line ("common.cob_fatal_error (common.COB_FERROR_INITIALIZED)");
	}
	output_block_close ();
	output_line ("common.cob_check_version (COB_SOURCE_FILE, COB_PACKAGE_VERSION, COB_PATCH_LEVEL)");
	if (!prog->flag_main) {
		if (cb_flag_implicit_init) {
			output_line ("call.cob_set_cancel (\"%s\", %s, %s_)",
				     prog->orig_source_name, prog->program_id,
				     prog->program_id);
		} else {
			output_line ("if module.next is not None:");
			output_block_open ();
			output_line ("call.cob_set_cancel (\"%s\", %s, %s_)",
				     prog->orig_source_name, prog->program_id,
				     prog->program_id);
			output_block_close ();
		}
	}
	/* Decimal registers were created above; numeric.cob_decimal() performs
	   their initialisation, so no separate cob_decimal_init step is emitted. */
	if (!prog->flag_initial) {
		for (l = prog->file_list; l; l = CB_CHAIN (l)) {
			f = CB_FILE (CB_VALUE (l))->record;
			if (f->flag_external) {
				strcpy (name, f->name);
				for (p = name; *p; p++) {
					if (*p == '-') {
						*p = '_';
					}
				}
				output_line ("%s%s = common.cob_external_addr (\"%s\", %d)",
					     CB_PREFIX_BASE, name, name,
					     CB_FILE (CB_VALUE (l))->record_max);
			}
		}
		output_initial_values (prog->working_storage);
		if (has_external) {
			output_external_data_init ();
		}
		if (prog->file_list) {
			output_newline ();
			for (l = prog->file_list; l; l = CB_CHAIN (l)) {
				output_file_initialization (CB_FILE (CB_VALUE (l)));
			}
			output_newline ();
		}
	}
	output_line ("%s_._initialized = 1", prog->program_id);
	output_block_close ();
	if (prog->flag_chained) {
		output_line ("else:");
		output_block_open ();
		output_line ("common.cob_fatal_error (common.COB_FERROR_CHAINING)");
		output_block_close ();
	}
	output_newline ();

	/* Build the LOCAL-STORAGE allocation cache (compile-time bookkeeping;
	   emits nothing) */
	if (prog->local_storage) {
		for (f = prog->local_storage; f; f = f->sister) {
			ff = cb_field_founder (f);
			if (ff->redefines) {
				ff = ff->redefines;
			}
			if (ff->flag_item_based || ff->flag_local_alloced) {
				continue;
			}
			if (ff->flag_item_78) {
				fprintf (stderr, "Unexpected CONSTANT item\n");
				ABORT ();
			}
			ff->flag_local_alloced = 1;
			locptr = cobc_malloc (sizeof (struct local_list));
			locptr->f = ff;
			locptr->next = local_cache;
			local_cache = locptr;
		}
		local_cache = local_list_reverse (local_cache);
	}

	/* GLOBAL USE save pointers must be module-level so they survive across
	   the re-entry; declare them global before first use. */
	if (prog->flag_global_use) {
		for (locptr = local_cache; locptr; locptr = locptr->next) {
			output_line ("global save_%s%d", CB_PREFIX_BASE,
				     locptr->f->id);
		}
		for (l = parameter_list; l; l = CB_CHAIN (l)) {
			output_line ("global save_%s%d", CB_PREFIX_BASE,
				     cb_field (CB_VALUE (l))->id);
		}
		i = 0;
		for (l = parameter_list; l; l = CB_CHAIN (l), i++) {
			if (cb_field (CB_VALUE (l))->flag_any_length) {
				output_line ("global save_anylen_%d", i);
			}
		}
	}

	/* Entry routing: GLOBAL USE re-entries restore the saved state and jump
	   (via _entry) straight to the declarative; all other entries run the
	   normal prologue. */
	if (prog->global_list) {
		output_comment ("Global entry dispatch");
		first = 1;
		for (l = prog->global_list; l; l = CB_CHAIN (l)) {
			if (first) {
				output_line ("if _entry == %d:",
					     CB_LABEL (CB_VALUE (l))->id);
				first = 0;
			} else {
				output_line ("elif _entry == %d:",
					     CB_LABEL (CB_VALUE (l))->id);
			}
			output_block_open ();
			if (cb_flag_traceall) {
				output_line ("common.cob_ready_trace ()");
			}
			for (locptr = local_cache; locptr; locptr = locptr->next) {
				output_line ("%s%d = save_%s%d",
					     CB_PREFIX_BASE, locptr->f->id,
					     CB_PREFIX_BASE, locptr->f->id);
			}
			i = 0;
			for (l2 = parameter_list; l2; l2 = CB_CHAIN (l2), i++) {
				f = cb_field (CB_VALUE (l2));
				output_line ("%s%d = save_%s%d",
					     CB_PREFIX_BASE, f->id,
					     CB_PREFIX_BASE, f->id);
				if (f->flag_any_length) {
					output_line ("anylen_%d = save_anylen_%d",
						     i, i);
				}
			}
			output_block_close ();
		}
		output_line ("else:");
		output_block_open ();
		output_internal_normal_prologue (prog, parameter_list, anyseen);
		output_block_close ();
	} else {
		output_internal_normal_prologue (prog, parameter_list, anyseen);
	}
	output_newline ();

	/* ---- Dispatch loop: the PROCEDURE DIVISION as re-enterable segments ---- */
	output_line ("def _dispatch (_pc, _through):");
	output_block_open ();
	output_line ("while True:");
	output_block_open ();
	output_line ("try:");
	output_block_open ();

	/* PROCEDURE DIVISION */
	output_comment ("PROCEDURE DIVISION");
	output_segment_open = 0;
	for (l = prog->exec_list; l; l = CB_CHAIN (l)) {
		output_stmt (CB_VALUE (l));
	}
	/* close the final main-line segment with the implicit STOP RUN */
	if (output_segment_open) {
		output_comment ("Fall through end of program");
		output_line ("raise _CobExit ()");
		output_block_close ();
		output_segment_open = 0;
	}

	/* Error handlers (reached only by PERFORM from the I/O runtime) */
	if (prog->file_list || prog->gen_file_error) {
		seen = 0;
		for (i = COB_OPEN_INPUT; i <= COB_OPEN_EXTEND; i++) {
			if (prog->global_handler[i].handler_label) {
				seen = 1;
				break;
			}
		}
		output_stmt (cb_standard_error_handler);
		/* MIGRATION (C->Python): switch (cob_error_file->last_open_mode)
		   over GLOBAL USE handlers becomes a flat if/elif/else; the
		   default arm invokes the library default error handler. */
		if (seen) {
			first = 1;
			for (i = COB_OPEN_INPUT; i <= COB_OPEN_EXTEND; i++) {
				hstr = &prog->global_handler[i];
				if (hstr->handler_label) {
					if (first) {
						output_line ("if fileio.cob_error_file.last_open_mode == %d:",
							     i);
						first = 0;
					} else {
						output_line ("elif fileio.cob_error_file.last_open_mode == %d:",
							     i);
					}
					output_block_open ();
					if (prog == hstr->handler_prog) {
						output_perform_call (hstr->handler_label,
								     hstr->handler_label);
					} else {
						if (cb_flag_traceall) {
							output_line ("common.cob_reset_trace ()");
						}
						output_prefix ();
						output ("%s_ (%d",
							hstr->handler_prog->program_id,
							hstr->handler_label->id);
						parmnum = cb_list_length (hstr->handler_prog->parameter_list);
						for (n = 0; n < parmnum; n++) {
							output (", None");
						}
						output (")\n");
						if (cb_flag_traceall) {
							output_line ("common.cob_ready_trace ()");
						}
					}
					output_block_close ();
				}
			}
			output_line ("else:");
			output_block_open ();
			output_line ("if not (fileio.cob_error_file.flag_select_features & %d):",
				     COB_SELECT_FILE_STATUS);
			output_block_open ();
			output_line ("fileio.cob_default_error_handle ()");
			output_line ("common.cob_stop_run (1)");
			output_block_close ();
			output_block_close ();
		} else {
			output_line ("if not (fileio.cob_error_file.flag_select_features & %d):",
				     COB_SELECT_FILE_STATUS);
			output_block_open ();
			output_line ("fileio.cob_default_error_handle ()");
			output_line ("common.cob_stop_run (1)");
			output_block_close ();
		}
		output_perform_exit (CB_LABEL (cb_standard_error_handler));
		output_line ("common.cob_fatal_error (common.COB_FERROR_CODEGEN)");
		if (output_segment_open) {
			output_block_close ();
			output_segment_open = 0;
		}
	}

	/* Terminal: an unmatched _pc means the run unit is finished */
	output_line ("raise _CobExit ()");
	output_block_close ();		/* close try */
	output_line ("except _CobGoto as _g:");
	output_block_open ();
	output_line ("_pc = _g.pc");
	output_block_close ();
	if (cb_perform_osvs) {
		/* OS/VS PERFORM exit semantics: unwind to the matching range */
		output_line ("except _CobPerformExit as _pe:");
		output_block_open ();
		output_line ("if _pe.label_id == _through:");
		output_block_open ();
		output_line ("return 0");
		output_block_close ();
		output_line ("raise");
		output_block_close ();
	}
	output_block_close ();		/* close while True */
	output_block_close ();		/* close def _dispatch */
	output_newline ();

	/* Enter the dispatch loop at the requested entry label */
	output_line ("try:");
	output_block_open ();
	output_line ("_dispatch (_entry, 0)");
	output_block_close ();
	output_line ("except _CobExit:");
	output_block_open ();
	output_line ("pass");
	output_block_close ();
	output_newline ();

	/* Program exit cleanup (runs exactly once, regardless of how the run
	   unit terminated) */
	if (prog->local_storage) {
		output_comment ("Deallocate LOCAL storage");
		local_cache = local_list_reverse (local_cache);
		for (locptr = local_cache; locptr; locptr = locptr->next) {
			output_line ("if %s%d is not None:", CB_PREFIX_BASE,
				     locptr->f->id);
			output_block_open ();
			output_line ("%s%d = None", CB_PREFIX_BASE, locptr->f->id);
			output_block_close ();
		}
		output_newline ();
	}
	output_comment ("Pop module stack");
	output_line ("common.cob_current_module = common.cob_current_module.next");
	output_newline ();
	if (cb_flag_traceall) {
		output_line ("common.cob_reset_trace ()");
		output_newline ();
	}
	output_comment ("Program return");
	output_prefix ();
	output ("return ");
	output_integer (current_prog->cb_return_code);
	output ("\n");

	/* MIGRATION (C -> Python): the C emitter closed each program with a
	   "#ifndef __GNUC__" perform-frame jump table -- a "P_switch:" label, a
	   "switch (frame_ptr->return_address)" with one "case N: goto l_M;" per
	   PERFORM target, and a trailing "cob_fatal_error(COB_FERROR_CODEGEN)".
	   That fallback existed only for C compilers lacking GCC's computed-goto
	   ("&&label") extension, and it emitted C tokens (switch/case/goto) that
	   are not valid Python.  The Python backend models every PERFORM/GO TO
	   through the _dispatch loop and the _CobGoto / _CobExit / _CobPerformExit
	   exception classes emitted in the preamble -- a single representation that
	   is identical regardless of which C compiler builds cobc.  The jump table
	   is therefore dead and is removed for ALL compiler builds, satisfying the
	   Python-only emitted-code requirement (it would otherwise leak C tokens
	   into generated .py output when cobc is built with a non-GCC compiler). */

	output_block_close ();		/* close def <pid>_ */
	output_newline ();
}

static void
output_entry_function (struct cb_program *prog, cb_tree entry,
		       cb_tree parameter_list, const int gencode)
{
	const unsigned char	*entry_name;
	cb_tree			using_list;
	cb_tree			l;
	cb_tree			l1;
	cb_tree			l2;
	struct cb_field		*f;
	int			nbytes;

	entry_name = CB_LABEL (CB_PURPOSE (entry))->name;
	using_list = CB_VALUE (entry);

	/* MIGRATION (C->Python): the public ENTRY point becomes a thin Python
	   wrapper "def <entry> (<params>): return <pid>_ (<label id>, <args>)".
	   Python needs no forward prototypes, so the prototype pass
	   (gencode == 0) emits nothing.  Per the ENTRY CONVENTION the internal
	   function is entered with the entry's LABEL id (not a 0-based progid
	   index), so _dispatch begins directly at the correct segment. */
	if (!gencode) {
		return;
	}

	if (prog->flag_chained) {
		using_list = NULL;
		parameter_list = NULL;
	}

	output ("def %s (", entry_name);
	for (l = using_list; l; l = CB_CHAIN (l)) {
		f = cb_field (CB_VALUE (l));
		if (CB_PURPOSE_INT (l) == CB_CALL_BY_VALUE
		    && CB_TREE_CLASS (CB_VALUE (l)) == CB_CLASS_NUMERIC) {
			output ("i_%d=None", f->id);
		} else {
			output ("%s%d=None", CB_PREFIX_BASE, f->id);
		}
		if (CB_CHAIN (l)) {
			output (", ");
		}
	}
	output ("):\n");
	output_block_open ();

	output_prefix ();
	output ("return %s_ (%d", prog->program_id,
		CB_LABEL (CB_PURPOSE (entry))->id);
	for (l1 = parameter_list; l1; l1 = CB_CHAIN (l1)) {
		for (l2 = using_list; l2; l2 = CB_CHAIN (l2)) {
			if (strcasecmp (cb_field (CB_VALUE (l1))->name,
					cb_field (CB_VALUE (l2))->name) == 0) {
				f = cb_field (CB_VALUE (l2));
				switch (CB_PURPOSE_INT (l2)) {
				case CB_CALL_BY_VALUE:
					if (f->usage == CB_USAGE_POINTER ||
					    f->usage == CB_USAGE_PROGRAM_POINTER) {
						/* pass the pointer object directly */
						output (", %s%d", CB_PREFIX_BASE, f->id);
						break;
					} else if (CB_TREE_CLASS (CB_VALUE (l2)) == CB_CLASS_NUMERIC) {
						/* MIGRATION (C->Python): a BY VALUE numeric
						   argument arrives as a Python int; wrap it
						   into a byte buffer of the declared width so
						   the internal function reads it like the C
						   runtime read &i_<id>. */
						switch (CB_SIZES_INT (l2)) {
						case CB_SIZE_1:
							nbytes = 1;
							break;
						case CB_SIZE_2:
							nbytes = 2;
							break;
						case CB_SIZE_4:
							nbytes = 4;
							break;
						case CB_SIZE_8:
							nbytes = 8;
							break;
						default:
							nbytes = 4;
							break;
						}
						output (", call.cob_value_buffer (i_%d, %d, %d)",
							f->id, nbytes,
							(CB_SIZES (l2) & CB_SIZE_UNSIGNED) ? 1 : 0);
						break;
					}
					/* Fall through */
				case CB_CALL_BY_REFERENCE:
				case CB_CALL_BY_CONTENT:
					output (", %s%d", CB_PREFIX_BASE, f->id);
					break;
				}
				break;
			}
		}
		if (l2 == NULL) {
			/* This entry does not USE this parameter -> pass None */
			output (", None");
		}
	}
	output (")\n");
	output_block_close ();
	output_newline ();
}

static void
output_main_function (struct cb_program *prog)
{
	/* MIGRATION (C->Python): the C "int main (argc, argv)" entry becomes a
	   Python "def main ():" that initialises the runtime and runs the public
	   program entry.  The actual "if __name__ == \"__main__\":" trigger is
	   emitted by codegen() AFTER the module data has been flushed, so every
	   referenced name (functions and storage) already exists at run time.
	   cob_stop_run terminates the run unit (it is reached only when the
	   program GOBACKs / falls off the end without an explicit STOP RUN). */
	output_comment ("Main entry point");
	output_line ("def main ():");
	output_block_open ();
	/* MIGRATION / REVIEW FIX (CRITICAL #1): common.cob_init has the C-style
	   signature cob_init(argc=0, argv=None).  Emitting "cob_init (sys.argv)"
	   passed the argv LIST as the argc parameter, corrupting _cob_argc (it
	   became a list, breaking ACCEPT FROM ARGUMENT-NUMBER / COMMAND-LINE and any
	   cob_get_environment/argument API).  Pass the count and the vector
	   explicitly, mirroring the C "main (argc, argv) -> cob_init (argc, argv)". */
	output_line ("common.cob_init (len (sys.argv), sys.argv)");
	output_line ("common.cob_stop_run (%s ())", prog->program_id);
	output_block_close ();
	output_newline ();
	gen_main_trigger = 1;
}

static void
output_header (FILE *fp, const char *locbuff)
{
	int	i;

	if (fp) {
		/* MIGRATION (C -> Python): the provenance banner is emitted as Python
		   "#" comment lines carrying identical metadata (cobc version/patch,
		   source file, generation time, GNU Cobol build/package dates, and the
		   full compile command).  The C-style comment wrappers are removed; a
		   leading "#" comment block is valid as the first lines of a Python
		   module. */
		fprintf (fp, "# Generated by            cobc %s.%d\n",
			PACKAGE_VERSION, PATCH_LEVEL);
		fprintf (fp, "# Generated from          %s\n", cb_source_file);
		fprintf (fp, "# Generated at            %s\n", locbuff);
		fprintf (fp, "# GNU Cobol build date    %s\n", cb_oc_build_stamp);
		fprintf (fp, "# GNU Cobol package date  %s\n", COB_TAR_DATE);
		fprintf (fp, "# Compile command         ");
		for (i = 0; i < cb_saveargc; i++) {
			fprintf (fp, "%s ", cb_saveargv[i]);
		}
		fprintf (fp, "\n\n");
	}
}

static int field_cache_cmp (void *mp1, void *mp2) {
	struct field_list	*fl1;
	struct field_list	*fl2;
	int			ret;

	fl1 = (struct field_list *)mp1;
	fl2 = (struct field_list *)mp2;
	ret = strcasecmp (fl1->curr_prog, fl2->curr_prog);
	if (ret) {
		return ret;
	}
	return fl1->f->id - fl2->f->id;
}

static int base_cache_cmp (void *mp1, void *mp2) {
	struct base_list	*fl1;
	struct base_list	*fl2;

	fl1 = (struct base_list *)mp1;
	fl2 = (struct base_list *)mp2;
	return fl1->f->id - fl2->f->id;
}

/* Sort a structure linked list in place */
/* Assumed that "next" is first item in structure */
static void *
list_cache_sort (void *inlist, int (*cmpfunc)(void *mp1, void *mp2))
{
	struct sort_list	*p;
	struct sort_list	*q;
	struct sort_list	*e;
	struct sort_list	*tail;
	struct sort_list	*list;
	int			insize;
	int			nmerges;
	int			psize;
	int			qsize;
	int			i;

	if (!inlist) {
		return NULL;
	}
	list = (struct sort_list *)inlist;
	insize = 1;
	for (;;) {
		p = list;
		list = NULL;
		tail = NULL;
		nmerges = 0;
		while (p) {
			nmerges++;
			q = p;
			psize = 0;
			for (i = 0; i < insize; i++) {
				psize++;
				q = q->next;
				if (!q) {
					break;
				}
			}
			qsize = insize;
			while (psize > 0 || (qsize > 0 && q)) {
				if (psize == 0) {
					e = q;
					q = q->next;
					qsize--;
				} else if (qsize == 0 || !q) {
					e = p;
					p = p->next;
					psize--;
				} else if ((*cmpfunc) (p, q) <= 0) {
					e = p;
					p = p->next;
					psize--;
				} else {
					e = q;
					q = q->next;
					qsize--;
				}
				if (tail) {
					tail->next = e;
				} else {
					list = e;
				}
				tail = e;
			}
			p = q;
		}
		tail->next = NULL;
		if (nmerges <= 1) {
			return (void *)list;
		}
		insize *= 2;
	}
}

void
codegen (struct cb_program *prog, const int nested)
{
	int			i;
	int			n;	/* MIGRATION (C->Python): PICTURE byte-count for output_string */
	cb_tree			l;
	struct attr_list	*j;
	struct literal_list	*m;
	struct field_list	*k;
	struct base_list	*blp;
	unsigned char		*s;
	struct cb_program	*cp;
	cb_tree			l1;
	cb_tree			l2;
	const char		*prevprog;
	time_t			loctime;
	char			locbuff[48];

	/* Clear local program stuff */
	current_prog = prog;
	param_id = 0;
	stack_id = 0;
	num_cob_fields = 0;
	progid = 0;
	loop_counter = 0;
	output_indent_level = 0;
	last_line = 0;
	needs_exit_prog = 0;
	gen_custom = 0;
	call_cache = NULL;
	label_cache = NULL;
	local_cache = NULL;
	excp_current_program_id = prog->orig_source_name;
	excp_current_section = NULL;
	excp_current_paragraph = NULL;
	memset ((char *)i_counters, 0, sizeof (i_counters));

	output_target = yyout;

	if (!nested) {
		gen_ebcdic = 0;
		gen_ebcdic_ascii = 0;
		gen_full_ebcdic = 0;
		gen_native = 0;
		attr_cache = NULL;
		base_cache = NULL;
		literal_cache = NULL;
		field_cache = NULL;

		loctime = time (NULL);
		strftime (locbuff, sizeof(locbuff) - 1, "%b %d %Y %H:%M:%S %Z",
			localtime (&loctime));
		output_header (output_target, locbuff);
		/* MIGRATION (C -> Python): the C emitter wrote a provenance header
		   into the separate storage (".h") and per-program local-storage
		   (".l.h") files as well.  Under the single-module model there are no
		   such files -- all storage is appended into THIS module by
		   output_flush_module_buffers() -- so those header calls are dropped. */

		/* MIGRATION (C -> Python): the C runtime scaffolding emitted here --
		   the "struct cob_frame" perform-frame stack, the "union
		   cob_call_union" function-pointer holder, the <stdio.h>/<stdlib.h>/
		   <string.h>/<math.h> includes, the WORDS_BIGENDIAN/HAVE_BUILTIN_EXPECT
		   feature macros, and "#include <libcob.h>" -- has no place in Python.
		   The perform-frame stack is replaced by ordinary Python recursion in
		   the _dispatch loop; cob_call_union is replaced by a plain Python
		   callable (_unifunc in output_call); and the C runtime is replaced by
		   the libcob_py package.  We instead emit the Python module preamble:
		   the runtime imports, the recursion-limit bump (deep PERFORM nesting
		   recurses through _dispatch), the control-flow exception classes used
		   by the dispatch model, and the module-level provenance constants. */
		output_line ("import sys");
		/* MIGRATION (C -> Python): bind the libcob_py package name itself in
		   addition to the ten submodules.  codegen_pymod() rule 15 routes any
		   symbol it cannot classify through the package facade, emitting
		   "libcob_py.<name>(...)"; libcob_py/__init__.py re-exports the whole
		   cob_* surface, so this import makes that fallback reference resolvable
		   at runtime (without it the bare "libcob_py" name would be undefined). */
		output_line ("import libcob_py");
		output_line ("from libcob_py import common, numeric, move, strings, "
			     "intrinsic, fileio, call, screenio, termio, system");
		output_newline ();
		output_line ("# Deep PERFORM nesting recurses through _dispatch; raise");
		output_line ("# Python's recursion ceiling well above the C frame stack.");
		output_line ("sys.setrecursionlimit (1000000)");
		output_newline ();
		/* Control-flow exception classes (mirror the C computed-goto model):
		   _CobGoto carries a target label id (GO TO), _CobExit unwinds the
		   whole program (GOBACK / EXIT PROGRAM / STOP RUN fall-through), and
		   _CobPerformExit carries a label id for the OSVS multi-level
		   PERFORM-exit search.  Defined once per module (the preamble runs only
		   for the outermost, non-nested program). */
		output_line ("class _CobGoto (Exception):");
		output_block_open ();
		output_line ("def __init__ (self, pc):");
		output_block_open ();
		output_line ("self.pc = pc");
		output_block_close ();
		output_block_close ();
		output_newline ();
		output_line ("class _CobExit (Exception):");
		output_block_open ();
		output_line ("pass");
		output_block_close ();
		output_newline ();
		output_line ("class _CobPerformExit (Exception):");
		output_block_open ();
		output_line ("def __init__ (self, label_id):");
		output_block_open ();
		output_line ("self.label_id = label_id");
		output_block_close ();
		output_block_close ();
		output_newline ();
		output_line ("COB_SOURCE_FILE = \"%s\"", cb_source_file);
		output_line ("COB_PACKAGE_VERSION = \"%s\"", PACKAGE_VERSION);
		output_line ("COB_PATCH_LEVEL = %d", PATCH_LEVEL);
		output_newline ();

		/* MIGRATION (C -> Python): the GMP-based helpers cob_decimal_set_int /
		   cob_decimal_set_uint and the pointer-arithmetic helper
		   cob_pointer_manip were emitted as static C functions by the C
		   backend.  In Python they live in the libcob_py runtime
		   (numeric.cob_decimal_set_int / ..._uint and common pointer helpers),
		   so nothing is emitted here; the gen_decset/gen_udecset/gen_ptrmanip
		   flags are no longer consulted at emission time.

		   Python needs no forward function prototypes (a "def" is visible
		   module-wide once executed and call targets resolve at call time), so
		   the C "Function prototypes" block is dropped too.  Its loop, however,
		   performed an ESSENTIAL side effect -- building each program's
		   parameter_list by unioning the USING fields of every ENTRY -- which is
		   preserved below; only the prototype emission is removed. */
		for (cp = prog; cp; cp = cp->next_program) {
			/* Build parameter list (side effect retained) */
			for (l = cp->entry_list; l; l = CB_CHAIN (l)) {
				for (l1 = CB_VALUE (l); l1; l1 = CB_CHAIN (l1)) {
					for (l2 = cp->parameter_list; l2; l2 = CB_CHAIN (l2)) {
						if (strcasecmp (cb_field (CB_VALUE (l1))->name,
								cb_field (CB_VALUE (l2))->name) == 0) {
							break;
						}
					}
					if (l2 == NULL) {
						cp->parameter_list = cb_list_add (cp->parameter_list, CB_VALUE (l1));
					}
				}
			}
		}
	}

	/* Class-names */
	if (!prog->nested_level && prog->class_name_list) {
		output ("# Class names\n");
		for (l = prog->class_name_list; l; l = CB_CHAIN (l)) {
			output_class_name_definition (CB_CLASS_NAME (CB_VALUE (l)));
		}
	}

	/* Main function */
	if (prog->flag_main) {
		output_main_function (prog);
	}

	/* Functions */
	if (!nested) {
		output ("# Functions\n\n");
	}
	for (l = prog->entry_list; l; l = CB_CHAIN (l)) {
		output_entry_function (prog, l, prog->parameter_list, 1);
	}

	output_internal_function (prog, prog->parameter_list);

	if (!prog->next_program) {
		output ("# End functions\n\n");
	}

	if (gen_native || gen_full_ebcdic ||
	    gen_ebcdic_ascii || prog->alphabet_name_list) {
		(void)lookup_attr (COB_TYPE_ALPHANUMERIC, 0, 0, 0, NULL, 0);
	}

	/* MIGRATION (C -> Python): direct the storage-finalization emissions
	   (fields/constants/collating tables, which use output()) into the
	   in-memory storage stream rather than a separate ".h" file, so they are
	   flushed into the single module by output_flush_module_buffers(). */
	output_storage_open ();
	output_target = storage_mem;

	/* MIGRATION (C->Python): Program-local declaration block neutralized.
	   The C backend emitted per-program local C declarations into local_mem:
	     - CALL cache: one "union cob_call_union call_<name>" static per CALL
	       target (memoized resolved function pointer). The Python backend
	       resolves CALL targets through a per-call-site local "_unifunc"
	       (see output_call), so no module-level cache declaration is emitted.
	     - Local indexes: "int iN;" subscript temporaries. In Python each
	       subscript loop is a "for iN in range(...):" which self-declares iN.
	     - Local implicit fields: "cob_field fN;" temporaries. In Python each
	       such reference is materialized inline as common.cob_field(...), so
	       no forward declaration is required.
	   The i_counters[]/num_cob_fields/call_cache bookkeeping is still computed
	   during emission (it is harmless and feeds dedup/diagnostics); only the
	   emission of the now-unused declarations is suppressed. */

	/* Skip to next nested program */

	if (prog->next_program) {
		codegen (prog->next_program, 1);
		return;
	}

	/* Finalize the storage file */

	if (base_cache) {
		/* MIGRATION (C->Python): each WORKING-STORAGE base becomes a mutable
		   bytearray of exactly memory_size bytes (the C "aligned" attribute is
		   irrelevant in Python).  output_data() slices memoryview(b_N) so the
		   emitted code shares this backing store byte-for-byte. */
		output_storage ("\n# Data storage\n");
		base_cache = list_cache_sort (base_cache, &base_cache_cmp);
		prevprog = NULL;
		for (blp = base_cache; blp; blp = blp->next) {
			if (blp->curr_prog != prevprog) {
				prevprog = blp->curr_prog;
				output_storage ("\n# PROGRAM-ID : %s\n", prevprog);
			}
			output_storage ("%s%d = bytearray (%d)",
					CB_PREFIX_BASE, blp->f->id,
					blp->f->memory_size);
			output_storage ("\t# %s\n", blp->f->name);
		}
		output_storage ("\n# End of data storage\n\n");
	}

	/* Attributes */
	if (attr_cache) {
		/* MIGRATION (C->Python): a cob_field_attr aggregate becomes a
		   common.cob_field_attr(type, digits, scale, flags, pic) constructor.
		   The PICTURE string (5-byte groups: symbol + 4 binary count bytes) is
		   emitted as a byte-exact Python bytes literal via output_string. */
		output_storage ("\n# Attributes\n\n");
		attr_cache = attr_list_reverse (attr_cache);
		for (j = attr_cache; j; j = j->next) {
			output_storage ("%s%d = common.cob_field_attr (",
					CB_PREFIX_ATTR, j->id);
			output_storage ("%d, %d, %d, %d, ",
					j->type, j->digits,
					j->scale, j->flags);
			if (j->pic) {
				n = 0;
				for (s = j->pic; *s; s += 5) {
					n += 5;
				}
				output_string (j->pic, n);
			} else {
				output_storage ("None");
			}
			output_storage (")\n");
		}
	}

	if (field_cache) {
		output_storage ("\n# Fields\n");
		field_cache = list_cache_sort (field_cache, &field_cache_cmp);
		prevprog = NULL;
		for (k = field_cache; k; k = k->next) {
			if (k->curr_prog != prevprog) {
				prevprog = k->curr_prog;
				output_storage ("\n# PROGRAM-ID : %s\n",
						prevprog);
			}
			/* MIGRATION (C->Python): "static cob_field f_N = {...}" becomes a
			   module-level "f_N = common.cob_field(size, data, attr)".  LOCAL /
			   EXTERNAL fields are bound with a None data slot (re-pointed at run
			   time by the function prologue). */
			output ("%s%d = ", CB_PREFIX_FIELD, k->f->id);
			if (!k->f->flag_local && !k->f->flag_item_external) {
				output_field (k->x);
			} else {
				output ("common.cob_field (");
				output_size (k->x);
				output (", None, ");
				output_attr (k->x);
				output (")");
			}
			output ("\t# %s\n", k->f->name);
		}
		output_storage ("\n# End of fields\n\n");
	}

	/* Literals, constants */
	if (literal_cache) {
		output_storage ("\n# Constants\n");
		literal_cache = literal_list_reverse (literal_cache);
		for (m = literal_cache; m; m = m->next) {
			/* MIGRATION (C->Python): literal/constant cob_field aggregate ->
			   common.cob_field(...) constructor. */
			output ("%s%d = ", CB_PREFIX_CONST, m->id);
			output_field (m->x);
			output ("\n");
		}
		output ("\n");
	}

	/* Collating tables */
	if (gen_ebcdic) {
		output_storage ("\n# ASCII to EBCDIC translate table (restricted)\n");
		output ("cob_a2e = bytes((\n");
		if (alt_ebcdic) {
			output ("\t0x00, 0x01, 0x02, 0x03, 0x37, 0x2D, 0x2E, 0x2F,\n");
			output ("\t0x16, 0x05, 0x25, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F,\n");
			output ("\t0x10, 0x11, 0x12, 0x13, 0x3C, 0x3D, 0x32, 0x26,\n");
			output ("\t0x18, 0x19, 0x3F, 0x27, 0x1C, 0x1D, 0x1E, 0x1F,\n");
			output ("\t0x40, 0x5A, 0x7F, 0x7B, 0x5B, 0x6C, 0x50, 0x7D,\n");
			output ("\t0x4D, 0x5D, 0x5C, 0x4E, 0x6B, 0x60, 0x4B, 0x61,\n");
			output ("\t0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7,\n");
			output ("\t0xF8, 0xF9, 0x7A, 0x5E, 0x4C, 0x7E, 0x6E, 0x6F,\n");
			output ("\t0x7C, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7,\n");
			output ("\t0xC8, 0xC9, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6,\n");
			output ("\t0xD7, 0xD8, 0xD9, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6,\n");
			output ("\t0xE7, 0xE8, 0xE9, 0xAD, 0xE0, 0xBD, 0x5F, 0x6D,\n");
			output ("\t0x79, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,\n");
			output ("\t0x88, 0x89, 0x91, 0x92, 0x93, 0x94, 0x95, 0x96,\n");
			output ("\t0x97, 0x98, 0x99, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6,\n");
			output ("\t0xA7, 0xA8, 0xA9, 0xC0, 0x6A, 0xD0, 0xA1, 0x07,\n");
			output ("\t0x68, 0xDC, 0x51, 0x42, 0x43, 0x44, 0x47, 0x48,\n");
			output ("\t0x52, 0x53, 0x54, 0x57, 0x56, 0x58, 0x63, 0x67,\n");
			output ("\t0x71, 0x9C, 0x9E, 0xCB, 0xCC, 0xCD, 0xDB, 0xDD,\n");
			output ("\t0xDF, 0xEC, 0xFC, 0xB0, 0xB1, 0xB2, 0x3E, 0xB4,\n");
			output ("\t0x45, 0x55, 0xCE, 0xDE, 0x49, 0x69, 0x9A, 0x9B,\n");
			output ("\t0xAB, 0x9F, 0xBA, 0xB8, 0xB7, 0xAA, 0x8A, 0x8B,\n");
			output ("\t0xB6, 0xB5, 0x62, 0x4F, 0x64, 0x65, 0x66, 0x20,\n");
			output ("\t0x21, 0x22, 0x70, 0x23, 0x72, 0x73, 0x74, 0xBE,\n");
			output ("\t0x76, 0x77, 0x78, 0x80, 0x24, 0x15, 0x8C, 0x8D,\n");
			output ("\t0x8E, 0x41, 0x06, 0x17, 0x28, 0x29, 0x9D, 0x2A,\n");
			output ("\t0x2B, 0x2C, 0x09, 0x0A, 0xAC, 0x4A, 0xAE, 0xAF,\n");
			output ("\t0x1B, 0x30, 0x31, 0xFA, 0x1A, 0x33, 0x34, 0x35,\n");
			output ("\t0x36, 0x59, 0x08, 0x38, 0xBC, 0x39, 0xA0, 0xBF,\n");
			output ("\t0xCA, 0x3A, 0xFE, 0x3B, 0x04, 0xCF, 0xDA, 0x14,\n");
			output ("\t0xE1, 0x8F, 0x46, 0x75, 0xFD, 0xEB, 0xEE, 0xED,\n");
			output ("\t0x90, 0xEF, 0xB3, 0xFB, 0xB9, 0xEA, 0xBB, 0xFF\n");
		} else {
			/* MF */
			output ("\t0x00, 0x01, 0x02, 0x03, 0x1D, 0x19, 0x1A, 0x1B,\n");
			output ("\t0x0F, 0x04, 0x16, 0x06, 0x07, 0x08, 0x09, 0x0A,\n");
			output ("\t0x0B, 0x0C, 0x0D, 0x0E, 0x1E, 0x1F, 0x1C, 0x17,\n");
			output ("\t0x10, 0x11, 0x20, 0x18, 0x12, 0x13, 0x14, 0x15,\n");
			output ("\t0x21, 0x27, 0x3A, 0x36, 0x28, 0x30, 0x26, 0x38,\n");
			output ("\t0x24, 0x2A, 0x29, 0x25, 0x2F, 0x2C, 0x22, 0x2D,\n");
			output ("\t0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7A,\n");
			output ("\t0x7B, 0x7C, 0x35, 0x2B, 0x23, 0x39, 0x32, 0x33,\n");
			output ("\t0x37, 0x57, 0x58, 0x59, 0x5A, 0x5B, 0x5C, 0x5D,\n");
			output ("\t0x5E, 0x5F, 0x61, 0x62, 0x63, 0x64, 0x65, 0x66,\n");
			output ("\t0x67, 0x68, 0x69, 0x6B, 0x6C, 0x6D, 0x6E, 0x6F,\n");
			output ("\t0x70, 0x71, 0x72, 0x7D, 0x6A, 0x7E, 0x7F, 0x31,\n");
			output ("\t0x34, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40, 0x41,\n");
			output ("\t0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49,\n");
			output ("\t0x4A, 0x4B, 0x4C, 0x4E, 0x4F, 0x50, 0x51, 0x52,\n");
			output ("\t0x53, 0x54, 0x55, 0x56, 0x2E, 0x60, 0x4D, 0x05,\n");
			output ("\t0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,\n");
			output ("\t0x88, 0x89, 0x8A, 0x8B, 0x8C, 0x8D, 0x8E, 0x8F,\n");
			output ("\t0x90, 0x91, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97,\n");
			output ("\t0x98, 0x99, 0x9A, 0x9B, 0x9C, 0x9D, 0x9E, 0x9F,\n");
			output ("\t0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7,\n");
			output ("\t0xA8, 0xA9, 0xAA, 0xAB, 0xAC, 0xAD, 0xAE, 0xAF,\n");
			output ("\t0xB0, 0xB1, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7,\n");
			output ("\t0xB8, 0xB9, 0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF,\n");
			output ("\t0xC0, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7,\n");
			output ("\t0xC8, 0xC9, 0xCA, 0xCB, 0xCC, 0xCD, 0xCE, 0xCF,\n");
			output ("\t0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7,\n");
			output ("\t0xD8, 0xD9, 0xDA, 0xDB, 0xDC, 0xDD, 0xDE, 0xDF,\n");
			output ("\t0xE0, 0xE1, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7,\n");
			output ("\t0xE8, 0xE9, 0xEA, 0xEB, 0xEC, 0xED, 0xEE, 0xEF,\n");
			output ("\t0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7,\n");
			output ("\t0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFE, 0xFF\n");
		}
		output ("))\n");
		output_storage ("\n");
	}
	if (gen_full_ebcdic) {
		output_storage ("\n# ASCII to EBCDIC table\n");
		output ("cob_ebcdic = bytes((\n");
		output ("\t0x00, 0x01, 0x02, 0x03, 0x37, 0x2D, 0x2E, 0x2F,\n");
		output ("\t0x16, 0x05, 0x25, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F,\n");
		output ("\t0x10, 0x11, 0x12, 0x13, 0x3C, 0x3D, 0x32, 0x26,\n");
		output ("\t0x18, 0x19, 0x3F, 0x27, 0x1C, 0x1D, 0x1E, 0x1F,\n");
		output ("\t0x40, 0x5A, 0x7F, 0x7B, 0x5B, 0x6C, 0x50, 0x7D,\n");
		output ("\t0x4D, 0x5D, 0x5C, 0x4E, 0x6B, 0x60, 0x4B, 0x61,\n");
		output ("\t0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7,\n");
		output ("\t0xF8, 0xF9, 0x7A, 0x5E, 0x4C, 0x7E, 0x6E, 0x6F,\n");
		output ("\t0x7C, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7,\n");
		output ("\t0xC8, 0xC9, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6,\n");
		output ("\t0xD7, 0xD8, 0xD9, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6,\n");
		output ("\t0xE7, 0xE8, 0xE9, 0xAD, 0xE0, 0xBD, 0x5F, 0x6D,\n");
		output ("\t0x79, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,\n");
		output ("\t0x88, 0x89, 0x91, 0x92, 0x93, 0x94, 0x95, 0x96,\n");
		output ("\t0x97, 0x98, 0x99, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6,\n");
		output ("\t0xA7, 0xA8, 0xA9, 0xC0, 0x6A, 0xD0, 0xA1, 0x07,\n");
		output ("\t0x68, 0xDC, 0x51, 0x42, 0x43, 0x44, 0x47, 0x48,\n");
		output ("\t0x52, 0x53, 0x54, 0x57, 0x56, 0x58, 0x63, 0x67,\n");
		output ("\t0x71, 0x9C, 0x9E, 0xCB, 0xCC, 0xCD, 0xDB, 0xDD,\n");
		output ("\t0xDF, 0xEC, 0xFC, 0xB0, 0xB1, 0xB2, 0x3E, 0xB4,\n");
		output ("\t0x45, 0x55, 0xCE, 0xDE, 0x49, 0x69, 0x9A, 0x9B,\n");
		output ("\t0xAB, 0x9F, 0xBA, 0xB8, 0xB7, 0xAA, 0x8A, 0x8B,\n");
		output ("\t0xB6, 0xB5, 0x62, 0x4F, 0x64, 0x65, 0x66, 0x20,\n");
		output ("\t0x21, 0x22, 0x70, 0x23, 0x72, 0x73, 0x74, 0xBE,\n");
		output ("\t0x76, 0x77, 0x78, 0x80, 0x24, 0x15, 0x8C, 0x8D,\n");
		output ("\t0x8E, 0x41, 0x06, 0x17, 0x28, 0x29, 0x9D, 0x2A,\n");
		output ("\t0x2B, 0x2C, 0x09, 0x0A, 0xAC, 0x4A, 0xAE, 0xAF,\n");
		output ("\t0x1B, 0x30, 0x31, 0xFA, 0x1A, 0x33, 0x34, 0x35,\n");
		output ("\t0x36, 0x59, 0x08, 0x38, 0xBC, 0x39, 0xA0, 0xBF,\n");
		output ("\t0xCA, 0x3A, 0xFE, 0x3B, 0x04, 0xCF, 0xDA, 0x14,\n");
		output ("\t0xE1, 0x8F, 0x46, 0x75, 0xFD, 0xEB, 0xEE, 0xED,\n");
		output ("\t0x90, 0xEF, 0xB3, 0xFB, 0xB9, 0xEA, 0xBB, 0xFF\n");
		output ("))\n");
		i = lookup_attr (COB_TYPE_ALPHANUMERIC, 0, 0, 0, NULL, 0);
		output
		    ("f_ebcdic = common.cob_field (256, cob_ebcdic, %s%d)\n",
		     CB_PREFIX_ATTR, i);
		output_storage ("\n");
	}
	if (gen_ebcdic_ascii) {
		output_storage ("\n# EBCDIC to ASCII table\n");
		output ("cob_ebcdic_ascii = bytes((\n");
		output ("\t0x00, 0x01, 0x02, 0x03, 0xEC, 0x09, 0xCA, 0x7F,\n");
		output ("\t0xE2, 0xD2, 0xD3, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F,\n");
		output ("\t0x10, 0x11, 0x12, 0x13, 0xEF, 0xC5, 0x08, 0xCB,\n");
		output ("\t0x18, 0x19, 0xDC, 0xD8, 0x1C, 0x1D, 0x1E, 0x1F,\n");
		output ("\t0xB7, 0xB8, 0xB9, 0xBB, 0xC4, 0x0A, 0x17, 0x1B,\n");
		output ("\t0xCC, 0xCD, 0xCF, 0xD0, 0xD1, 0x05, 0x06, 0x07,\n");
		output ("\t0xD9, 0xDA, 0x16, 0xDD, 0xDE, 0xDF, 0xE0, 0x04,\n");
		output ("\t0xE3, 0xE5, 0xE9, 0xEB, 0x14, 0x15, 0x9E, 0x1A,\n");
		output ("\t0x20, 0xC9, 0x83, 0x84, 0x85, 0xA0, 0xF2, 0x86,\n");
		output ("\t0x87, 0xA4, 0xD5, 0x2E, 0x3C, 0x28, 0x2B, 0xB3,\n");
		output ("\t0x26, 0x82, 0x88, 0x89, 0x8A, 0xA1, 0x8C, 0x8B,\n");
		output ("\t0x8D, 0xE1, 0x21, 0x24, 0x2A, 0x29, 0x3B, 0x5E,\n");
		output ("\t0x2D, 0x2F, 0xB2, 0x8E, 0xB4, 0xB5, 0xB6, 0x8F,\n");
		output ("\t0x80, 0xA5, 0x7C, 0x2C, 0x25, 0x5F, 0x3E, 0x3F,\n");
		output ("\t0xBA, 0x90, 0xBC, 0xBD, 0xBE, 0xF3, 0xC0, 0xC1,\n");
		output ("\t0xC2, 0x60, 0x3A, 0x23, 0x40, 0x27, 0x3D, 0x22,\n");
		output ("\t0xC3, 0x61, 0x62, 0x63, 0x64, 0x65, 0x66, 0x67,\n");
		output ("\t0x68, 0x69, 0xAE, 0xAF, 0xC6, 0xC7, 0xC8, 0xF1,\n");
		output ("\t0xF8, 0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x6F, 0x70,\n");
		output ("\t0x71, 0x72, 0xA6, 0xA7, 0x91, 0xCE, 0x92, 0xA9,\n");
		output ("\t0xE6, 0x7E, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78,\n");
		output ("\t0x79, 0x7A, 0xAD, 0xA8, 0xD4, 0x5B, 0xD6, 0xD7,\n");
		output ("\t0x9B, 0x9C, 0x9D, 0xFA, 0x9F, 0xB1, 0xB0, 0xAC,\n");
		output ("\t0xAB, 0xFC, 0xAA, 0xFE, 0xE4, 0x5D, 0xBF, 0xE7,\n");
		output ("\t0x7B, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47,\n");
		output ("\t0x48, 0x49, 0xE8, 0x93, 0x94, 0x95, 0xA2, 0xED,\n");
		output ("\t0x7D, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F, 0x50,\n");
		output ("\t0x51, 0x52, 0xEE, 0x96, 0x81, 0x97, 0xA3, 0x98,\n");
		output ("\t0x5C, 0xF0, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,\n");
		output ("\t0x59, 0x5A, 0xFD, 0xF5, 0x99, 0xF7, 0xF6, 0xF9,\n");
		output ("\t0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37,\n");
		output ("\t0x38, 0x39, 0xDB, 0xFB, 0x9A, 0xF4, 0xEA, 0xFF\n");
		output ("))\n");
		i = lookup_attr (COB_TYPE_ALPHANUMERIC, 0, 0, 0, NULL, 0);
		output
		    ("f_ebcdic_ascii = common.cob_field (256, cob_ebcdic_ascii, %s%d)\n",
		     CB_PREFIX_ATTR, i);
		output_storage ("\n");
	}
	if (gen_native) {
		output_storage ("\n# NATIVE table\n");
		output ("cob_native = bytes((\n");
		output ("\t0, 1, 2, 3, 4, 5, 6, 7,\n");
		output ("\t8, 9, 10, 11, 12, 13, 14, 15,\n");
		output ("\t16, 17, 18, 19, 20, 21, 22, 23,\n");
		output ("\t24, 25, 26, 27, 28, 29, 30, 31,\n");
		output ("\t32, 33, 34, 35, 36, 37, 38, 39,\n");
		output ("\t40, 41, 42, 43, 44, 45, 46, 47,\n");
		output ("\t48, 49, 50, 51, 52, 53, 54, 55,\n");
		output ("\t56, 57, 58, 59, 60, 61, 62, 63,\n");
		output ("\t64, 65, 66, 67, 68, 69, 70, 71,\n");
		output ("\t72, 73, 74, 75, 76, 77, 78, 79,\n");
		output ("\t80, 81, 82, 83, 84, 85, 86, 87,\n");
		output ("\t88, 89, 90, 91, 92, 93, 94, 95,\n");
		output ("\t96, 97, 98, 99, 100, 101, 102, 103,\n");
		output ("\t104, 105, 106, 107, 108, 109, 110, 111,\n");
		output ("\t112, 113, 114, 115, 116, 117, 118, 119,\n");
		output ("\t120, 121, 122, 123, 124, 125, 126, 127,\n");
		output ("\t128, 129, 130, 131, 132, 133, 134, 135,\n");
		output ("\t136, 137, 138, 139, 140, 141, 142, 143,\n");
		output ("\t144, 145, 146, 147, 148, 149, 150, 151,\n");
		output ("\t152, 153, 154, 155, 156, 157, 158, 159,\n");
		output ("\t160, 161, 162, 163, 164, 165, 166, 167,\n");
		output ("\t168, 169, 170, 171, 172, 173, 174, 175,\n");
		output ("\t176, 177, 178, 179, 180, 181, 182, 183,\n");
		output ("\t184, 185, 186, 187, 188, 189, 190, 191,\n");
		output ("\t192, 193, 194, 195, 196, 197, 198, 199,\n");
		output ("\t200, 201, 202, 203, 204, 205, 206, 207,\n");
		output ("\t208, 209, 210, 211, 212, 213, 214, 215,\n");
		output ("\t216, 217, 218, 219, 220, 221, 222, 223,\n");
		output ("\t224, 225, 226, 227, 228, 229, 230, 231,\n");
		output ("\t232, 233, 234, 235, 236, 237, 238, 239,\n");
		output ("\t240, 241, 242, 243, 244, 245, 246, 247,\n");
		output ("\t248, 249, 250, 251, 252, 253, 254, 255\n");
		output ("))\n");
		i = lookup_attr (COB_TYPE_ALPHANUMERIC, 0, 0, 0, NULL, 0);
		output
		    ("f_native = common.cob_field (256, cob_native, %s%d)\n",
		     CB_PREFIX_ATTR, i);
		output_storage ("\n");
	}

	/* MIGRATION (C -> Python): collapse the three former streams into one.
	   All storage and local declarations accumulated above are now flushed
	   into the single module stream (yyout) so the emitted ".py" is fully
	   self-contained with no external storage/local include dependency.  This
	   runs once, at the outermost finalization (prog->next_program == NULL). */
	output_flush_module_buffers ();

	/* MIGRATION (C->Python): emit the module entry trigger AFTER the data has
	   been flushed, so every name (functions + storage) the main() body
	   references already exists when the module is run as a script. */
	if (gen_main_trigger) {
		output_target = yyout;
		output_line ("if __name__ == \"__main__\":");
		output_block_open ();
		output_line ("main ()");
		output_block_close ();
	}
}
