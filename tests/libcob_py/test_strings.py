"""Unit tests for :mod:`libcob_py.strings`.

Exercises the pure-Python port of the C runtime ``libcob/strings.c`` - the
COBOL ``INSPECT`` / ``STRING`` / ``UNSTRING`` statement runtime.  The suite
covers, per the AAP coverage gate (sections 0.6.5 / 0.7.1, >=80% line coverage):

* INSPECT TALLYING and REPLACING for ALL / LEADING / FIRST / TRAILING /
  CHARACTERS, with BEFORE / AFTER region boundaries and the marker semantics
  that prevent double counting / replacing,
* INSPECT CONVERTING (the translate-table form, including the wider-source
  clamp),
* STRING with multiple sources, DELIMITED BY, WITH POINTER and the
  ``EC-OVERFLOW-STRING`` overflow path,
* UNSTRING with DELIMITED BY (incl. ALL), COUNT IN, DELIMITER IN, TALLYING,
  WITH POINTER and the ``EC-OVERFLOW-UNSTRING`` overflow path.

Standard library only - the runtime under test introduces ZERO third-party
dependencies; ``pytest`` is a development-only test framework (AAP 0.5 / 0.7.1).

The data-movement sibling ``libcob_py.move`` (which provides ``cob_move`` /
``cob_get_int`` / ``cob_set_int`` used by the STRING/UNSTRING POINTER, COUNT IN
and DELIMITER IN paths, and indirectly by ``common.cob_memcpy``) is built in
parallel and may not yet exist.  When it is absent this module installs a small,
**standard-library-only** functional shim so the move-backed code paths can be
unit-tested deterministically; when the real module is present it is used
unchanged.  ``libcob_py.numeric`` (which provides ``cob_add_int`` for the
TALLYING paths) already exists and is used as-is.
"""
import importlib.util
import sys
import types

import pytest

# Obtain the parallel-built runtime lazily so collection degrades to a clean
# SKIP (never a hard error) when the package is not yet importable.
libcob_py = pytest.importorskip("libcob_py")
common = pytest.importorskip("libcob_py.common")
strings = pytest.importorskip("libcob_py.strings")


# ===========================================================================
# Functional ``libcob_py.move`` shim (installed only when the real module is
# absent).  ``strings.py`` calls ``cob_move`` / ``cob_get_int`` / ``cob_set_int``
# via deferred imports, and ``common.cob_memcpy`` delegates to
# ``move.cob_move`` - so these three entry points must exist for the move-backed
# paths to run.  The shim implements exactly the COBOL semantics those paths
# rely on, using only the standard library.
# ===========================================================================
def _build_move_shim():
    shim = types.ModuleType("libcob_py.move")

    def cob_move(src, dst):
        """Alphanumeric MOVE: left-justify, space-fill or truncate.

        Handles the figurative ``ALPHANUMERIC_ALL`` constants (ZERO / SPACE)
        used by UNSTRING's DELIMITER IN fallback by repeating the source byte
        across the receiver, and the ordinary alphanumeric copy used by
        ``cob_memcpy`` (left-justified, space-padded, truncated to fit).
        """
        if src.attr.type == common.COB_TYPE_ALPHANUMERIC_ALL:
            fill = bytes(src.data[:src.size]) or b" "
            for i in range(dst.size):
                dst.data[i] = fill[i % len(fill)]
            return
        n = min(src.size, dst.size)
        dst.data[0:n] = src.data[:n]
        for i in range(n, dst.size):
            dst.data[i] = 0x20  # space-fill the remainder

    def cob_get_int(f):
        """Parse the digit bytes of a numeric field into a Python int."""
        digits = bytes(b for b in f.data[:f.size] if 0x30 <= b <= 0x39)
        return int(digits.decode("ascii")) if digits else 0

    def cob_set_int(f, n):
        """Store *n* into a numeric field as right-justified zoned digits."""
        width = f.size
        text = str(abs(int(n)))[-width:].rjust(width, "0")
        f.data[0:width] = text.encode("ascii")

    shim.cob_move = cob_move
    shim.cob_get_int = cob_get_int
    shim.cob_set_int = cob_set_int
    return shim


# Install the shim once, at import time, only when the real ``libcob_py.move``
# is not importable from disk.  ``find_spec`` consults the filesystem, so once
# the real module lands this guard becomes a no-op and the real module is used.
if importlib.util.find_spec("libcob_py.move") is None \
        and "libcob_py.move" not in sys.modules:
    _shim = _build_move_shim()
    sys.modules["libcob_py.move"] = _shim
    setattr(libcob_py, "move", _shim)


# ===========================================================================
# Field-construction helpers
# ===========================================================================
T_ALNUM = common.COB_TYPE_ALPHANUMERIC
T_ALL = common.COB_TYPE_ALPHANUMERIC_ALL
T_DISP = common.COB_TYPE_NUMERIC_DISPLAY

F_SIGN = common.COB_FLAG_HAVE_SIGN
F_SEP = common.COB_FLAG_SIGN_SEPARATE
F_LEAD = common.COB_FLAG_SIGN_LEADING

EC_OVERFLOW_STRING = 0x0A02
EC_OVERFLOW_UNSTRING = 0x0A03
EC_RANGE_INSPECT_SIZE = 0x0D03


def alnum(text, size=None):
    """Build an ALPHANUMERIC ``cob_field`` from *text* (str or bytes).

    With no *size* the field is exactly ``len(text)`` bytes (used for sources,
    delimiters and literals).  With a *size* the field is *size* bytes filled by
    repeating *text* cyclically - a test convenience so an unwritten destination
    visibly shows its filler character (e.g. ``alnum(".", size=6)`` ->
    ``"......"``), making partial writes easy to assert.
    """
    payload = text.encode("latin-1") if isinstance(text, str) else bytes(text)
    attr = common.cob_field_attr(type=T_ALNUM, digits=0, scale=0, flags=0, pic=None)
    if size is None:
        return common.cob_field(size=len(payload), data=bytearray(payload),
                                attr=attr)
    if not payload:
        payload = b" "
    data = bytearray(payload[i % len(payload)] for i in range(size))
    return common.cob_field(size=size, data=data, attr=attr)


def all_lit(text):
    """Build an ALPHANUMERIC_ALL figurative-style field (e.g. SPACE/ZERO)."""
    payload = text.encode("latin-1") if isinstance(text, str) else bytes(text)
    attr = common.cob_field_attr(type=T_ALL, digits=0, scale=0, flags=0, pic=None)
    return common.cob_field(size=len(payload), data=bytearray(payload), attr=attr)


def counter(digits=4, value=0):
    """Build an unsigned zoned DISPLAY counter pre-set to *value*."""
    text = str(value)[-digits:].rjust(digits, "0")
    attr = common.cob_field_attr(type=T_DISP, digits=digits, scale=0, flags=0,
                                 pic=None)
    return common.cob_field(size=digits, data=bytearray(text.encode("ascii")),
                            attr=attr)


def cval(field):
    """Decode a zoned DISPLAY counter back to an int (ignoring leading zeros)."""
    return int(bytes(field.data[:field.size]).decode("latin-1"))


def text_of(field):
    """Return the latin-1 text of a field's value bytes."""
    return bytes(field.data[:field.size]).decode("latin-1")


@pytest.fixture(autouse=True)
def _fresh_strings_state():
    """Reset the module's working state before each test (cob_init_strings)."""
    strings.cob_init_strings()
    common.cob_exception_code = 0
    yield
    common.cob_exception_code = 0


# ===========================================================================
# Internal helpers
# ===========================================================================
class TestHelpers:
    def test_cob_min_int(self):
        assert strings.cob_min_int(3, 7) == 3
        assert strings.cob_min_int(7, 3) == 3
        assert strings.cob_min_int(5, 5) == 5
        assert strings.cob_min_int(-2, 4) == -2

    def test_alloc_figurative_stretches_cyclically(self):
        # A 2-byte pattern stretched to width 5 repeats cyclically.
        fig = strings.alloc_figurative(alnum("ab"), alnum("XXXXX"))
        assert fig.size == 5
        assert bytes(fig.data) == b"ababa"
        assert fig.attr.type == T_ALNUM

    def test_alloc_figurative_single_byte(self):
        fig = strings.alloc_figurative(all_lit(" "), alnum("###"))
        assert bytes(fig.data) == b"   "

    def test_inspect_selectors_are_distinct(self):
        assert {strings.INSPECT_ALL, strings.INSPECT_LEADING,
                strings.INSPECT_FIRST, strings.INSPECT_TRAILING} == {0, 1, 2, 3}


# ===========================================================================
# INSPECT ... TALLYING
# ===========================================================================
class TestInspectTallying:
    def _tally(self, subject, op, search, value=0, before=None, after=None):
        var = alnum(subject)
        cnt = counter(value=value)
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        if after is not None:
            strings.cob_inspect_after(alnum(after))
        if before is not None:
            strings.cob_inspect_before(alnum(before))
        op(cnt, search)
        strings.cob_inspect_finish()
        return cval(cnt)

    def test_tally_all(self):
        assert self._tally("HELLO WORLD", strings.cob_inspect_all, alnum("L")) == 3

    def test_tally_all_multichar(self):
        # Non-overlapping occurrences of "AB" in "ABABAB" -> 3.
        assert self._tally("ABABAB", strings.cob_inspect_all, alnum("AB")) == 3

    def test_tally_all_accumulates_into_existing(self):
        # cob_add_int adds to the counter's existing value.
        assert self._tally("AAAA", strings.cob_inspect_all, alnum("A"),
                           value=10) == 14

    def test_tally_leading(self):
        assert self._tally("00120", strings.cob_inspect_leading, alnum("0")) == 2

    def test_tally_leading_none_when_not_at_start(self):
        assert self._tally("10000", strings.cob_inspect_leading, alnum("0")) == 0

    def test_tally_first(self):
        # FIRST counts a single occurrence only.
        assert self._tally("ABABAB", strings.cob_inspect_first, alnum("AB")) == 1

    def test_tally_trailing(self):
        assert self._tally("12000", strings.cob_inspect_trailing, alnum("0")) == 3

    def test_tally_trailing_none(self):
        assert self._tally("12001", strings.cob_inspect_trailing, alnum("0")) == 0

    def test_tally_characters(self):
        # CHARACTERS counts every (unmarked) byte of the region.
        var = alnum("ABCDE")
        cnt = counter()
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_characters(cnt)
        strings.cob_inspect_finish()
        assert cval(cnt) == 5

    def test_tally_all_before(self):
        # Count "L" only before the first "O": HE[LL]O WORLD -> 2.
        assert self._tally("HELLO WORLD", strings.cob_inspect_all, alnum("L"),
                           before="O") == 2

    def test_tally_all_after(self):
        # Count "L" only after the first "E": HE[LLO WOR L D] -> 3.
        assert self._tally("HELLO WORLD", strings.cob_inspect_all, alnum("L"),
                           after="E") == 3

    def test_tally_all_after_and_before(self):
        # Region is the "LL" between E and O -> 2.
        assert self._tally("HELLO WORLD", strings.cob_inspect_all, alnum("L"),
                           after="E", before="O") == 2

    def test_tally_characters_after(self):
        # Characters after "," -> "WORLD" = 5.
        var = alnum("HELLO,WORLD")
        cnt = counter()
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_after(alnum(","))
        strings.cob_inspect_characters(cnt)
        strings.cob_inspect_finish()
        assert cval(cnt) == 5

    def test_after_no_match_collapses_region(self):
        # AFTER a string that does not occur -> region empty -> count 0.
        assert self._tally("ABCDE", strings.cob_inspect_all, alnum("A"),
                           after="Z") == 0

    def test_before_no_match_keeps_region(self):
        # BEFORE a string that does not occur -> region unchanged.
        assert self._tally("AAAZZ", strings.cob_inspect_all, alnum("A"),
                           before="X") == 3

    def test_tally_combined_characters_and_all_no_double_count(self):
        # TALLYING ALL "L" then CHARACTERS must not re-count the marked Ls.
        var = alnum("HELLO")
        c1 = counter()
        c2 = counter()
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_all(c1, alnum("L"))     # marks the two Ls
        strings.cob_inspect_characters(c2)          # counts the remaining 3
        strings.cob_inspect_finish()
        assert (cval(c1), cval(c2)) == (2, 3)


# ===========================================================================
# INSPECT ... REPLACING
# ===========================================================================
class TestInspectReplacing:
    def _replace(self, subject, op, by, what, before=None, after=None):
        var = alnum(subject)
        strings.cob_inspect_init(var, 1)
        strings.cob_inspect_start()
        if after is not None:
            strings.cob_inspect_after(alnum(after))
        if before is not None:
            strings.cob_inspect_before(alnum(before))
        op(by, what)
        strings.cob_inspect_finish()
        return text_of(var)

    def test_replace_all(self):
        assert self._replace("HELLO", strings.cob_inspect_all,
                             alnum("*"), alnum("L")) == "HE**O"

    def test_replace_all_multichar(self):
        assert self._replace("ABABAB", strings.cob_inspect_all,
                             alnum("XY"), alnum("AB")) == "XYXYXY"

    def test_replace_leading(self):
        assert self._replace("00120", strings.cob_inspect_leading,
                             alnum("*"), alnum("0")) == "**120"

    def test_replace_first(self):
        assert self._replace("ABABAB", strings.cob_inspect_first,
                             alnum("XY"), alnum("AB")) == "XYABAB"

    def test_replace_trailing(self):
        assert self._replace("12000", strings.cob_inspect_trailing,
                             alnum("*"), alnum("0")) == "12***"

    def test_replace_characters(self):
        var = alnum("ABCDE")
        strings.cob_inspect_init(var, 1)
        strings.cob_inspect_start()
        strings.cob_inspect_characters(alnum("*"))
        strings.cob_inspect_finish()
        assert text_of(var) == "*****"

    def test_replace_all_before_after(self):
        # Replace "L" by "*" only between E and O.
        assert self._replace("HELLO WORLD", strings.cob_inspect_all,
                             alnum("*"), alnum("L"),
                             after="E", before="O") == "HE**O WORLD"

    def test_replace_all_figurative_stretches(self):
        # REPLACING ALL "AB" BY SPACE: SPACE is ALPHANUMERIC_ALL (size 1) and is
        # stretched to width 2 via alloc_figurative.
        var = alnum("ABxyAB")
        strings.cob_inspect_init(var, 1)
        strings.cob_inspect_start()
        strings.cob_inspect_all(all_lit(" "), alnum("AB"))
        strings.cob_inspect_finish()
        assert text_of(var) == "  xy  "

    def test_replace_size_mismatch_raises_range(self):
        # A non-figurative replacement whose width differs from the match width
        # is EC-RANGE-INSPECT-SIZE and performs no replacement.
        var = alnum("ABAB")
        strings.cob_inspect_init(var, 1)
        strings.cob_inspect_start()
        strings.cob_inspect_all(alnum("X"), alnum("AB"))  # 1 vs 2 -> error
        strings.cob_inspect_finish()
        assert common.cob_exception_code == EC_RANGE_INSPECT_SIZE
        assert text_of(var) == "ABAB"  # unchanged


# ===========================================================================
# INSPECT ... CONVERTING
# ===========================================================================
class TestInspectConverting:
    def _convert(self, subject, frm, to, before=None, after=None):
        var = alnum(subject)
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        if after is not None:
            strings.cob_inspect_after(alnum(after))
        if before is not None:
            strings.cob_inspect_before(alnum(before))
        strings.cob_inspect_converting(alnum(frm), alnum(to))
        strings.cob_inspect_finish()
        return text_of(var)

    def test_convert_translate_table(self):
        assert self._convert("aXbYc", "abc", "xyz") == "xXyYz"

    def test_convert_case_fold(self):
        assert self._convert("Hello", "abcdefghijklmnopqrstuvwxyz",
                             "ABCDEFGHIJKLMNOPQRSTUVWXYZ") == "HELLO"

    def test_convert_wider_source_clamps_to_last(self):
        # f1 wider than f2: surplus source chars map to the final f2 byte.
        # "abc" -> "X": every a/b/c becomes "X" (j>=f2.size clamps to f2.size-1).
        assert self._convert("cab", "abc", "X") == "XXX"

    def test_convert_no_double_convert(self):
        # A converted byte is marked and not converted again on a later pass
        # within the same statement.
        assert self._convert("aa", "a", "b") == "bb"


# ===========================================================================
# INSPECT on signed DISPLAY (sign extraction / restoration via base offset)
# ===========================================================================
class TestInspectSigned:
    def test_tally_on_trailing_overpunch_sign(self):
        # S9(4) DISPLAY, trailing overpunch, value -1230 -> bytes "123p"
        # ('0'+0x40 == 'p').  cob_get_sign normalises the sign digit so the scan
        # sees clean digits; cob_put_sign restores it at finish.
        attr = common.cob_field_attr(type=T_DISP, digits=4, scale=0, flags=F_SIGN,
                                     pic=None)
        var = common.cob_field(size=4, data=bytearray(b"123p"), attr=attr)
        cnt = counter()
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_all(cnt, alnum("0"))
        strings.cob_inspect_finish()
        # Region digits after sign-normalisation are "1230" -> one "0".
        assert cval(cnt) == 1
        # Sign is restored: the trailing byte is the negative overpunch again.
        assert var.data[3] == ord("p")

    def test_tally_on_separate_leading_sign(self):
        # S9(3) DISPLAY SIGN LEADING SEPARATE, value +000: byte 0 is '+', value
        # region is data[1:].  base offset must skip the sign byte so writes and
        # the region align with the digits.
        attr = common.cob_field_attr(type=T_DISP, digits=3, scale=0,
                                     flags=F_SIGN | F_SEP | F_LEAD, pic=None)
        var = common.cob_field(size=4, data=bytearray(b"+000"), attr=attr)
        cnt = counter()
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_all(cnt, alnum("0"))
        strings.cob_inspect_finish()
        assert cval(cnt) == 3
        assert chr(var.data[0]) == "+"  # sign byte preserved


# ===========================================================================
# STRING
# ===========================================================================
class TestString:
    def test_concat_multiple_sources_delimited_size(self):
        dst = alnum(".", size=6)
        strings.cob_string_init(dst, None)
        strings.cob_string_delimited(None)         # DELIMITED BY SIZE
        strings.cob_string_append(alnum("AB"))
        strings.cob_string_append(alnum("CD"))
        strings.cob_string_finish()
        assert text_of(dst) == "ABCD.."
        assert common.cob_exception_code == 0

    def test_delimited_by_value_truncates_source(self):
        dst = alnum(".", size=6)
        strings.cob_string_init(dst, None)
        strings.cob_string_delimited(alnum("/"))   # DELIMITED BY "/"
        strings.cob_string_append(alnum("AB/CD"))  # only "AB" is taken
        strings.cob_string_finish()
        assert text_of(dst) == "AB...."

    def test_delimited_value_absent_appends_whole(self):
        dst = alnum(".", size=6)
        strings.cob_string_init(dst, None)
        strings.cob_string_delimited(alnum("/"))
        strings.cob_string_append(alnum("ABCD"))   # no "/" -> whole source
        strings.cob_string_finish()
        assert text_of(dst) == "ABCD.."

    def test_overflow_sets_exception_and_fills_to_end(self):
        dst = alnum(".", size=4)
        strings.cob_string_init(dst, None)
        strings.cob_string_delimited(None)
        strings.cob_string_append(alnum("ABCDEF"))  # only 4 fit
        strings.cob_string_finish()
        assert text_of(dst) == "ABCD"
        assert common.cob_exception_code == EC_OVERFLOW_STRING

    def test_append_is_noop_after_overflow(self):
        dst = alnum(".", size=4)
        strings.cob_string_init(dst, None)
        strings.cob_string_delimited(None)
        strings.cob_string_append(alnum("ABCD"))    # fills exactly
        strings.cob_string_append(alnum("EF"))      # overflow -> exception
        strings.cob_string_append(alnum("GH"))      # no-op (exception pending)
        strings.cob_string_finish()
        assert text_of(dst) == "ABCD"
        assert common.cob_exception_code == EC_OVERFLOW_STRING

    def test_with_pointer_seeds_offset_and_writes_back(self):
        # POINTER value 3 (1-based) -> start writing at index 2.
        dst = alnum(".", size=6)
        ptr = counter(digits=2, value=3)
        strings.cob_string_init(dst, ptr)
        strings.cob_string_delimited(None)
        strings.cob_string_append(alnum("XY"))
        strings.cob_string_finish()
        assert text_of(dst) == "..XY.."
        # Cursor advanced past the two written bytes: 3 + 2 -> 5.
        assert cval(ptr) == 5

    def test_with_pointer_out_of_range_raises_overflow(self):
        # POINTER beyond the destination size -> EC-OVERFLOW-STRING at init,
        # suppressing the append entirely.
        dst = alnum(".", size=4)
        ptr = counter(digits=2, value=9)
        strings.cob_string_init(dst, ptr)
        strings.cob_string_delimited(None)
        strings.cob_string_append(alnum("AB"))
        strings.cob_string_finish()
        assert common.cob_exception_code == EC_OVERFLOW_STRING
        assert text_of(dst) == "...."  # nothing written

    def test_with_pointer_zero_is_out_of_range(self):
        # POINTER value 0 -> offset -1 -> out of range.
        dst = alnum(".", size=4)
        ptr = counter(digits=2, value=0)
        strings.cob_string_init(dst, ptr)
        assert common.cob_exception_code == EC_OVERFLOW_STRING


# ===========================================================================
# UNSTRING
# ===========================================================================
class TestUnstring:
    def test_delimited_by_size_fills_receivers(self):
        # No delimiters: each INTO takes up to the receiver's capacity.
        src = alnum("ABCDEF")
        r1 = alnum(".", size=3)
        r2 = alnum(".", size=3)
        strings.cob_unstring_init(src, None, 0)
        strings.cob_unstring_into(r1, None, None)
        strings.cob_unstring_into(r2, None, None)
        strings.cob_unstring_finish()
        assert (text_of(r1), text_of(r2)) == ("ABC", "DEF")

    def test_single_delimiter_split(self):
        src = alnum("AB,CD,EF")
        r1 = alnum(".", size=2)
        r2 = alnum(".", size=2)
        r3 = alnum(".", size=2)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum(","), 0)
        strings.cob_unstring_into(r1, None, None)
        strings.cob_unstring_into(r2, None, None)
        strings.cob_unstring_into(r3, None, None)
        strings.cob_unstring_finish()
        assert (text_of(r1), text_of(r2), text_of(r3)) == ("AB", "CD", "EF")

    def test_short_receiver_truncates_field(self):
        # Field "ABCD" into a 2-byte receiver keeps the left 2 bytes.
        src = alnum("ABCD,EF")
        r1 = alnum(".", size=2)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum(","), 0)
        strings.cob_unstring_into(r1, None, None)
        # offset advanced past the whole "ABCD," delimiter.
        assert text_of(r1) == "AB"

    def test_multiple_delimiters(self):
        # Either "," or ";" delimits.
        src = alnum("AB;CD,EF")
        r1 = alnum(".", size=2)
        r2 = alnum(".", size=2)
        r3 = alnum(".", size=2)
        strings.cob_unstring_init(src, None, 2)
        strings.cob_unstring_delimited(alnum(","), 0)
        strings.cob_unstring_delimited(alnum(";"), 0)
        strings.cob_unstring_into(r1, None, None)
        strings.cob_unstring_into(r2, None, None)
        strings.cob_unstring_into(r3, None, None)
        strings.cob_unstring_finish()
        assert (text_of(r1), text_of(r2), text_of(r3)) == ("AB", "CD", "EF")

    def test_delimited_by_all_collapses_runs(self):
        # DELIMITED BY ALL ",": the "AB,,,CD" run is a single separator.
        src = alnum("AB,,,CD")
        r1 = alnum(".", size=2)
        r2 = alnum(".", size=2)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum(","), 1)   # ALL
        strings.cob_unstring_into(r1, None, None)
        strings.cob_unstring_into(r2, None, None)
        strings.cob_unstring_finish()
        assert (text_of(r1), text_of(r2)) == ("AB", "CD")

    def test_count_in(self):
        # COUNT IN receives the number of source chars moved into the receiver.
        src = alnum("ABCD,EF")
        r1 = alnum(".", size=6)
        cnt = counter()
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum(","), 0)
        strings.cob_unstring_into(r1, None, cnt)
        strings.cob_unstring_finish()
        assert text_of(r1) == "ABCD  "   # space-filled receiver
        assert cval(cnt) == 4

    def test_delimiter_in_matched(self):
        # DELIMITER IN receives the matched delimiter bytes.
        src = alnum("AB;CD")
        r1 = alnum(".", size=2)
        dlm = alnum(".", size=1)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum(";"), 0)
        strings.cob_unstring_into(r1, dlm, None)
        assert text_of(r1) == "AB"
        assert text_of(dlm) == ";"

    def test_delimiter_in_space_when_no_delimiter(self):
        # When the field ends without a delimiter, an alphanumeric DELIMITER IN
        # receives SPACE.
        src = alnum("ABCD")
        r1 = alnum(".", size=4)
        dlm = alnum("Z", size=1)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum(","), 0)
        strings.cob_unstring_into(r1, dlm, None)
        assert text_of(r1) == "ABCD"
        assert text_of(dlm) == " "

    def test_delimiter_in_zero_when_numeric_and_no_delimiter(self):
        # A numeric DELIMITER IN receives ZERO ('0' fill) at end-of-source.
        src = alnum("ABCD")
        r1 = alnum(".", size=4)
        dlm = counter(digits=2, value=99)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum(","), 0)
        strings.cob_unstring_into(r1, dlm, None)
        assert text_of(dlm) == "00"

    def test_tallying_counts_receivers(self):
        src = alnum("A,B,C")
        tally = counter(value=0)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum(","), 0)
        for _ in range(3):
            strings.cob_unstring_into(alnum(".", size=1), None, None)
        strings.cob_unstring_tallying(tally)
        strings.cob_unstring_finish()
        assert cval(tally) == 3

    def test_with_pointer_seeds_and_writes_back(self):
        # Source "AB,CD,EF" (1-based): 1=A 2=B 3=',' 4=C 5=D 6=',' 7=E 8=F.
        # POINTER value 4 -> begin reading at 0-based index 3 ("CD,EF").
        src = alnum("AB,CD,EF")
        ptr = counter(digits=2, value=4)
        r1 = alnum(".", size=2)
        strings.cob_unstring_init(src, ptr, 1)
        strings.cob_unstring_delimited(alnum(","), 0)
        strings.cob_unstring_into(r1, None, None)
        strings.cob_unstring_finish()
        assert text_of(r1) == "CD"
        # cursor advances past "CD" and its "," (0-based index 5) -> next read at
        # 0-based 6 -> 1-based 7.
        assert cval(ptr) == 7

    def test_overflow_when_source_remains(self):
        # Only the first field is extracted; remaining source -> EC-OVERFLOW.
        src = alnum("AB,CD,EF")
        r1 = alnum(".", size=2)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum(","), 0)
        strings.cob_unstring_into(r1, None, None)
        strings.cob_unstring_finish()
        assert text_of(r1) == "AB"
        assert common.cob_exception_code == EC_OVERFLOW_UNSTRING

    def test_no_overflow_when_fully_consumed(self):
        src = alnum("AB,CD")
        r1 = alnum(".", size=2)
        r2 = alnum(".", size=2)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum(","), 0)
        strings.cob_unstring_into(r1, None, None)
        strings.cob_unstring_into(r2, None, None)
        strings.cob_unstring_finish()
        assert common.cob_exception_code == 0

    def test_pointer_out_of_range_raises_overflow(self):
        src = alnum("ABCD")
        ptr = counter(digits=2, value=99)
        strings.cob_unstring_init(src, ptr, 0)
        assert common.cob_exception_code == EC_OVERFLOW_UNSTRING

    def test_into_is_noop_after_pending_exception(self):
        # A pending exception (from an out-of-range POINTER) short-circuits INTO.
        src = alnum("ABCD")
        ptr = counter(digits=2, value=99)
        r1 = alnum(".", size=4)
        strings.cob_unstring_init(src, ptr, 0)        # raises overflow
        strings.cob_unstring_into(r1, None, None)     # no-op
        assert text_of(r1) == "...."

    def test_into_past_end_of_source_is_noop(self):
        # When the cursor is already at end-of-source, INTO does nothing.
        src = alnum("AB")
        r1 = alnum(".", size=2)
        r2 = alnum("Z", size=2)
        strings.cob_unstring_init(src, None, 0)
        strings.cob_unstring_into(r1, None, None)     # consumes "AB"
        strings.cob_unstring_into(r2, None, None)     # offset == size -> no-op
        assert text_of(r1) == "AB"
        assert text_of(r2) == "ZZ"   # untouched


# ===========================================================================
# Edge cases that exercise the marker-overlap, figurative-operand and
# multi-byte-delimiter boundary paths.
# ===========================================================================
class TestEdgeCases:
    def test_copy_field_none_passthrough(self):
        # The shared-data snapshot helper passes a None (OMITTED) argument
        # through unchanged.
        assert strings._copy_field(None) is None

    def test_overlapping_all_marks_block_second_search(self):
        # Within one INSPECT bracket, ALL "AB" marks bytes 0-1 of "ABC"; the
        # subsequent ALL "BC" finds a match at 1-2 but byte 1 is already marked,
        # so the overlapping occurrence is skipped (not counted).
        var = alnum("ABC")
        c1 = counter()
        c2 = counter()
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_all(c1, alnum("AB"))
        strings.cob_inspect_all(c2, alnum("BC"))
        strings.cob_inspect_finish()
        assert (cval(c1), cval(c2)) == (1, 0)

    def test_overlapping_trailing_marks_blocked(self):
        # TRAILING scan that meets an already-marked byte stops counting there.
        var = alnum("XAAA")
        c1 = counter()
        c2 = counter()
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_trailing(c1, alnum("AA"))   # marks the last "AA"
        strings.cob_inspect_trailing(c2, alnum("AAA"))  # overlaps -> blocked
        strings.cob_inspect_finish()
        assert cval(c1) == 1
        assert cval(c2) == 0

    def test_tally_all_none_operand_is_low_value(self):
        # A None matched-operand models figurative LOW-VALUE (0x00); count the
        # NUL bytes in the subject.
        var = alnum(b"A\x00B\x00")
        cnt = counter()
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_all(cnt, None)
        strings.cob_inspect_finish()
        assert cval(cnt) == 2

    def test_unstring_multibyte_delimiter_runs_past_end(self):
        # A 2-byte delimiter "::" with a lone trailing ":" exercises the
        # "delimiter would run past the source end" continue path; the trailing
        # ":" is data, not a delimiter.
        src = alnum("AB::C:")
        r1 = alnum(".", size=2)
        r2 = alnum(".", size=2)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum("::"), 0)
        strings.cob_unstring_into(r1, None, None)
        strings.cob_unstring_into(r2, None, None)
        strings.cob_unstring_finish()
        assert text_of(r1) == "AB"
        assert text_of(r2) == "C:"   # the lone ':' is kept as data

    def test_unstring_all_multibyte_delimiter(self):
        # DELIMITED BY ALL with a 2-byte delimiter collapses consecutive
        # 2-byte runs, exercising the ALL inner-loop boundary handling.
        src = alnum("AB::::CD")
        r1 = alnum(".", size=2)
        r2 = alnum(".", size=2)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum("::"), 1)   # ALL
        strings.cob_unstring_into(r1, None, None)
        strings.cob_unstring_into(r2, None, None)
        strings.cob_unstring_finish()
        assert (text_of(r1), text_of(r2)) == ("AB", "CD")

    def test_before_match_inside_region_clamps_end(self):
        # BEFORE where the match is partway through forces the in-loop advance
        # (p increments) before clamping the region end.
        var = alnum("xxYzz")
        cnt = counter()
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_before(alnum("Y"))   # region becomes "xx"
        strings.cob_inspect_characters(cnt)
        strings.cob_inspect_finish()
        assert cval(cnt) == 2

    def test_after_match_inside_region_advances_start(self):
        # AFTER where the match is partway through forces the in-loop advance.
        var = alnum("xxYzz")
        cnt = counter()
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_after(alnum("Y"))    # region becomes "zz"
        strings.cob_inspect_characters(cnt)
        strings.cob_inspect_finish()
        assert cval(cnt) == 2


# ===========================================================================
# Agent-prompt checklist tests (explicit, named exactly as the file
# specification enumerates them in Phases 1-5).  These provide direct,
# 1:1 traceability from each required checklist item to a passing test.
# They reuse the module-level field helpers (``alnum`` / ``counter`` / ``cval``
# / ``text_of``) and drive the documented ``init -> ... -> finish`` call
# protocol of :mod:`libcob_py.strings` exactly as the cobc emitter generates it.
# The broader behavioural matrix above (TestInspect*/TestString/TestUnstring/
# TestEdgeCases) remains the primary coverage; this class guarantees the
# specification's named scenarios are each present and green.
# ===========================================================================
class TestAgentPromptChecklist:
    # ----- Phase 1 - INSPECT TALLYING ------------------------------------
    def test_inspect_tallying_all(self):
        # INSPECT "AABAA" TALLYING cnt FOR ALL "A" -> 4.
        var = alnum("AABAA")
        cnt = counter(value=0)
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_all(cnt, alnum("A"))
        strings.cob_inspect_finish()
        assert cval(cnt) == 4

    def test_inspect_tallying_leading(self):
        # INSPECT "AABAA" TALLYING cnt FOR LEADING "A" -> 2 (the leading run).
        var = alnum("AABAA")
        cnt = counter(value=0)
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_leading(cnt, alnum("A"))
        strings.cob_inspect_finish()
        assert cval(cnt) == 2

    def test_inspect_tallying_characters_before_after(self):
        # CHARACTERS BEFORE INITIAL "B" in "AABAA" -> the "AA" prefix = 2;
        # CHARACTERS AFTER INITIAL "B" -> the trailing "AA" = 2.  Asserts that
        # the BEFORE/AFTER region boundaries are honoured.
        var_before = alnum("AABAA")
        cnt_before = counter(value=0)
        strings.cob_inspect_init(var_before, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_before(alnum("B"))
        strings.cob_inspect_characters(cnt_before)
        strings.cob_inspect_finish()
        assert cval(cnt_before) == 2

        var_after = alnum("AABAA")
        cnt_after = counter(value=0)
        strings.cob_inspect_init(var_after, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_after(alnum("B"))
        strings.cob_inspect_characters(cnt_after)
        strings.cob_inspect_finish()
        assert cval(cnt_after) == 2

    # ----- Phase 2 - INSPECT REPLACING & CONVERTING ----------------------
    def test_inspect_replacing_all(self):
        # INSPECT "AABAA" REPLACING ALL "A" BY "X" -> "XXBXX".
        var = alnum("AABAA")
        strings.cob_inspect_init(var, 1)
        strings.cob_inspect_start()
        strings.cob_inspect_all(alnum("X"), alnum("A"))
        strings.cob_inspect_finish()
        assert text_of(var) == "XXBXX"

    def test_inspect_replacing_first_leading(self):
        # FIRST replaces only the first occurrence; LEADING replaces the
        # leading run only - both partial replacements.
        var_first = alnum("AABAA")
        strings.cob_inspect_init(var_first, 1)
        strings.cob_inspect_start()
        strings.cob_inspect_first(alnum("X"), alnum("A"))
        strings.cob_inspect_finish()
        assert text_of(var_first) == "XABAA"

        var_leading = alnum("AABAA")
        strings.cob_inspect_init(var_leading, 1)
        strings.cob_inspect_start()
        strings.cob_inspect_leading(alnum("X"), alnum("A"))
        strings.cob_inspect_finish()
        assert text_of(var_leading) == "XXBAA"

    def test_inspect_converting(self):
        # CONVERTING "abc" TO "ABC": a translate table over the subject
        # (lowercase a/b/c -> uppercase A/B/C); other bytes are untouched.
        var = alnum("abcXabc")
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_converting(alnum("abc"), alnum("ABC"))
        strings.cob_inspect_finish()
        assert text_of(var) == "ABCXABC"

    # ----- Phase 3 - STRING ----------------------------------------------
    def test_string_basic(self):
        # STRING "AB", "CD" DELIMITED BY SIZE INTO PIC X(10) (initialised to
        # spaces) -> "ABCD      ", with WITH POINTER advanced to 5 (1-based).
        dst = alnum(" ", size=10)
        ptr = counter(digits=2, value=1)
        strings.cob_string_init(dst, ptr)
        strings.cob_string_delimited(None)            # DELIMITED BY SIZE
        strings.cob_string_append(alnum("AB"))
        strings.cob_string_append(alnum("CD"))
        strings.cob_string_finish()
        assert text_of(dst) == "ABCD      "
        assert cval(ptr) == 5                         # 4 bytes written -> next = 5
        assert common.cob_exception_code == 0

    def test_string_delimited_value(self):
        # DELIMITED BY "/" truncates each source at the delimiter: "AB/Z" -> "AB"
        # and "CD/Q" -> "CD", giving "ABCD" then space filler.
        dst = alnum(" ", size=6)
        strings.cob_string_init(dst, None)
        strings.cob_string_delimited(alnum("/"))
        strings.cob_string_append(alnum("AB/Z"))
        strings.cob_string_append(alnum("CD/Q"))
        strings.cob_string_finish()
        assert text_of(dst) == "ABCD  "

    def test_string_overflow(self):
        # Concatenation exceeding the target raises ON OVERFLOW and sets
        # EC-OVERFLOW-STRING (0x0A02); the target is filled to its end.
        dst = alnum(" ", size=4)
        strings.cob_string_init(dst, None)
        strings.cob_string_delimited(None)
        strings.cob_string_append(alnum("ABCDEF"))    # only 4 fit
        strings.cob_string_finish()
        assert text_of(dst) == "ABCD"
        assert common.cob_exception_code == 0x0A02

    # ----- Phase 4 - UNSTRING --------------------------------------------
    def test_unstring_basic(self):
        # UNSTRING "A,B,C" DELIMITED BY "," INTO three receivers -> "A","B","C";
        # TALLYING records the 3 receiving items filled.
        src = alnum("A,B,C")
        r1 = alnum(".", size=1)
        r2 = alnum(".", size=1)
        r3 = alnum(".", size=1)
        tally = counter(value=0)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum(","), 0)
        strings.cob_unstring_into(r1, None, None)
        strings.cob_unstring_into(r2, None, None)
        strings.cob_unstring_into(r3, None, None)
        strings.cob_unstring_tallying(tally)
        strings.cob_unstring_finish()
        assert (text_of(r1), text_of(r2), text_of(r3)) == ("A", "B", "C")
        assert cval(tally) == 3

    def test_unstring_all_delimiter(self):
        # DELIMITED BY ALL "," collapses the consecutive ",,," run in "AB,,,CD"
        # into a single separator -> "AB","CD".
        src = alnum("AB,,,CD")
        r1 = alnum(".", size=2)
        r2 = alnum(".", size=2)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum(","), 1)   # ALL
        strings.cob_unstring_into(r1, None, None)
        strings.cob_unstring_into(r2, None, None)
        strings.cob_unstring_finish()
        assert (text_of(r1), text_of(r2)) == ("AB", "CD")

    def test_unstring_all_multibyte_delimiter_runs_past_end(self):
        # DELIMITED BY ALL with a 2-byte delimiter whose swallow loop reaches a
        # point with fewer than 2 bytes remaining exercises the "delimiter would
        # run past the source end" break inside the ALL collapse: "AB:::" ALL
        # "::" yields "AB", then the lone trailing ":" remains as data.
        src = alnum("AB:::")
        r1 = alnum(".", size=2)
        r2 = alnum(".", size=2)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum("::"), 1)   # ALL
        strings.cob_unstring_into(r1, None, None)
        strings.cob_unstring_into(r2, None, None)
        strings.cob_unstring_finish()
        assert text_of(r1) == "AB"
        # The lone trailing ':' is data; cob_memcpy left-justifies it into the
        # 2-byte receiver and space-fills the remainder (-> ": ").
        assert text_of(r2) == ": "

    def test_unstring_count_delimiter_in(self):
        # COUNT IN receives the source-character count moved (4 for "ABCD");
        # DELIMITER IN receives the matched delimiter (",").
        src = alnum("ABCD,EF")
        r1 = alnum(".", size=6)
        dlm = alnum(".", size=1)
        cnt = counter(value=0)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum(","), 0)
        strings.cob_unstring_into(r1, dlm, cnt)
        assert text_of(r1) == "ABCD  "    # left-justified, space-filled receiver
        assert text_of(dlm) == ","
        assert cval(cnt) == 4

    def test_unstring_overflow(self):
        # More fields than receivers: only the first field is extracted and
        # source characters remain, so ON OVERFLOW fires with
        # EC-OVERFLOW-UNSTRING (0x0A03).
        src = alnum("AB,CD,EF")
        r1 = alnum(".", size=2)
        strings.cob_unstring_init(src, None, 1)
        strings.cob_unstring_delimited(alnum(","), 0)
        strings.cob_unstring_into(r1, None, None)
        strings.cob_unstring_finish()
        assert text_of(r1) == "AB"
        assert common.cob_exception_code == 0x0A03

    # ----- Phase 5 - initialisation --------------------------------------
    def test_init_strings_callable(self):
        # ``cob_init_strings()`` must be callable without arguments or error and
        # must leave the subsystem in a clean baseline (no pending exception).
        common.cob_exception_code = 0
        result = strings.cob_init_strings()
        assert result is None
        assert common.cob_exception_code == 0
        # A subsequent INSPECT runs correctly after a re-init, proving the reset
        # left usable state.
        var = alnum("ZZZ")
        cnt = counter(value=0)
        strings.cob_inspect_init(var, 0)
        strings.cob_inspect_start()
        strings.cob_inspect_all(cnt, alnum("Z"))
        strings.cob_inspect_finish()
        assert cval(cnt) == 3

