"""libcob_py.common - runtime base module for the Python COBOL backend.

This module is a pure-Python port of the GNU Cobol (OpenCOBOL) C runtime core.
It is the *base* runtime module of the ``libcob_py`` package: every other
runtime module (``numeric``, ``move``, ``strings``, ``intrinsic``, ``fileio``,
``termio``, ``call``, ``screenio``, ``system``) imports it, and the generated
Python emitted by the rewritten ``cobc`` code generator calls into it through
the stable ``cob_*`` names preserved here.

Authoritative sources (ported semantically - the C files are NOT modified):

* ``libcob/common.c``     - init/alloc lifecycle, sign handling, comparisons,
                            class tests, runtime checks, switches, environment
                            and date/time ACCEPT/DISPLAY, table sort and the
                            ``C$``/system helper routines.
* ``libcob/exception.def``- the authoritative exception enumeration. It defines
                            EXACTLY 146 ``COB_EXCEPTION`` entries across 23
                            categories. (The AAP narrative's figure of "95"
                            codes / "22" categories is a non-authoritative
                            aggregate; per AAP section 0.6.1 rule 3 we implement
                            ALL 146 codes and 23 categories and log the delta.)
* ``libcob/common.h``     - the field/flag/type model and the ``cob_*`` API
                            surface that this module mirrors.

Implementation constraints (AAP sections 0.5 / 0.7.1):

* STANDARD LIBRARY ONLY - this module imports nothing outside the CPython
  standard library and introduces ZERO third-party dependencies.
* The ``cob_*`` public names are preserved verbatim so that the emitter's
  ``cob_<fn>`` call-sites map 1:1 onto ``libcob_py.common.<fn>``.

Because ``common`` sits at the bottom of the dependency order, it must not
import its sibling modules at module-import time (that would create import
cycles).  The few routines that genuinely need data-movement / numeric helpers
(``cob_move``, ``cob_get_int``, ``cob_numeric_cmp`` ...) perform a *deferred*
import inside the function body.
"""

from __future__ import annotations

import os
import struct
import sys
import time
# NOTE: ``struct`` and ``io`` are part of the runtime's standard-library surface
# but are *not* imported here because ``common.py`` itself does not use them.
# Per AAP s0.5.1, each sibling module imports the stdlib modules it needs
# directly (e.g. ``numeric.py`` imports ``struct`` for packed-decimal helpers,
# ``fileio.py`` imports ``io``) - the base module stays minimal and only
# imports what it actually references (os, sys, time; namedtuple/importlib/
# functools/types are imported at their points of use).

__all__ = []  # extended programmatically at the end of the module


# ---------------------------------------------------------------------------
# Buffer size definitions (common.h L358-L375)
# ---------------------------------------------------------------------------
COB_MINI_BUFF = 256
COB_SMALL_BUFF = 1024
COB_NORMAL_BUFF = 2048
COB_MEDIUM_BUFF = 8192
COB_LARGE_BUFF = 16384
COB_MINI_MAX = COB_MINI_BUFF - 1
COB_SMALL_MAX = COB_SMALL_BUFF - 1
COB_NORMAL_MAX = COB_NORMAL_BUFF - 1
COB_MEDIUM_MAX = COB_MEDIUM_BUFF - 1
COB_LARGE_MAX = COB_LARGE_BUFF - 1

# Perform stack size / maximum number of parameters (common.h L371-L375)
COB_STACK_SIZE = 255
COB_MAX_FIELD_PARAMS = 64

# Error buffer size (common.c L77)
COB_ERRBUF_SIZE = 256


# ---------------------------------------------------------------------------
# Field types (common.h L381-L398) - reproduced byte-for-byte
# ---------------------------------------------------------------------------
COB_TYPE_UNKNOWN = 0x00
COB_TYPE_GROUP = 0x01
COB_TYPE_BOOLEAN = 0x02

COB_TYPE_NUMERIC = 0x10
COB_TYPE_NUMERIC_DISPLAY = 0x10
COB_TYPE_NUMERIC_BINARY = 0x11
COB_TYPE_NUMERIC_PACKED = 0x12
COB_TYPE_NUMERIC_FLOAT = 0x13
COB_TYPE_NUMERIC_DOUBLE = 0x14
COB_TYPE_NUMERIC_EDITED = 0x24

COB_TYPE_ALPHANUMERIC = 0x21
COB_TYPE_ALPHANUMERIC_ALL = 0x22
COB_TYPE_ALPHANUMERIC_EDITED = 0x23

COB_TYPE_NATIONAL = 0x40
COB_TYPE_NATIONAL_EDITED = 0x41


# ---------------------------------------------------------------------------
# Field flags (common.h L402-L409)
# ---------------------------------------------------------------------------
COB_FLAG_HAVE_SIGN = 0x01
COB_FLAG_SIGN_SEPARATE = 0x02
COB_FLAG_SIGN_LEADING = 0x04
COB_FLAG_BLANK_ZERO = 0x08
COB_FLAG_JUSTIFIED = 0x10
COB_FLAG_BINARY_SWAP = 0x20
COB_FLAG_REAL_BINARY = 0x40
COB_FLAG_IS_POINTER = 0x80

# Display sign representation (common.h L447-L448)
COB_DISPLAY_SIGN_ASCII = 0
COB_DISPLAY_SIGN_EBCDIC = 1


# ---------------------------------------------------------------------------
# Fatal error definitions (common.h L450-L455) - argument to cob_fatal_error
# ---------------------------------------------------------------------------
COB_FERROR_INITIALIZED = 0
COB_FERROR_CODEGEN = 1
COB_FERROR_CHAINING = 2
COB_FERROR_STACK = 3


# ---------------------------------------------------------------------------
# Number store flags (common.h L602-L605)
# ---------------------------------------------------------------------------
COB_STORE_ROUND = 0x01
COB_STORE_KEEP_ON_OVERFLOW = 0x02
COB_STORE_TRUNC_ON_OVERFLOW = 0x04


# ---------------------------------------------------------------------------
# Start conditions / sort order (common.h L476-L485)
# ---------------------------------------------------------------------------
COB_EQ = 1   # x == y
COB_LT = 2   # x <  y
COB_LE = 3   # x <= y
COB_GT = 4   # x >  y
COB_GE = 5   # x >= y
COB_NE = 6   # x != y

COB_ASCENDING = 0
COB_DESCENDING = 1

COB_FILE_MODE = 0o644


# ---------------------------------------------------------------------------
# File organization / access / lock / open / close / read / write modes
# (common.h L489-L558).  Reproduced for the generated code and fileio.py.
# ---------------------------------------------------------------------------
COB_ORG_SEQUENTIAL = 0
COB_ORG_LINE_SEQUENTIAL = 1
COB_ORG_RELATIVE = 2
COB_ORG_INDEXED = 3
COB_ORG_SORT = 4
COB_ORG_MAX = 5

COB_ACCESS_SEQUENTIAL = 1
COB_ACCESS_DYNAMIC = 2
COB_ACCESS_RANDOM = 3

COB_SELECT_FILE_STATUS = 0x01
COB_SELECT_EXTERNAL = 0x02
COB_SELECT_LINAGE = 0x04
COB_SELECT_SPLITKEY = 0x08

COB_LOCK_EXCLUSIVE = 1
COB_LOCK_MANUAL = 2
COB_LOCK_AUTOMATIC = 4
COB_LOCK_MULTIPLE = 8
COB_LOCK_MASK = 0x7

COB_OPEN_CLOSED = 0
COB_OPEN_INPUT = 1
COB_OPEN_OUTPUT = 2
COB_OPEN_I_O = 3
COB_OPEN_EXTEND = 4
COB_OPEN_LOCKED = 5

COB_CLOSE_NORMAL = 0
COB_CLOSE_LOCK = 1
COB_CLOSE_NO_REWIND = 2
COB_CLOSE_UNIT = 3
COB_CLOSE_UNIT_REMOVAL = 4

COB_WRITE_MASK = 0x0000FFFF
COB_WRITE_LINES = 0x00010000
COB_WRITE_PAGE = 0x00020000
COB_WRITE_CHANNEL = 0x00040000
COB_WRITE_AFTER = 0x00100000
COB_WRITE_BEFORE = 0x00200000
COB_WRITE_EOP = 0x00400000
COB_WRITE_LOCK = 0x00800000

COB_READ_NEXT = 0x01
COB_READ_PREVIOUS = 0x02
COB_READ_FIRST = 0x04
COB_READ_LAST = 0x08
COB_READ_LOCK = 0x10
COB_READ_NO_LOCK = 0x20
COB_READ_KEPT_LOCK = 0x40
COB_READ_WAIT_LOCK = 0x80
COB_READ_IGNORE_LOCK = 0x100

# File version + special statuses (common.h L474, L596-L598)
COB_FILE_VERSION = 0
COB_LINAGE_INVALID = 16384
COB_NOT_CONFIGURED = 32768


# ---------------------------------------------------------------------------
# Screen colours and attributes (common.h L608-L645) - consumed by screenio.py
# ---------------------------------------------------------------------------
COB_SCREEN_BLACK = 0
COB_SCREEN_BLUE = 1
COB_SCREEN_GREEN = 2
COB_SCREEN_CYAN = 3
COB_SCREEN_RED = 4
COB_SCREEN_MAGENTA = 5
COB_SCREEN_YELLOW = 6
COB_SCREEN_WHITE = 7

COB_SCREEN_LINE_PLUS = 0x00000001
COB_SCREEN_LINE_MINUS = 0x00000002
COB_SCREEN_COLUMN_PLUS = 0x00000004
COB_SCREEN_COLUMN_MINUS = 0x00000008
COB_SCREEN_AUTO = 0x00000010
COB_SCREEN_BELL = 0x00000020
COB_SCREEN_BLANK_LINE = 0x00000040
COB_SCREEN_BLANK_SCREEN = 0x00000080
COB_SCREEN_BLINK = 0x00000100
COB_SCREEN_ERASE_EOL = 0x00000200
COB_SCREEN_ERASE_EOS = 0x00000400
COB_SCREEN_FULL = 0x00000800
COB_SCREEN_HIGHLIGHT = 0x00001000
COB_SCREEN_LOWLIGHT = 0x00002000
COB_SCREEN_REQUIRED = 0x00004000
COB_SCREEN_REVERSE = 0x00008000
COB_SCREEN_SECURE = 0x00010000
COB_SCREEN_UNDERLINE = 0x00020000
COB_SCREEN_OVERLINE = 0x00040000
COB_SCREEN_PROMPT = 0x00080000
COB_SCREEN_UPDATE = 0x00100000
COB_SCREEN_INPUT = 0x00200000
COB_SCREEN_SCROLL_DOWN = 0x00400000

COB_SCREEN_TYPE_GROUP = 0
COB_SCREEN_TYPE_FIELD = 1
COB_SCREEN_TYPE_VALUE = 2
COB_SCREEN_TYPE_ATTRIBUTE = 3


# ---------------------------------------------------------------------------
# I-O status codes (common.h L562-L591) - used by fileio.py and generated code
# ---------------------------------------------------------------------------
COB_STATUS_00_SUCCESS = 0
COB_STATUS_02_SUCCESS_DUPLICATE = 2
COB_STATUS_04_SUCCESS_INCOMPLETE = 4
COB_STATUS_05_SUCCESS_OPTIONAL = 5
COB_STATUS_07_SUCCESS_NO_UNIT = 7
COB_STATUS_10_END_OF_FILE = 10
COB_STATUS_14_OUT_OF_KEY_RANGE = 14
COB_STATUS_21_KEY_INVALID = 21
COB_STATUS_22_KEY_EXISTS = 22
COB_STATUS_23_KEY_NOT_EXISTS = 23
COB_STATUS_30_PERMANENT_ERROR = 30
COB_STATUS_31_INCONSISTENT_FILENAME = 31
COB_STATUS_34_BOUNDARY_VIOLATION = 34
COB_STATUS_35_NOT_EXISTS = 35
COB_STATUS_37_PERMISSION_DENIED = 37
COB_STATUS_38_CLOSED_WITH_LOCK = 38
COB_STATUS_39_CONFLICT_ATTRIBUTE = 39
COB_STATUS_41_ALREADY_OPEN = 41
COB_STATUS_42_NOT_OPEN = 42
COB_STATUS_43_READ_NOT_DONE = 43
COB_STATUS_44_RECORD_OVERFLOW = 44
COB_STATUS_46_READ_ERROR = 46
COB_STATUS_47_INPUT_DENIED = 47
COB_STATUS_48_OUTPUT_DENIED = 48
COB_STATUS_49_I_O_DENIED = 49
COB_STATUS_51_RECORD_LOCKED = 51
COB_STATUS_52_EOP = 52
COB_STATUS_57_I_O_LINAGE = 57
COB_STATUS_61_FILE_SHARING = 61
COB_STATUS_91_NOT_AVAILABLE = 91


# ===========================================================================
# Field model (port of common.h L682-L740 / L411-L435)
#
# The generated Python program constructs ``cob_field`` / ``cob_field_attr``
# objects to describe its WORKING-STORAGE items, exactly as the C emitter
# constructs the corresponding C structs.  Keep this constructor / attribute
# API stable: emitted code and every sibling runtime module depends on it.
# ===========================================================================

class cob_field_attr(object):
    """Field attribute structure - mirror of the C ``cob_field_attr`` struct.

    Attributes (matching the C struct member order, common.h L682-L688):

    * ``type``   - one of the ``COB_TYPE_*`` constants.
    * ``digits`` - digit count for numeric items (0 otherwise).
    * ``scale``  - signed decimal scale (number of fractional digits;
                   may be negative for ``P`` scaling).
    * ``flags``  - bitwise OR of the ``COB_FLAG_*`` constants.
    * ``pic``    - the picture string (``str`` or ``None``).
    """

    __slots__ = ("type", "digits", "scale", "flags", "pic")

    def __init__(self, type=COB_TYPE_UNKNOWN, digits=0, scale=0,
                 flags=0, pic=None):
        self.type = type
        self.digits = digits
        self.scale = scale
        self.flags = flags
        self.pic = pic

    def __repr__(self):  # pragma: no cover - debugging aid only
        return ("cob_field_attr(type=0x%02x, digits=%d, scale=%d, "
                "flags=0x%02x, pic=%r)"
                % (self.type, self.digits, self.scale, self.flags, self.pic))


class cob_field(object):
    """Field structure - mirror of the C ``cob_field`` struct (common.h L692).

    Attributes:

    * ``size`` - the field size in bytes.
    * ``data`` - the field storage.  This MUST alias the underlying storage the
                 way the C ``cob_field.data`` pointer points INTO a program's
                 WORKING-STORAGE / record buffer: a ``bytearray`` is kept by
                 reference, and a ``memoryview`` (which the cobc emitter passes
                 as ``memoryview(b_N)[off:]`` so an elementary item points into
                 its owning record bytearray) is ALSO kept by reference -- never
                 copied -- so writes performed directly on the storage buffer
                 (the ``memcpy``/``memset``/``cob_setswp_*`` the emitter
                 generates for VALUE-clause and group initialisation) are
                 visible through this field, and writes through this field are
                 visible to any REDEFINES/overlapping field sharing the buffer.
                 In-place operations (sign overpunch, editing, MOVE) work on both
                 ``bytearray`` and a writable ``memoryview``.  An immutable
                 ``bytes`` literal (emitted constants) is copied into a private
                 ``bytearray`` so edit/sign writes remain possible.  ``None`` is
                 preserved (used to model an OMITTED argument).
    * ``attr`` - the associated :class:`cob_field_attr`.
    """

    __slots__ = ("size", "data", "attr")

    def __init__(self, size=0, data=None, attr=None):
        self.size = size
        if data is None:
            self.data = None
        elif isinstance(data, bytearray):
            self.data = data
        elif isinstance(data, memoryview):
            # MIGRATION (C->Python): a memoryview aliases its underlying storage
            # buffer, mirroring the C cob_field.data pointer that points INTO a
            # program's WORKING-STORAGE/record image.  Store it BY REFERENCE --
            # copying it (the previous behaviour) silently broke the aliasing
            # contract: VALUE-clause and group initialisation that the emitter
            # performs with memcpy/memset/cob_setswp_* directly on the storage
            # bytearray became invisible to the field, and REDEFINES/overlapping
            # items stopped sharing storage.  A memoryview of a bytearray is
            # writable and supports the indexed and equal-length slice writes the
            # runtime performs, so no copy is needed or wanted.
            self.data = data
        elif isinstance(data, bytes):
            # Immutable literal (emitted constants / figurative values): take a
            # private mutable copy so in-place sign/edit writes are possible
            # without mutating a shared read-only object.
            self.data = bytearray(data)
        elif isinstance(data, str):
            # Convenience for the figurative constants and literals; COBOL
            # data is single-byte (Latin-1) so encode 1:1.
            self.data = bytearray(data.encode("latin-1"))
        else:
            self.data = bytearray(data)
        self.attr = attr if attr is not None else cob_field_attr()

    def __repr__(self):  # pragma: no cover - debugging aid only
        return ("cob_field(size=%d, data=%r, type=0x%02x)"
                % (self.size, bytes(self.data) if self.data is not None
                   else None, self.attr.type if self.attr else 0))


class cob_external(object):
    """Linked-list node for EXTERNAL items (common.c struct ``cob_external``)."""

    __slots__ = ("next", "ext_alloc", "ename", "esize")

    def __init__(self):
        self.next = None
        self.ext_alloc = None
        self.ename = None
        self.esize = 0


class cob_module(object):
    """Module structure - mirror of the C ``struct cob_module`` (common.h L726).

    Holds the per-program runtime context maintained by
    :func:`cob_module_enter` / :func:`cob_module_leave`.  Defaults model the
    common ASCII, ``.``/``,`` configuration; the emitter overrides them per
    compilation unit.
    """

    __slots__ = (
        "next", "collating_sequence", "crt_status", "cursor_pos",
        "cob_procedure_parameters", "display_sign", "decimal_point",
        "currency_symbol", "numeric_separator", "flag_filename_mapping",
        "flag_binary_truncate", "flag_pretty_display", "spare8",
    )

    def __init__(self, collating_sequence=None, crt_status=None,
                 cursor_pos=None, cob_procedure_parameters=None,
                 display_sign=COB_DISPLAY_SIGN_ASCII, decimal_point=ord("."),
                 currency_symbol=ord("$"), numeric_separator=ord(","),
                 flag_filename_mapping=1, flag_binary_truncate=1,
                 flag_pretty_display=1):
        self.next = None
        self.collating_sequence = collating_sequence
        self.crt_status = crt_status
        self.cursor_pos = cursor_pos
        # COBOL procedure parameters are accessed positionally; default to an
        # empty list so ``cob_procedure_parameters[n]`` style lookups are safe.
        self.cob_procedure_parameters = (
            cob_procedure_parameters if cob_procedure_parameters is not None
            else [])
        self.display_sign = display_sign
        self.decimal_point = decimal_point
        self.currency_symbol = currency_symbol
        self.numeric_separator = numeric_separator
        self.flag_filename_mapping = flag_filename_mapping
        self.flag_binary_truncate = flag_binary_truncate
        self.flag_pretty_display = flag_pretty_display
        self.spare8 = 0


# ---------------------------------------------------------------------------
# Field-access helpers (port of the common.h L411-L435 accessor macros).
#
# In C these are preprocessor macros over ``cob_field *``; here they are plain
# functions taking a :class:`cob_field`.  Keeping the ``COB_FIELD_*`` spelling
# lets the emitter translate the C accessor macros 1:1.
# ---------------------------------------------------------------------------

def COB_FIELD_TYPE(f):
    """Return the field's type (one of ``COB_TYPE_*``)."""
    return f.attr.type


def COB_FIELD_DIGITS(f):
    """Return the field's digit count."""
    return f.attr.digits


def COB_FIELD_SCALE(f):
    """Return the field's decimal scale."""
    return f.attr.scale


def COB_FIELD_FLAGS(f):
    """Return the field's flag bits (``COB_FLAG_*``)."""
    return f.attr.flags


def COB_FIELD_PIC(f):
    """Return the field's picture string (or ``None``)."""
    return f.attr.pic


def COB_FIELD_HAVE_SIGN(f):
    """True if the field carries an operational sign."""
    return f.attr.flags & COB_FLAG_HAVE_SIGN


def COB_FIELD_SIGN_SEPARATE(f):
    """True if the sign occupies a separate leading/trailing byte."""
    return f.attr.flags & COB_FLAG_SIGN_SEPARATE


def COB_FIELD_SIGN_LEADING(f):
    """True if the sign is carried on the leading (first) digit/byte."""
    return f.attr.flags & COB_FLAG_SIGN_LEADING


def COB_FIELD_BLANK_ZERO(f):
    """True if BLANK WHEN ZERO applies."""
    return f.attr.flags & COB_FLAG_BLANK_ZERO


def COB_FIELD_JUSTIFIED(f):
    """True if the field is JUSTIFIED RIGHT."""
    return f.attr.flags & COB_FLAG_JUSTIFIED


def COB_FIELD_BINARY_SWAP(f):
    """True if the binary representation requires a byte swap."""
    return f.attr.flags & COB_FLAG_BINARY_SWAP


def COB_FIELD_REAL_BINARY(f):
    """True if the binary item uses the real (native) binary representation."""
    return f.attr.flags & COB_FLAG_REAL_BINARY


def COB_FIELD_IS_POINTER(f):
    """True if the field is a pointer item."""
    return f.attr.flags & COB_FLAG_IS_POINTER


def COB_FIELD_DATA(f):
    """Return the field's value data as a ``bytearray``.

    Mirrors the C ``COB_FIELD_DATA`` macro: when the sign is SEPARATE *and*
    LEADING the first byte holds the sign, so the value bytes start at offset
    one.  The returned object aliases ``f.data`` (it is a slice for the leading
    case, a direct reference otherwise) so callers must treat it as read-mostly
    when the leading-sign offset is in effect.
    """
    if COB_FIELD_SIGN_SEPARATE(f) and COB_FIELD_SIGN_LEADING(f):
        return f.data[1:]
    return f.data


def COB_FIELD_SIZE(f):
    """Return the value-byte count, excluding a SEPARATE sign byte."""
    return f.size - (1 if COB_FIELD_SIGN_SEPARATE(f) else 0)


def COB_FIELD_IS_NUMERIC(f):
    """True if the field type has the numeric class bit (0x10) set."""
    return COB_FIELD_TYPE(f) & COB_TYPE_NUMERIC


def cob_d2i(x):
    """Convert a digit byte (e.g. ord('0')) to its integer value (common.h L661)."""
    return x - ord("0")


def cob_i2d(x):
    """Convert an integer digit (0-9) to its ASCII byte value (common.h L664)."""
    return x + ord("0")


# ---------------------------------------------------------------------------
# Built-in figurative-constant fields (common.c L155-L181).
#
# ``all_attr`` is an ALPHANUMERIC_ALL attribute (used by ZERO/SPACE/HIGH/LOW/
# QUOTE); ``one_attr`` is a single-digit NUMERIC attribute used by ONE.
# ---------------------------------------------------------------------------
_all_attr = cob_field_attr(COB_TYPE_ALPHANUMERIC_ALL, 0, 0, 0, None)
_one_attr = cob_field_attr(COB_TYPE_NUMERIC, 1, 0, 0, None)

cob_zero = cob_field(1, bytearray(b"0"), _all_attr)
cob_space = cob_field(1, bytearray(b" "), _all_attr)
cob_high = cob_field(1, bytearray(b"\xff"), _all_attr)
cob_low = cob_field(1, bytearray(b"\x00"), _all_attr)
cob_quote = cob_field(1, bytearray(b'"'), _all_attr)
cob_one = cob_field(1, bytearray(b"1"), _one_attr)


# ===========================================================================
# Exception subsystem (port of libcob/exception.def + common.c L115-L168,
# L722-L749).
#
# NOTE (AAP section 0.6.1 rule 3): libcob/exception.def defines EXACTLY 146
# COB_EXCEPTION entries across 23 categories.  The AAP narrative's figure of
# "95" codes / "22" categories is a non-authoritative aggregate; the .def is
# authoritative, so ALL 146 codes and 23 categories are implemented here and
# the delta is logged in this comment per rule 3.
#
# The id enumeration below mirrors ``enum cob_exception_id`` (common.h L462):
# COB_EC_ZERO == 0, then the exception.def entries in definition order
# (COB_EC_ALL == 1 ... COB_EC_XML_RANGE == 146), then COB_EC_MAX.  The id is
# the index cob_set_exception() uses into the parallel code/name tables
# (mirror of cob_exception_tab_code[] / cob_exception_tab_name[],
# common.c L117-L129).
# ===========================================================================

from collections import namedtuple

COB_EC_ZERO = 0
COB_EC_ALL = 1
COB_EC_ARGUMENT = 2
COB_EC_ARGUMENT_FUNCTION = 3
COB_EC_ARGUMENT_IMP = 4
COB_EC_BOUND = 5
COB_EC_BOUND_IMP = 6
COB_EC_BOUND_ODO = 7
COB_EC_BOUND_OVERFLOW = 8
COB_EC_BOUND_PTR = 9
COB_EC_BOUND_REF_MOD = 10
COB_EC_BOUND_SET = 11
COB_EC_BOUND_SUBSCRIPT = 12
COB_EC_BOUND_TABLE_LIMIT = 13
COB_EC_DATA = 14
COB_EC_DATA_CONVERSION = 15
COB_EC_DATA_IMP = 16
COB_EC_DATA_INCOMPATIBLE = 17
COB_EC_DATA_INTEGRITY = 18
COB_EC_DATA_PTR_NULL = 19
COB_EC_DATA_INFINITY = 20
COB_EC_DATA_NEGATIVE_INFINITY = 21
COB_EC_DATA_NOT_A_NUMBER = 22
COB_EC_FLOW = 23
COB_EC_FLOW_GLOBAL_EXIT = 24
COB_EC_FLOW_GLOBAL_GOBACK = 25
COB_EC_FLOW_IMP = 26
COB_EC_FLOW_RELEASE = 27
COB_EC_FLOW_REPORT = 28
COB_EC_FLOW_RETURN = 29
COB_EC_FLOW_SEARCH = 30
COB_EC_FLOW_USE = 31
COB_EC_I_O = 32
COB_EC_I_O_AT_END = 33
COB_EC_I_O_EOP = 34
COB_EC_I_O_EOP_OVERFLOW = 35
COB_EC_I_O_FILE_SHARING = 36
COB_EC_I_O_IMP = 37
COB_EC_I_O_INVALID_KEY = 38
COB_EC_I_O_LINAGE = 39
COB_EC_I_O_LOGIC_ERROR = 40
COB_EC_I_O_PERMANENT_ERROR = 41
COB_EC_I_O_RECORD_OPERATION = 42
COB_EC_IMP = 43
COB_EC_IMP_ACCEPT = 44
COB_EC_IMP_DISPLAY = 45
COB_EC_LOCALE = 46
COB_EC_LOCALE_IMP = 47
COB_EC_LOCALE_INCOMPATIBLE = 48
COB_EC_LOCALE_INVALID = 49
COB_EC_LOCALE_INVALID_PTR = 50
COB_EC_LOCALE_MISSING = 51
COB_EC_LOCALE_SIZE = 52
COB_EC_OO = 53
COB_EC_OO_CONFORMANCE = 54
COB_EC_OO_EXCEPTION = 55
COB_EC_OO_IMP = 56
COB_EC_OO_METHOD = 57
COB_EC_OO_NULL = 58
COB_EC_OO_RESOURCE = 59
COB_EC_OO_UNIVERSAL = 60
COB_EC_ORDER = 61
COB_EC_ORDER_IMP = 62
COB_EC_ORDER_NOT_SUPPORTED = 63
COB_EC_OVERFLOW = 64
COB_EC_OVERFLOW_IMP = 65
COB_EC_OVERFLOW_STRING = 66
COB_EC_OVERFLOW_UNSTRING = 67
COB_EC_PROGRAM = 68
COB_EC_PROGRAM_ARG_MISMATCH = 69
COB_EC_PROGRAM_ARG_OMITTED = 70
COB_EC_PROGRAM_CANCEL_ACTIVE = 71
COB_EC_PROGRAM_IMP = 72
COB_EC_PROGRAM_NOT_FOUND = 73
COB_EC_PROGRAM_PTR_NULL = 74
COB_EC_PROGRAM_RECURSIVE_CALL = 75
COB_EC_PROGRAM_RESOURCES = 76
COB_EC_RAISING = 77
COB_EC_RAISING_IMP = 78
COB_EC_RAISING_NOT_SPECIFIED = 79
COB_EC_RANGE = 80
COB_EC_RANGE_IMP = 81
COB_EC_RANGE_INDEX = 82
COB_EC_RANGE_INSPECT_SIZE = 83
COB_EC_RANGE_INVALID = 84
COB_EC_RANGE_PERFORM_VARYING = 85
COB_EC_RANGE_PTR = 86
COB_EC_RANGE_SEARCH_INDEX = 87
COB_EC_RANGE_SEARCH_NO_MATCH = 88
COB_EC_REPORT = 89
COB_EC_REPORT_ACTIVE = 90
COB_EC_REPORT_COLUMN_OVERLAP = 91
COB_EC_REPORT_FILE_MODE = 92
COB_EC_REPORT_IMP = 93
COB_EC_REPORT_INACTIVE = 94
COB_EC_REPORT_LINE_OVERLAP = 95
COB_EC_REPORT_NOT_TERMINATED = 96
COB_EC_REPORT_PAGE_LIMIT = 97
COB_EC_REPORT_PAGE_WIDTH = 98
COB_EC_REPORT_SUM_SIZE = 99
COB_EC_REPORT_VARYING = 100
COB_EC_SCREEN = 101
COB_EC_SCREEN_FIELD_OVERLAP = 102
COB_EC_SCREEN_IMP = 103
COB_EC_SCREEN_ITEM_TRUNCATED = 104
COB_EC_SCREEN_LINE_NUMBER = 105
COB_EC_SCREEN_STARTING_COLUMN = 106
COB_EC_SIZE = 107
COB_EC_SIZE_ADDRESS = 108
COB_EC_SIZE_EXPONENTIATION = 109
COB_EC_SIZE_IMP = 110
COB_EC_SIZE_OVERFLOW = 111
COB_EC_SIZE_TRUNCATION = 112
COB_EC_SIZE_UNDERFLOW = 113
COB_EC_SIZE_ZERO_DIVIDE = 114
COB_EC_SORT_MERGE = 115
COB_EC_SORT_MERGE_ACTIVE = 116
COB_EC_SORT_MERGE_FILE_OPEN = 117
COB_EC_SORT_MERGE_IMP = 118
COB_EC_SORT_MERGE_RELEASE = 119
COB_EC_SORT_MERGE_RETURN = 120
COB_EC_SORT_MERGE_SEQUENCE = 121
COB_EC_STORAGE = 122
COB_EC_STORAGE_IMP = 123
COB_EC_STORAGE_NOT_ALLOC = 124
COB_EC_STORAGE_NOT_AVAIL = 125
COB_EC_USER = 126
COB_EC_VALIDATE = 127
COB_EC_VALIDATE_CONTENT = 128
COB_EC_VALIDATE_FORMAT = 129
COB_EC_VALIDATE_IMP = 130
COB_EC_VALIDATE_RELATION = 131
COB_EC_VALIDATE_VARYING = 132
COB_EC_FUNCTION = 133
COB_EC_FUNCTION_NOT_FOUND = 134
COB_EC_FUNCTION_PTR_INVALID = 135
COB_EC_FUNCTION_PTR_NULL = 136
COB_EC_XML = 137
COB_EC_XML_CODESET = 138
COB_EC_XML_CODESET_CONVERSION = 139
COB_EC_XML_COUNT = 140
COB_EC_XML_DOCUMENT_TYPE = 141
COB_EC_XML_IMPLICIT_CLOSE = 142
COB_EC_XML_INVALID = 143
COB_EC_XML_NAMESPACE = 144
COB_EC_XML_STACKED_OPEN = 145
COB_EC_XML_RANGE = 146
COB_EC_MAX = 147


_ExcEntry = namedtuple("_ExcEntry", ("code", "id", "name", "critical"))


def _EXC(code, ec_id, name, critical):
    """Build one exception-table row (code, enum id, EC-name, critical flag)."""
    return _ExcEntry(code, ec_id, name, critical)


#: Ordered table of all 146 COBOL exception conditions (definition order).
#: ``len(EXCEPTION_TABLE) == 146`` and it spans 23 distinct categories.
EXCEPTION_TABLE = [
    _EXC(0xFFFF, COB_EC_ALL, "EC-ALL", 0),
    _EXC(0x0100, COB_EC_ARGUMENT, "EC-ARGUMENT", 0),
    _EXC(0x0101, COB_EC_ARGUMENT_FUNCTION, "EC-ARGUMENT-FUNCTION", 1),
    _EXC(0x0102, COB_EC_ARGUMENT_IMP, "EC-ARGUMENT-IMP", 0),
    _EXC(0x0200, COB_EC_BOUND, "EC-BOUND", 0),
    _EXC(0x0201, COB_EC_BOUND_IMP, "EC-BOUND-IMP", 0),
    _EXC(0x0202, COB_EC_BOUND_ODO, "EC-BOUND-ODO", 1),
    _EXC(0x0203, COB_EC_BOUND_OVERFLOW, "EC-BOUND-OVERFLOW", 1),
    _EXC(0x0204, COB_EC_BOUND_PTR, "EC-BOUND-PTR", 1),
    _EXC(0x0205, COB_EC_BOUND_REF_MOD, "EC-BOUND-REF-MOD", 1),
    _EXC(0x0206, COB_EC_BOUND_SET, "EC-BOUND-SET", 1),
    _EXC(0x0207, COB_EC_BOUND_SUBSCRIPT, "EC-BOUND-SUBSCRIPT", 1),
    _EXC(0x0208, COB_EC_BOUND_TABLE_LIMIT, "EC-BOUND-TABLE-LIMIT", 1),
    _EXC(0x0300, COB_EC_DATA, "EC-DATA", 0),
    _EXC(0x0301, COB_EC_DATA_CONVERSION, "EC-DATA-CONVERSION", 0),
    _EXC(0x0302, COB_EC_DATA_IMP, "EC-DATA-IMP", 0),
    _EXC(0x0303, COB_EC_DATA_INCOMPATIBLE, "EC-DATA-INCOMPATIBLE", 1),
    _EXC(0x0304, COB_EC_DATA_INTEGRITY, "EC-DATA-INTEGRITY", 1),
    _EXC(0x0305, COB_EC_DATA_PTR_NULL, "EC-DATA-PTR-NULL", 1),
    _EXC(0x0306, COB_EC_DATA_INFINITY, "EC-DATA-INFINITY", 1),
    _EXC(0x0307, COB_EC_DATA_NEGATIVE_INFINITY, "EC-DATA-NEGATIVE-INFINITY", 1),
    _EXC(0x0308, COB_EC_DATA_NOT_A_NUMBER, "EC-DATA-NOT_A_NUMBER", 1),
    _EXC(0x0400, COB_EC_FLOW, "EC-FLOW", 0),
    _EXC(0x0401, COB_EC_FLOW_GLOBAL_EXIT, "EC-FLOW-GLOBAL-EXIT", 1),
    _EXC(0x0402, COB_EC_FLOW_GLOBAL_GOBACK, "EC-FLOW-GLOBAL-GOBACK", 1),
    _EXC(0x0403, COB_EC_FLOW_IMP, "EC-FLOW-IMP", 0),
    _EXC(0x0404, COB_EC_FLOW_RELEASE, "EC-FLOW-RELEASE", 1),
    _EXC(0x0405, COB_EC_FLOW_REPORT, "EC-FLOW-REPORT", 1),
    _EXC(0x0406, COB_EC_FLOW_RETURN, "EC-FLOW-RETURN", 1),
    _EXC(0x0407, COB_EC_FLOW_SEARCH, "EC-FLOW-SEARCH", 1),
    _EXC(0x0408, COB_EC_FLOW_USE, "EC-FLOW-USE", 1),
    _EXC(0x0500, COB_EC_I_O, "EC-I-O", 0),
    _EXC(0x0501, COB_EC_I_O_AT_END, "EC-I-O-AT-END", 0),
    _EXC(0x0502, COB_EC_I_O_EOP, "EC-I-O-EOP", 0),
    _EXC(0x0503, COB_EC_I_O_EOP_OVERFLOW, "EC-I-O-EOP-OVERFLOW", 0),
    _EXC(0x0504, COB_EC_I_O_FILE_SHARING, "EC-I-O-FILE-SHARING", 0),
    _EXC(0x0505, COB_EC_I_O_IMP, "EC-I-O-IMP", 0),
    _EXC(0x0506, COB_EC_I_O_INVALID_KEY, "EC-I-O-INVALID-KEY", 0),
    _EXC(0x0507, COB_EC_I_O_LINAGE, "EC-I-O-LINAGE", 1),
    _EXC(0x0508, COB_EC_I_O_LOGIC_ERROR, "EC-I-O-LOGIC-ERROR", 1),
    _EXC(0x0509, COB_EC_I_O_PERMANENT_ERROR, "EC-I-O-PERMANENT-ERROR", 1),
    _EXC(0x050A, COB_EC_I_O_RECORD_OPERATION, "EC-I-O-RECORD-OPERATION", 0),
    _EXC(0x0600, COB_EC_IMP, "EC-IMP", 0),
    _EXC(0x0601, COB_EC_IMP_ACCEPT, "EC-IMP-ACCEPT", 0),
    _EXC(0x0602, COB_EC_IMP_DISPLAY, "EC-IMP-DISPLAY", 0),
    _EXC(0x0700, COB_EC_LOCALE, "EC-LOCALE", 0),
    _EXC(0x0701, COB_EC_LOCALE_IMP, "EC-LOCALE-IMP", 0),
    _EXC(0x0702, COB_EC_LOCALE_INCOMPATIBLE, "EC-LOCALE-INCOMPATIBLE", 0),
    _EXC(0x0703, COB_EC_LOCALE_INVALID, "EC-LOCALE-INVALID", 1),
    _EXC(0x0704, COB_EC_LOCALE_INVALID_PTR, "EC-LOCALE-INVALID-PTR", 1),
    _EXC(0x0705, COB_EC_LOCALE_MISSING, "EC-LOCALE-MISSING", 1),
    _EXC(0x0706, COB_EC_LOCALE_SIZE, "EC-LOCALE-SIZE", 1),
    _EXC(0x0800, COB_EC_OO, "EC-OO", 0),
    _EXC(0x0801, COB_EC_OO_CONFORMANCE, "EC-OO-CONFORMANCE", 1),
    _EXC(0x0802, COB_EC_OO_EXCEPTION, "EC-OO-EXCEPTION", 1),
    _EXC(0x0803, COB_EC_OO_IMP, "EC-OO-IMP", 0),
    _EXC(0x0804, COB_EC_OO_METHOD, "EC-OO-METHOD", 1),
    _EXC(0x0805, COB_EC_OO_NULL, "EC-OO-NULL", 1),
    _EXC(0x0806, COB_EC_OO_RESOURCE, "EC-OO-RESOURCE", 1),
    _EXC(0x0807, COB_EC_OO_UNIVERSAL, "EC-OO-UNIVERSAL", 1),
    _EXC(0x0900, COB_EC_ORDER, "EC-ORDER", 0),
    _EXC(0x0901, COB_EC_ORDER_IMP, "EC-ORDER-IMP", 0),
    _EXC(0x0902, COB_EC_ORDER_NOT_SUPPORTED, "EC-ORDER-NOT-SUPPORTED", 1),
    _EXC(0x0A00, COB_EC_OVERFLOW, "EC-OVERFLOW", 0),
    _EXC(0x0A01, COB_EC_OVERFLOW_IMP, "EC-OVERFLOW-IMP", 0),
    _EXC(0x0A02, COB_EC_OVERFLOW_STRING, "EC-OVERFLOW-STRING", 0),
    _EXC(0x0A03, COB_EC_OVERFLOW_UNSTRING, "EC-OVERFLOW-UNSTRING", 0),
    _EXC(0x0B00, COB_EC_PROGRAM, "EC-PROGRAM", 0),
    _EXC(0x0B01, COB_EC_PROGRAM_ARG_MISMATCH, "EC-PROGRAM-ARG-MISMATCH", 1),
    _EXC(0x0B02, COB_EC_PROGRAM_ARG_OMITTED, "EC-PROGRAM-ARG-OMITTED", 1),
    _EXC(0x0B03, COB_EC_PROGRAM_CANCEL_ACTIVE, "EC-PROGRAM-CANCEL-ACTIVE", 1),
    _EXC(0x0B04, COB_EC_PROGRAM_IMP, "EC-PROGRAM-IMP", 0),
    _EXC(0x0B05, COB_EC_PROGRAM_NOT_FOUND, "EC-PROGRAM-NOT-FOUND", 1),
    _EXC(0x0B06, COB_EC_PROGRAM_PTR_NULL, "EC-PROGRAM-PTR-NULL", 1),
    _EXC(0x0B07, COB_EC_PROGRAM_RECURSIVE_CALL, "EC-PROGRAM-RECURSIVE-CALL", 1),
    _EXC(0x0B08, COB_EC_PROGRAM_RESOURCES, "EC-PROGRAM-RESOURCES", 1),
    _EXC(0x0C00, COB_EC_RAISING, "EC-RAISING", 0),
    _EXC(0x0C01, COB_EC_RAISING_IMP, "EC-RAISING-IMP", 0),
    _EXC(0x0C02, COB_EC_RAISING_NOT_SPECIFIED, "EC-RAISING-NOT-SPECIFIED", 1),
    _EXC(0x0D00, COB_EC_RANGE, "EC-RANGE", 0),
    _EXC(0x0D01, COB_EC_RANGE_IMP, "EC-RANGE-IMP", 0),
    _EXC(0x0D02, COB_EC_RANGE_INDEX, "EC-RANGE-INDEX", 1),
    _EXC(0x0D03, COB_EC_RANGE_INSPECT_SIZE, "EC-RANGE-INSPECT-SIZE", 1),
    _EXC(0x0D04, COB_EC_RANGE_INVALID, "EC-RANGE-INVALID", 0),
    _EXC(0x0D05, COB_EC_RANGE_PERFORM_VARYING, "EC-RANGE-PERFORM-VARYING", 1),
    _EXC(0x0D06, COB_EC_RANGE_PTR, "EC-RANGE-PTR", 1),
    _EXC(0x0D07, COB_EC_RANGE_SEARCH_INDEX, "EC-RANGE-SEARCH-INDEX", 0),
    _EXC(0x0D08, COB_EC_RANGE_SEARCH_NO_MATCH, "EC-RANGE-SEARCH-NO-MATCH", 0),
    _EXC(0x0E00, COB_EC_REPORT, "EC-REPORT", 0),
    _EXC(0x0E01, COB_EC_REPORT_ACTIVE, "EC-REPORT-ACTIVE", 1),
    _EXC(0x0E02, COB_EC_REPORT_COLUMN_OVERLAP, "EC-REPORT-COLUMN-OVERLAP", 1),
    _EXC(0x0E03, COB_EC_REPORT_FILE_MODE, "EC-REPORT-FILE-MODE", 1),
    _EXC(0x0E04, COB_EC_REPORT_IMP, "EC-REPORT-IMP", 0),
    _EXC(0x0E05, COB_EC_REPORT_INACTIVE, "EC-REPORT-INACTIVE", 1),
    _EXC(0x0E06, COB_EC_REPORT_LINE_OVERLAP, "EC-REPORT-LINE-OVERLAP", 0),
    _EXC(0x0E08, COB_EC_REPORT_NOT_TERMINATED, "EC-REPORT-NOT-TERMINATED", 0),
    _EXC(0x0E09, COB_EC_REPORT_PAGE_LIMIT, "EC-REPORT-PAGE-LIMIT", 0),
    _EXC(0x0E0A, COB_EC_REPORT_PAGE_WIDTH, "EC-REPORT-PAGE-WIDTH", 0),
    _EXC(0x0E0B, COB_EC_REPORT_SUM_SIZE, "EC-REPORT-SUM-SIZE", 1),
    _EXC(0x0E0C, COB_EC_REPORT_VARYING, "EC-REPORT-VARYING", 1),
    _EXC(0x0F00, COB_EC_SCREEN, "EC-SCREEN", 0),
    _EXC(0x0F01, COB_EC_SCREEN_FIELD_OVERLAP, "EC-SCREEN-FIELD-OVERLAP", 0),
    _EXC(0x0F02, COB_EC_SCREEN_IMP, "EC-SCREEN-IMP", 0),
    _EXC(0x0F03, COB_EC_SCREEN_ITEM_TRUNCATED, "EC-SCREEN-ITEM-TRUNCATED", 0),
    _EXC(0x0F04, COB_EC_SCREEN_LINE_NUMBER, "EC-SCREEN-LINE-NUMBER", 0),
    _EXC(0x0F05, COB_EC_SCREEN_STARTING_COLUMN, "EC-SCREEN-STARTING-COLUMN", 0),
    _EXC(0x1000, COB_EC_SIZE, "EC-SIZE", 0),
    _EXC(0x1001, COB_EC_SIZE_ADDRESS, "EC-SIZE-ADDRESS", 1),
    _EXC(0x1002, COB_EC_SIZE_EXPONENTIATION, "EC-SIZE-EXPONENTIATION", 1),
    _EXC(0x1003, COB_EC_SIZE_IMP, "EC-SIZE-IMP", 0),
    _EXC(0x1004, COB_EC_SIZE_OVERFLOW, "EC-SIZE-OVERFLOW", 1),
    _EXC(0x1005, COB_EC_SIZE_TRUNCATION, "EC-SIZE-TRUNCATION", 1),
    _EXC(0x1006, COB_EC_SIZE_UNDERFLOW, "EC-SIZE-UNDERFLOW", 1),
    _EXC(0x1007, COB_EC_SIZE_ZERO_DIVIDE, "EC-SIZE-ZERO-DIVIDE", 1),
    _EXC(0x1100, COB_EC_SORT_MERGE, "EC-SORT-MERGE", 0),
    _EXC(0x1101, COB_EC_SORT_MERGE_ACTIVE, "EC-SORT-MERGE-ACTIVE", 1),
    _EXC(0x1102, COB_EC_SORT_MERGE_FILE_OPEN, "EC-SORT-MERGE-FILE-OPEN", 1),
    _EXC(0x1103, COB_EC_SORT_MERGE_IMP, "EC-SORT-MERGE-IMP", 0),
    _EXC(0x1104, COB_EC_SORT_MERGE_RELEASE, "EC-SORT-MERGE-RELEASE", 1),
    _EXC(0x1105, COB_EC_SORT_MERGE_RETURN, "EC-SORT-MERGE-RETURN", 1),
    _EXC(0x1106, COB_EC_SORT_MERGE_SEQUENCE, "EC-SORT-MERGE-SEQUENCE", 1),
    _EXC(0x1200, COB_EC_STORAGE, "EC-STORAGE", 0),
    _EXC(0x1201, COB_EC_STORAGE_IMP, "EC-STORAGE-IMP", 0),
    _EXC(0x1202, COB_EC_STORAGE_NOT_ALLOC, "EC-STORAGE-NOT-ALLOC", 0),
    _EXC(0x1203, COB_EC_STORAGE_NOT_AVAIL, "EC-STORAGE-NOT-AVAIL", 0),
    _EXC(0x1300, COB_EC_USER, "EC-USER", 0),
    _EXC(0x1400, COB_EC_VALIDATE, "EC-VALIDATE", 0),
    _EXC(0x1401, COB_EC_VALIDATE_CONTENT, "EC-VALIDATE-CONTENT", 0),
    _EXC(0x1402, COB_EC_VALIDATE_FORMAT, "EC-VALIDATE-FORMAT", 0),
    _EXC(0x1403, COB_EC_VALIDATE_IMP, "EC-VALIDATE-IMP", 0),
    _EXC(0x1404, COB_EC_VALIDATE_RELATION, "EC-VALIDATE-RELATION", 0),
    _EXC(0x1405, COB_EC_VALIDATE_VARYING, "EC-VALIDATE-VARYING", 1),
    _EXC(0x1500, COB_EC_FUNCTION, "EC-FUNCTION", 0),
    _EXC(0x1501, COB_EC_FUNCTION_NOT_FOUND, "EC-FUNCTION-NOT-FOUND", 1),
    _EXC(0x1502, COB_EC_FUNCTION_PTR_INVALID, "EC-FUNCTION-PTR-INVALID", 1),
    _EXC(0x1503, COB_EC_FUNCTION_PTR_NULL, "EC-FUNCTION-PTR-NULL", 1),
    _EXC(0x1600, COB_EC_XML, "EC-XML", 0),
    _EXC(0x1601, COB_EC_XML_CODESET, "EC-XML-CODESET", 1),
    _EXC(0x1602, COB_EC_XML_CODESET_CONVERSION, "EC-XML-CODESET-CONVERSION", 1),
    _EXC(0x1603, COB_EC_XML_COUNT, "EC-XML-COUNT", 1),
    _EXC(0x1604, COB_EC_XML_DOCUMENT_TYPE, "EC-XML-DOCUMENT-TYPE", 1),
    _EXC(0x1605, COB_EC_XML_IMPLICIT_CLOSE, "EC-XML-IMPLICIT-CLOSE", 1),
    _EXC(0x1606, COB_EC_XML_INVALID, "EC-XML-INVALID", 1),
    _EXC(0x1607, COB_EC_XML_NAMESPACE, "EC-XML-NAMESPACE", 1),
    _EXC(0x1608, COB_EC_XML_STACKED_OPEN, "EC-XML-STACKED-OPEN", 1),
    _EXC(0x1609, COB_EC_XML_RANGE, "EC-XML-RANGE", 1),
]

# Sanity guard - keep the authoritative counts honest at import time.
assert len(EXCEPTION_TABLE) == 146, (
    "exception table must contain exactly 146 entries"
)

#: Number of distinct exception categories (high byte of the code; EC-ALL's
#: 0xFFFF counts as its own category).  Equals 23.
EXCEPTION_CATEGORY_COUNT = len(
    {0xFFFF if e.code == 0xFFFF else (e.code >> 8) for e in EXCEPTION_TABLE}
)

# Parallel code/name lookup tables indexed by enum id, with the COB_EC_ZERO
# (index 0) and COB_EC_MAX (final index) sentinels carrying code 0 / name
# None - exactly like the C arrays.
_TAB_CODE = [0] + [e.code for e in EXCEPTION_TABLE] + [0]
_TAB_NAME = [None] + [e.name for e in EXCEPTION_TABLE] + [None]


# ===========================================================================
# Global runtime state (port of the file-scope globals in common.c L139-L174).
#
# These are genuine module globals so that sibling runtime modules and the
# generated program observe a single shared runtime state, exactly as the C
# runtime shares process globals.  Functions that mutate them use ``global``.
# ===========================================================================

#: Set once cob_init() has completed (common.c L162).
cob_initialized = 0
#: The most recently raised exception's hex code (common.c L163).
cob_exception_code = 0
#: Latched to 1 once any non-zero exception has been raised (common.c L168).
cob_got_exception = 0

#: Number of parameters passed on the current CALL (common.c L165).
cob_call_params = 0
#: Saved copy of cob_call_params across a nested CALL (common.c L166).
cob_save_call_params = 0
#: Set to 1 when an EXTERNAL item is first allocated (common.c L167).
cob_initial_external = 0

#: Head of the active module chain (common.c L160).  ``None`` == no module.
cob_current_module = None

#: PROGRAM COLLATING / source-location bookkeeping (common.c L96-L102, L170-174).
cob_current_program_id = None
cob_current_section = None
cob_current_paragraph = None
cob_source_file = None
cob_source_statement = None
cob_source_line = 0
cob_orig_statement = None
cob_orig_program_id = None
cob_orig_section = None
cob_orig_paragraph = None
cob_orig_line = 0

#: COBOL implementor switches SWITCH-1 .. SWITCH-8 (common.c L139).
_cob_switch = [0] * 8

#: Whether line tracing is active (common.c L102).
_cob_line_trace = 0

# Internal bookkeeping mirrored from common.c file-scope statics.
_cob_argc = 0
_cob_argv = []
_cob_local_env = None
_current_arg = 1
_commln = None             # DISPLAY UPON COMMAND-LINE buffer (common.c L87-88)
_cob_alloc_base = []       # ALLOCATE/FREE bookkeeping cache (common.c L83)
_externals = {}            # EXTERNAL item store (common.c struct cob_external)
_exit_handlers = []        # CBL_EXIT_PROC handler stack (common.c L142)
_error_handlers = []       # CBL_ERROR_PROC handler stack (common.c L148)

# Table-sort working state (common.c L92-L94).
_sort_nkeys = 0
_sort_keys = []
_sort_collate = None


# ---------------------------------------------------------------------------
# Exception accessors (common.c L722-L746)
# ---------------------------------------------------------------------------

def cob_get_exception_name(exception_code):
    """Return the ``EC-...`` name for *exception_code*, or ``None``.

    Linear scan of the code table, mirroring common.c L722-L731.  Lookups by
    hex code (e.g. ``0x0F03``) return the matching exception name; an unknown
    code returns ``None``.
    """
    for n, code in enumerate(_TAB_CODE):
        if exception_code == code and n != 0 and n != COB_EC_MAX:
            return _TAB_NAME[n]
    return None


def cob_set_exception(ec_id):
    """Raise the exception identified by enum id *ec_id* (common.c L734-L746).

    Sets :data:`cob_exception_code` to the table code for *ec_id*.  When that
    code is non-zero, latches :data:`cob_got_exception` and snapshots the
    current source location (program-id / section / paragraph / line /
    statement), exactly as the C runtime does.
    """
    global cob_exception_code, cob_got_exception
    global cob_orig_statement, cob_orig_line, cob_orig_program_id
    global cob_orig_section, cob_orig_paragraph

    cob_exception_code = _TAB_CODE[ec_id]
    if cob_exception_code:
        cob_got_exception = 1
        cob_orig_statement = cob_source_statement
        cob_orig_line = cob_source_line
        cob_orig_program_id = cob_current_program_id
        cob_orig_section = cob_current_section
        cob_orig_paragraph = cob_current_paragraph


# ===========================================================================
# Allocation lifecycle (common.c L646-L657, L1644-L1705)
# ===========================================================================

def cob_malloc(size):
    """Allocate *size* zero-initialised bytes (common.c L646-L657).

    Returns a fresh ``bytearray(size)`` (calloc semantics).  Python's garbage
    collector reclaims the storage automatically, so there is no explicit
    ``free`` - releasing such a buffer is a no-op (just drop the reference).
    """
    try:
        return bytearray(int(size))
    except (ValueError, MemoryError):
        # Mirror the C abort-on-OOM behaviour (common.c L652-L655).
        cob_runtime_error(
            "Cannot acquire %d bytes of memory - Aborting", size)
        cob_stop_run(1)


def cob_allocate(dataptr, retptr, sizefld):
    """ALLOCATE statement support (common.c L1644-L1673).

    *sizefld* is a numeric :class:`cob_field` giving the byte count.  On
    success a zero-filled ``bytearray`` is recorded in the allocation cache and
    returned via *dataptr* (a single-element list used as an out-parameter) and
    stored into *retptr*'s pointer slot when supplied.  When the request cannot
    be satisfied, ``EC-STORAGE-NOT-AVAIL`` is raised.  Returns the allocated
    ``bytearray`` (or ``None``).
    """
    global cob_exception_code

    cob_exception_code = 0
    mptr = None
    fsize = _lazy_get_int(sizefld)
    if fsize > 0:
        try:
            mptr = bytearray(fsize)
        except MemoryError:
            mptr = None
        if mptr is None:
            cob_set_exception(COB_EC_STORAGE_NOT_AVAIL)
        else:
            _cob_alloc_base.append(mptr)
    if dataptr is not None:
        # Out-parameter modelled as a one-element mutable sequence.
        try:
            dataptr[0] = mptr
        except (TypeError, IndexError):
            pass
    if retptr is not None and getattr(retptr, "data", None) is not None:
        retptr.data = mptr
    return mptr


def cob_free_alloc(ptr1, ptr2):
    """FREE statement support (common.c L1675-L1705).

    Releases storage previously produced by :func:`cob_allocate`.  *ptr1* /
    *ptr2* are one-element out-parameter lists (data-pointer / based item).
    Freeing storage that was never allocated raises ``EC-STORAGE-NOT-ALLOC``,
    matching the C contract - no silent success.
    """
    global cob_exception_code

    cob_exception_code = 0
    for holder in (ptr1, ptr2):
        if holder is None:
            continue
        try:
            target = holder[0]
        except (TypeError, IndexError):
            continue
        if target is None:
            continue
        for idx, cached in enumerate(_cob_alloc_base):
            if cached is target:
                del _cob_alloc_base[idx]
                holder[0] = None
                return
        cob_set_exception(COB_EC_STORAGE_NOT_ALLOC)
        return


# ===========================================================================
# Runtime error reporting (common.c L847-L928)
# ===========================================================================

def cob_runtime_error(fmt, *args):
    """Report a run-time error to stderr (common.c L847-L892).

    Honours any installed CBL_ERROR_PROC handlers first (each is invoked once
    with the formatted message and the handler list is then cleared, matching
    the C behaviour), then prints the ``libcob:`` prefixed message - preceded
    by ``file:line:`` when a source location is known.
    """
    global _error_handlers

    try:
        message = fmt % args if args else str(fmt)
    except (TypeError, ValueError):
        message = str(fmt)

    if _error_handlers:
        prefix = ""
        if cob_source_file:
            prefix = "%s:%d: " % (cob_source_file, cob_source_line)
        full = prefix + message
        for handler in _error_handlers:
            try:
                handler(full)
            except Exception:  # pragma: no cover - defensive: handler misbehaving
                pass
        _error_handlers = []

    if cob_source_file:
        sys.stderr.write("%s:%d: " % (cob_source_file, cob_source_line))
    sys.stderr.write("libcob: ")
    sys.stderr.write(message)
    sys.stderr.write("\n")
    sys.stderr.flush()


def cob_fatal_error(fatal_error):
    """Report a fatal runtime error and stop (common.c L894-L915)."""
    if fatal_error == COB_FERROR_INITIALIZED:
        cob_runtime_error("cob_init() has not been called")
    elif fatal_error == COB_FERROR_CODEGEN:
        cob_runtime_error("Codegen error - Please report this")
    elif fatal_error == COB_FERROR_CHAINING:
        cob_runtime_error("ERROR - Recursive call of chained program")
    elif fatal_error == COB_FERROR_STACK:
        cob_runtime_error("Stack overflow, possible PERFORM depth exceeded")
    else:
        cob_runtime_error("Unknown failure : %d", int(fatal_error))
    cob_stop_run(1)


def cob_check_version(prog, packver, patchlev):
    """Runtime/library version guard (common.c L917-L928).

    Under the Python backend the package/patch level are advisory.  A mismatch
    is reported (and stops the run) exactly as the C runtime did when the
    generated program was built against a different library version.
    """
    if packver != PACKAGE_VERSION or patchlev > PATCH_LEVEL:
        cob_runtime_error("Error - Version mismatch")
        cob_runtime_error("%s has version/patch level %s/%d",
                          prog, packver, patchlev)
        cob_runtime_error("Library has version/patch level %s/%d",
                          PACKAGE_VERSION, PATCH_LEVEL)
        cob_stop_run(1)


# ===========================================================================
# Runtime initialisation / module lifecycle (common.c L748-L845)
# ===========================================================================

#: Package/patch identification used by cob_check_version.  Mirrors the C
#: PACKAGE_VERSION / PATCH_LEVEL macros.  PACKAGE_VERSION MUST byte-match the
#: value the cobc emitter writes as COB_PACKAGE_VERSION (codegen.c emits
#: PACKAGE_VERSION verbatim), which is the autoconf AC_INIT version in
#: configure.ac ("1.1") and the config.h "#define PACKAGE_VERSION \"1.1\"".
#: cob_check_version does an exact strcmp (libcob/common.c L920), so a value of
#: "1.1.0" here would spuriously fail the version check against the emitted
#: "1.1" and abort every generated program with cob_stop_run(1).
PACKAGE_VERSION = "1.1"
PATCH_LEVEL = 0

#: Subsystem initialisation order (common.c L784-L790).  The presence of
#: ``move`` between ``strings`` and ``intrinsic`` is preserved exactly - it is
#: the dedicated data-movement module (AAP section 0.6.5).
COB_INIT_ORDER = (
    "numeric", "strings", "move", "intrinsic", "fileio", "termio", "call",
)


#: Environment flag that relaxes :func:`_run_subsystem_initializers` so that a
#: genuinely-absent subsystem module is skipped instead of raising.  This is the
#: *test-only* escape hatch: a unit test that exercises ``common`` (or one
#: sibling) in isolation, in an environment where the other runtime modules are
#: deliberately not importable, sets ``COB_PY_INIT_OPTIONAL=1``.  Production code
#: never sets it, so a missing required module is always a hard error.
_INIT_OPTIONAL_ENV = "COB_PY_INIT_OPTIONAL"


def _run_subsystem_initializers():
    """Invoke each subsystem's ``cob_init_<name>`` in the fixed canonical order.

    Performs a *deferred* import of each sibling module so that ``common`` has
    no import-time dependency on them (it is the base module they all import).

    Every module named in :data:`COB_INIT_ORDER` is a *required* runtime
    component (the AAP mandates reproducing the full C initialization sequence
    ``numeric -> strings -> move -> intrinsic -> fileio -> termio -> call``).  A
    missing module therefore means the runtime package is incomplete, and a
    generated program would otherwise run against a partially-initialised
    runtime; this is reported as a clear :class:`RuntimeError` rather than being
    silently skipped (resolves the review finding on silent ``ImportError``
    swallowing).  The only exception is when the test-only ``COB_PY_INIT_OPTIONAL``
    flag is set, in which case a genuinely-absent module is skipped.

    Crucially, an :class:`ImportError` raised *inside* a present module (i.e. a
    real defect in that module) is never swallowed -- presence is probed with
    :func:`importlib.util.find_spec` (which does not execute the module), so only
    a truly missing module is eligible for the test-mode skip; any error during
    the subsequent import always propagates.
    """
    import importlib
    import importlib.util

    optional = os.environ.get(_INIT_OPTIONAL_ENV) == "1"

    for name in COB_INIT_ORDER:
        qualified = "libcob_py." + name
        # Probe for the module file without executing it.  ``find_spec`` returns
        # ``None`` only when the module genuinely does not exist; a defect inside
        # a present module surfaces later at ``import_module`` and propagates.
        if importlib.util.find_spec(qualified) is None:
            if optional:
                # Test-isolation mode: the absent sibling is intentionally
                # unavailable, so skip its initializer.
                continue
            # Production: an incomplete runtime is fatal -- refuse to run a
            # generated program against a partially-initialised runtime.
            raise RuntimeError(
                "libcob_py runtime initialization failed: required subsystem "
                "module '%s' is not available. The libcob_py runtime package is "
                "incomplete; reinstall or rebuild it. (Set %s=1 only for "
                "isolated unit testing.)" % (qualified, _INIT_OPTIONAL_ENV)
            )
        mod = importlib.import_module(qualified)
        init_fn = getattr(mod, "cob_init_" + name, None)
        if callable(init_fn):
            init_fn()


def cob_init(argc=0, argv=None):
    """Initialise the COBOL runtime (common.c L748-L810).

    Idempotent: guarded by :data:`cob_initialized`.  Captures the command line,
    runs the subsystem initializers in the canonical order, then reads the
    ``COB_SWITCH_1`` .. ``COB_SWITCH_8`` and ``COB_LINE_TRACE`` environment
    variables exactly as the C runtime does.
    """
    global cob_initialized, _cob_argc, _cob_argv, _cob_line_trace

    if cob_initialized:
        return

    if argv is None:
        argv = list(sys.argv)
    _cob_argc = argc if argc else len(argv)
    _cob_argv = list(argv)

    # Subsystem start-up in the canonical order (numeric -> strings -> move ->
    # intrinsic -> fileio -> termio -> call).
    _run_subsystem_initializers()

    # COBOL implementor switches: COB_SWITCH_n == "ON" (case-insensitive).
    for i in range(8):
        value = os.environ.get("COB_SWITCH_%d" % (i + 1))
        _cob_switch[i] = 1 if (value and value.upper() == "ON") else 0

    value = os.environ.get("COB_LINE_TRACE")
    if value and value[:1] in ("Y", "y"):
        _cob_line_trace = 1

    cob_initialized = 1


def cobinit():
    """Convenience entry point: ``cob_init(0, None)`` returning 0 (common.c L1725)."""
    cob_init(0, None)
    return 0


def cob_module_enter(module):
    """Push *module* onto the active-module chain (common.c L812-L822).

    If the runtime has not yet been initialised this emits the same warning as
    the C runtime and performs a lazy ``cob_init``.
    """
    global cob_current_module

    if not cob_initialized:
        sys.stderr.write(
            "Warning: cob_init expected in the main program\n")
        cob_init(0, None)

    module.next = cob_current_module
    cob_current_module = module


def cob_module_leave(module):
    """Pop the current module off the active chain (common.c L824-L828)."""
    global cob_current_module
    cob_current_module = cob_current_module.next


def _run_exit_handlers():
    """Invoke installed CBL_EXIT_PROC handlers (common.c L835-L841/L1763-L1769)."""
    for handler in list(_exit_handlers):
        try:
            handler()
        except Exception:  # pragma: no cover - defensive
            pass


def _shutdown_runtime():
    """Best-effort screen/fileio/call teardown shared by cob_stop_run / cobtidy.

    The C runtime calls ``cob_screen_terminate``, ``cob_exit_fileio`` and
    ``cob_exit_call`` here; those live in the screenio / fileio / call modules,
    so they are invoked via a deferred import and skipped when unavailable.

    REVIEW FIX (MAJOR #7): ``call.cob_exit_call`` was added to this teardown so
    the (intentionally unbounded) dynamic-loader caches are released at STOP RUN
    / tidy - without it the call cache and cancel handlers would outlive the run
    and a subsequent ``cob_init`` would not start from a clean table.
    """
    import importlib

    for mod_name, fn_name in (("screenio", "cob_screen_terminate"),
                              ("fileio", "cob_exit_fileio"),
                              ("call", "cob_exit_call")):
        try:
            mod = importlib.import_module("libcob_py." + mod_name)
        except ImportError:
            continue
        fn = getattr(mod, fn_name, None)
        if callable(fn):
            try:
                fn()
            except Exception:  # pragma: no cover - defensive
                pass


def cob_stop_run(status):
    """STOP RUN - run exit handlers, tear down, then exit (common.c L830-L845)."""
    _run_exit_handlers()
    _shutdown_runtime()
    sys.exit(int(status))


def cobexit(status):
    """Alias of :func:`cob_stop_run` (common.c L1752-L1756)."""
    cob_stop_run(status)


def cobtidy():
    """Run exit handlers and tear down without exiting (common.c L1758-L1773)."""
    _run_exit_handlers()
    _shutdown_runtime()
    return 0


# ===========================================================================
# Deferred cross-module helpers.
#
# ``common`` is the base module, so it must not import ``move`` / ``numeric``
# at import time (they import ``common``).  These thin wrappers perform the
# import lazily on first use and raise a clear error only if the routine is
# genuinely invoked before the providing module exists.
# ===========================================================================

def _lazy_move(src, dst):
    """Invoke ``libcob_py.move.cob_move(src, dst)`` via a deferred import."""
    from libcob_py import move  # deferred: breaks the common<->move cycle
    move.cob_move(src, dst)


def _lazy_get_int(f):
    """Return ``libcob_py.move.cob_get_int(f)`` via a deferred import."""
    from libcob_py import move  # deferred
    return move.cob_get_int(f)


def _lazy_set_int(f, n):
    """Invoke ``libcob_py.move.cob_set_int(f, n)`` via a deferred import."""
    from libcob_py import move  # deferred
    move.cob_set_int(f, n)


def _lazy_numeric_cmp(f1, f2):
    """Return ``libcob_py.numeric.cob_numeric_cmp(f1, f2)`` (deferred import)."""
    from libcob_py import numeric  # deferred
    return numeric.cob_numeric_cmp(f1, f2)


def _lazy_cmp_int(f, n):
    """Return ``libcob_py.numeric.cob_cmp_int(f, n)`` (deferred import)."""
    from libcob_py import numeric  # deferred
    return numeric.cob_cmp_int(f, n)


# ===========================================================================
# Sign handling (common.c L259-L488, L934-L1020)
#
# Two encodings are supported, exactly as in the C runtime:
#   * ASCII  overpunch - the sign is folded into the leading/trailing digit by
#                        adding 0x40 for a negative value ('0'<->'p' ...
#                        '9'<->'y'); GET subtracts 0x40.  This matches both the
#                        GET_SIGN_ASCII/PUT_SIGN_ASCII macros (common.h L444)
#                        and the table-based helpers compiled on EBCDIC hosts.
#   * EBCDIC overpunch - positive '0'->'{','1'->'A'..'9'->'I';
#                        negative '0'->'}','1'->'J'..'9'->'R'.
# ===========================================================================

# ASCII overpunch translation tables (negative digit <-> overpunch byte).
_ASCII_PUT = {ord("0") + d: ord("p") + d for d in range(10)}
_ASCII_GET = {v: k for k, v in _ASCII_PUT.items()}

# EBCDIC overpunch tables (common.c L299-L488).
_EBCDIC_POS = {ord("0"): ord("{")}
_EBCDIC_POS.update({ord("1") + d: ord("A") + d for d in range(9)})
_EBCDIC_NEG = {ord("0"): ord("}")}
_EBCDIC_NEG.update({ord("1") + d: ord("J") + d for d in range(9)})
_EBCDIC_GET = {}
for _d, _b in _EBCDIC_POS.items():
    _EBCDIC_GET[_b] = (_d, 1)
for _d, _b in _EBCDIC_NEG.items():
    _EBCDIC_GET[_b] = (_d, -1)
del _d, _b


def cob_get_sign_ascii(c):
    """Decode an ASCII negative overpunch byte to its digit (common.c L260-295).

    Given a byte value *c* ('p'..'y' for an overpunched negative digit) return
    the underlying digit byte ('0'..'9').  Non-overpunch bytes are returned
    unchanged, matching the table-based C helper.
    """
    return _ASCII_GET.get(c, c)


def cob_put_sign_ascii(c):
    """Encode a digit byte as an ASCII negative overpunch (common.c L372-407).

    Given a digit byte *c* ('0'..'9') return the negative-overpunch byte
    ('p'..'y').  Non-digit bytes are returned unchanged.
    """
    return _ASCII_PUT.get(c, c)


def cob_get_sign_ebcdic(c):
    """Decode an EBCDIC overpunch byte (common.c L298-369).

    Returns ``(digit_byte, sign)`` where *sign* is ``+1`` or ``-1``.  Unknown
    bytes decode to ``(ord('0'), 1)``, mirroring the C ``default`` arm.
    """
    return _EBCDIC_GET.get(c, (ord("0"), 1))


def cob_put_sign_ebcdic(c, sign):
    """Encode a digit byte as an EBCDIC overpunch (common.c L410-488).

    *c* is a digit byte; *sign* selects the positive ('{','A'..'I') or negative
    ('}','J'..'R') zone.  Non-digit input folds to '{' / '}' like the C default.
    """
    if sign < 0:
        return _EBCDIC_NEG.get(c, ord("}"))
    return _EBCDIC_POS.get(c, ord("{"))


def _module_display_sign():
    """Return the active module's display-sign mode (ASCII when no module)."""
    if cob_current_module is not None:
        return cob_current_module.display_sign
    return COB_DISPLAY_SIGN_ASCII


def cob_real_get_sign(f):
    """Extract (and normalise) the operational sign of *f* (common.c L934-976).

    For zoned DISPLAY items the embedded/overpunched sign is read and the data
    byte is normalised back to a plain digit in place; for PACKED-DECIMAL the
    low nibble of the last byte is inspected.  Returns ``+1`` / ``-1`` for
    signed numerics and ``0`` for non-numeric types.
    """
    ftype = COB_FIELD_TYPE(f)
    if ftype == COB_TYPE_NUMERIC_DISPLAY:
        idx = 0 if COB_FIELD_SIGN_LEADING(f) else f.size - 1
        if COB_FIELD_SIGN_SEPARATE(f):
            return 1 if f.data[idx] == ord("+") else -1
        c = f.data[idx]
        if ord("0") <= c <= ord("9"):
            return 1
        if c == ord(" "):
            f.data[idx] = ord("0")
            return 1
        if _module_display_sign():
            digit, sign = cob_get_sign_ebcdic(c)
            f.data[idx] = digit
            return sign
        # ASCII overpunch: subtract 0x40 (equivalent to the table lookup).
        f.data[idx] = cob_get_sign_ascii(c) if c in _ASCII_GET else (c - 0x40)
        return -1
    if ftype == COB_TYPE_NUMERIC_PACKED:
        c = f.data[f.size - 1]
        return -1 if (c & 0x0F) == 0x0D else 1
    return 0


def cob_real_put_sign(f, sign):
    """Store the operational *sign* into *f* in place (common.c L978-1020)."""
    ftype = COB_FIELD_TYPE(f)
    if ftype == COB_TYPE_NUMERIC_DISPLAY:
        idx = 0 if COB_FIELD_SIGN_LEADING(f) else f.size - 1
        if COB_FIELD_SIGN_SEPARATE(f):
            c = ord("-") if sign < 0 else ord("+")
            if f.data[idx] != c:
                f.data[idx] = c
        elif _module_display_sign():
            f.data[idx] = cob_put_sign_ebcdic(f.data[idx], sign)
        elif sign < 0:
            # ASCII overpunch: add 0x40 (equivalent to the table lookup).
            c = f.data[idx]
            f.data[idx] = cob_put_sign_ascii(c) if c in _ASCII_PUT else (c + 0x40)
        return
    if ftype == COB_TYPE_NUMERIC_PACKED:
        idx = f.size - 1
        if sign < 0:
            f.data[idx] = (f.data[idx] & 0xF0) | 0x0D
        else:
            f.data[idx] = (f.data[idx] & 0xF0) | 0x0C
        return


def cob_get_sign(f):
    """Return the sign of *f*, or 0 when it is unsigned (common.h L420 macro)."""
    return cob_real_get_sign(f) if COB_FIELD_HAVE_SIGN(f) else 0


def cob_put_sign(f, sign):
    """Store *sign* into *f* only when it is a signed item (common.h L421 macro)."""
    if COB_FIELD_HAVE_SIGN(f):
        cob_real_put_sign(f, sign)


def cob_field_to_string(f, buf=None):
    """Convert field bytes to a trailing-space-trimmed ``str`` (common.c L1022).

    Used by ``call.py`` to resolve program names.  Returns the decoded string;
    when *buf* is a ``bytearray`` it is also filled with the NUL-terminated
    result for callers emulating the C out-parameter form.
    """
    raw = bytes(f.data[:f.size]) if f.data is not None else b""
    # Strip trailing spaces and NULs (common.c L1028-1033).
    end = len(raw)
    while end > 0 and raw[end - 1] in (0x20, 0x00):
        end -= 1
    text = raw[:end].decode("latin-1")
    if buf is not None:
        trimmed = raw[:end] + b"\x00"
        del buf[:]
        buf.extend(trimmed)
    return text


def cob_memcpy(dst, src, size):
    """Move *size* alphanumeric bytes from *src* into field *dst* (common.c L708).

    Builds a temporary ALPHANUMERIC source field and delegates to ``cob_move``
    so that all receiving-field editing/justification rules apply.  *src* may
    be ``bytes``/``bytearray``/``str``.
    """
    temp_attr = cob_field_attr(COB_TYPE_ALPHANUMERIC, 0, 0, 0, None)
    temp = cob_field(size, src, temp_attr)
    _lazy_move(temp, dst)


# ===========================================================================
# Comparison (common.c L490-L616, L1056-L1107)
# ===========================================================================

def _collating():
    """Return the active module's collating sequence, or ``None``."""
    if cob_current_module is not None:
        return cob_current_module.collating_sequence
    return None


def _common_cmpc(s1, off, c, size):
    """Compare *size* bytes of *s1* (from *off*) against the repeated byte *c*.

    Mirrors the static ``common_cmpc`` (common.c L490-512); honours the current
    module's collating sequence when one is installed.
    """
    s = _collating()
    if s:
        sc = s[c]
        for i in range(size):
            ret = s[s1[off + i]] - sc
            if ret != 0:
                return ret
    else:
        for i in range(size):
            ret = s1[off + i] - c
            if ret != 0:
                return ret
    return 0


def _common_cmps(s1, off1, s2, off2, size, col):
    """Compare *size* bytes of *s1*/*s2* under collating table *col*.

    Mirrors the static ``common_cmps`` (common.c L514-535).
    """
    if col:
        for i in range(size):
            ret = col[s1[off1 + i]] - col[s2[off2 + i]]
            if ret != 0:
                return ret
    else:
        for i in range(size):
            ret = s1[off1 + i] - s2[off2 + i]
            if ret != 0:
                return ret
    return 0


def cob_cmp_char(f, c):
    """Compare numeric/alphanumeric field *f* against the single byte *c*.

    Mirrors the static ``cob_cmp_char`` (common.c L537-549): reads (and then
    restores) the field's sign around the byte comparison except for PACKED.
    """
    sign = cob_get_sign(f)
    ret = _common_cmpc(f.data, 0, c, f.size)
    if COB_FIELD_TYPE(f) != COB_TYPE_NUMERIC_PACKED:
        cob_put_sign(f, sign)
    return ret


def cob_cmp_all(f1, f2):
    """Compare *f1* against an ALL-literal *f2* (common.c L551-581)."""
    size = f1.size
    sign = cob_get_sign(f1)
    ret = 0
    s = _collating()
    data = f1.data
    offset = 0
    f2size = f2.size
    f2data = f2.data
    while size >= f2size:
        ret = _common_cmps(data, offset, f2data, 0, f2size, s)
        if ret != 0:
            break
        size -= f2size
        offset += f2size
    if ret == 0 and size > 0:
        ret = _common_cmps(data, offset, f2data, 0, size, s)
    if COB_FIELD_TYPE(f1) != COB_TYPE_NUMERIC_PACKED:
        cob_put_sign(f1, sign)
    return ret


def cob_cmp_alnum(f1, f2):
    """Alphanumeric comparison of *f1* and *f2* (common.c L583-616).

    Compares the common prefix, then pads the shorter operand with spaces -
    the standard COBOL alphanumeric comparison rule.
    """
    sign1 = cob_get_sign(f1)
    sign2 = cob_get_sign(f2)
    minlen = f1.size if f1.size < f2.size else f2.size
    s = _collating()
    ret = _common_cmps(f1.data, 0, f2.data, 0, minlen, s)
    if ret == 0:
        if f1.size > f2.size:
            ret = _common_cmpc(f1.data, minlen, ord(" "), f1.size - minlen)
        elif f1.size < f2.size:
            ret = -_common_cmpc(f2.data, minlen, ord(" "), f2.size - minlen)
    if COB_FIELD_TYPE(f1) != COB_TYPE_NUMERIC_PACKED:
        cob_put_sign(f1, sign1)
    if COB_FIELD_TYPE(f2) != COB_TYPE_NUMERIC_PACKED:
        cob_put_sign(f2, sign2)
    return ret


def cob_cmp(f1, f2):
    """General COBOL comparison of two fields (common.c L1056-1107).

    Numeric/numeric comparisons defer to the numeric subsystem; ALL-literals
    and figurative ZERO are special-cased; non-DISPLAY numerics are converted
    to zoned DISPLAY (via ``cob_move``) before an alphanumeric comparison, all
    exactly as in the C runtime.
    """
    if COB_FIELD_IS_NUMERIC(f1) and COB_FIELD_IS_NUMERIC(f2):
        return _lazy_numeric_cmp(f1, f2)

    if COB_FIELD_TYPE(f2) == COB_TYPE_ALPHANUMERIC_ALL:
        if f2 is cob_zero and COB_FIELD_IS_NUMERIC(f1):
            return _lazy_cmp_int(f1, 0)
        elif f2.size == 1:
            return cob_cmp_char(f1, f2.data[0])
        else:
            return cob_cmp_all(f1, f2)
    elif COB_FIELD_TYPE(f1) == COB_TYPE_ALPHANUMERIC_ALL:
        if f1 is cob_zero and COB_FIELD_IS_NUMERIC(f2):
            return -_lazy_cmp_int(f2, 0)
        elif f1.size == 1:
            return -cob_cmp_char(f2, f1.data[0])
        else:
            return -cob_cmp_all(f2, f1)
    else:
        if (COB_FIELD_IS_NUMERIC(f1)
                and COB_FIELD_TYPE(f1) != COB_TYPE_NUMERIC_DISPLAY):
            f1 = _to_display(f1)
        if (COB_FIELD_IS_NUMERIC(f2)
                and COB_FIELD_TYPE(f2) != COB_TYPE_NUMERIC_DISPLAY):
            f2 = _to_display(f2)
        return cob_cmp_alnum(f1, f2)


def _to_display(f):
    """Return a zoned-DISPLAY copy of numeric field *f* (common.c L1083-1104)."""
    digits = COB_FIELD_DIGITS(f)
    attr = cob_field_attr(COB_TYPE_NUMERIC_DISPLAY, f.attr.digits, f.attr.scale,
                          f.attr.flags & ~COB_FLAG_HAVE_SIGN, f.attr.pic)
    temp = cob_field(digits, bytearray(digits), attr)
    _lazy_move(f, temp)
    return temp


# ===========================================================================
# Class checks (common.c L1109-L1212)
# ===========================================================================

def _isdigit(b):
    """ASCII digit test (the C locale is "C", so only 0-9)."""
    return 0x30 <= b <= 0x39


def _isspace(b):
    """ASCII whitespace test matching C ``isspace`` in the C locale."""
    return b in (0x20, 0x09, 0x0A, 0x0B, 0x0C, 0x0D)


def _isalpha(b):
    """ASCII alphabetic test (A-Z, a-z)."""
    return 0x41 <= b <= 0x5A or 0x61 <= b <= 0x7A


def _isupper(b):
    """ASCII uppercase test (A-Z)."""
    return 0x41 <= b <= 0x5A


def _islower(b):
    """ASCII lowercase test (a-z)."""
    return 0x61 <= b <= 0x7A


def cob_is_omitted(f):
    """CLASS test: true when *f* models an OMITTED argument (common.c L1113)."""
    return f.data is None


def cob_is_numeric(f):
    """NUMERIC class test (common.c L1119-1173).

    Validates per-USAGE: BINARY/FLOAT/DOUBLE are always numeric; PACKED checks
    every BCD nibble plus the sign nibble; zoned DISPLAY validates each digit
    around a sign read/restore; everything else checks each byte is a digit.
    """
    ftype = COB_FIELD_TYPE(f)
    if ftype in (COB_TYPE_NUMERIC_BINARY, COB_TYPE_NUMERIC_FLOAT,
                 COB_TYPE_NUMERIC_DOUBLE):
        return 1
    if ftype == COB_TYPE_NUMERIC_PACKED:
        last = f.size - 1
        for i in range(last):
            if (f.data[i] & 0xF0) > 0x90 or (f.data[i] & 0x0F) > 0x09:
                return 0
        if (f.data[last] & 0xF0) > 0x90:
            return 0
        sign = f.data[last] & 0x0F
        if sign == 0x0F:
            return 1
        if COB_FIELD_HAVE_SIGN(f) and sign in (0x0C, 0x0D):
            return 1
        return 0
    if ftype == COB_TYPE_NUMERIC_DISPLAY:
        data = COB_FIELD_DATA(f)
        size = COB_FIELD_SIZE(f)
        sign = cob_get_sign(f)
        for i in range(size):
            if not _isdigit(data[i]):
                cob_put_sign(f, sign)
                return 0
        cob_put_sign(f, sign)
        return 1
    for i in range(f.size):
        if not _isdigit(f.data[i]):
            return 0
    return 1


def cob_is_alpha(f):
    """ALPHABETIC class test - each byte is a space or a letter (common.c L1175)."""
    for i in range(f.size):
        if not _isspace(f.data[i]) and not _isalpha(f.data[i]):
            return 0
    return 1


def cob_is_upper(f):
    """ALPHABETIC-UPPER class test (common.c L1188)."""
    for i in range(f.size):
        if not _isspace(f.data[i]) and not _isupper(f.data[i]):
            return 0
    return 1


def cob_is_lower(f):
    """ALPHABETIC-LOWER class test (common.c L1201)."""
    for i in range(f.size):
        if not _isspace(f.data[i]) and not _islower(f.data[i]):
            return 0
    return 1


# ===========================================================================
# Switches (common.c L1040-L1054)
# ===========================================================================

def cob_get_switch(n):
    """Return the state (0/1) of implementor SWITCH *n* (0-based; common.c L1040)."""
    return _cob_switch[n]


def cob_set_switch(n, flag):
    """Set implementor SWITCH *n* to 0 or 1 (common.c L1046).  Other values are ignored."""
    if flag == 0:
        _cob_switch[n] = 0
    elif flag == 1:
        _cob_switch[n] = 1


# ===========================================================================
# Run-time bounds / validity checks (common.c L1250-L1322)
#
# Each check raises the documented exception via cob_set_exception and then
# reports a runtime error and stops, exactly as the C runtime does.
# ===========================================================================

def cob_check_based(x, name):
    """Verify a BASED/LINKAGE item address is non-NULL (common.c L1250-1257)."""
    if not x:
        cob_runtime_error("BASED/LINKAGE item '%s' has NULL address", name)
        cob_stop_run(1)


def cob_check_numeric(f, name):
    """Verify *f* holds a valid numeric value (common.c L1259-1282).

    On failure the offending content is rendered with octal escapes for
    non-printable bytes (matching the C diagnostic) and the run is stopped.
    """
    if not cob_is_numeric(f):
        parts = []
        for b in f.data[:f.size]:
            if 0x20 <= b < 0x7F:
                parts.append(chr(b))
            else:
                parts.append("\\%03o" % b)
        cob_runtime_error("'%s' not numeric: '%s'", name, "".join(parts))
        cob_stop_run(1)


def cob_check_odo(i, minimum, maximum, name):
    """Validate an OCCURS DEPENDING ON subscript (common.c L1284-1293 -> EC-BOUND-ODO)."""
    if i < minimum or maximum < i:
        cob_set_exception(COB_EC_BOUND_ODO)
        cob_runtime_error("OCCURS DEPENDING ON '%s' out of bounds: %d", name, i)
        cob_stop_run(1)


def cob_check_subscript(i, minimum, maximum, name):
    """Validate a table subscript (common.c L1295-1304 -> EC-BOUND-SUBSCRIPT)."""
    if i < minimum or maximum < i:
        cob_set_exception(COB_EC_BOUND_SUBSCRIPT)
        cob_runtime_error("Subscript of '%s' out of bounds: %d", name, i)
        cob_stop_run(1)


def cob_check_ref_mod(offset, length, size, name):
    """Validate a reference modification (common.c L1306-1322 -> EC-BOUND-REF-MOD)."""
    if offset < 1 or offset > size:
        cob_set_exception(COB_EC_BOUND_REF_MOD)
        cob_runtime_error("Offset of '%s' out of bounds: %d", name, offset)
        cob_stop_run(1)
    if length < 1 or offset + length - 1 > size:
        cob_set_exception(COB_EC_BOUND_REF_MOD)
        cob_runtime_error("Length of '%s' out of bounds: %d", name, length)
        cob_stop_run(1)


# ===========================================================================
# EXTERNAL items (common.c L1324-L1351)
# ===========================================================================

def cob_external_addr(exname, exlength):
    """Return the shared storage for EXTERNAL item *exname* (common.c L1324-1351).

    The first reference allocates a zero-filled ``bytearray`` of *exlength*
    bytes and latches :data:`cob_initial_external`; later references return the
    same buffer.  Requesting a larger size than the original is a fatal error,
    matching the C contract.
    """
    global cob_initial_external

    ext = _externals.get(exname)
    if ext is not None:
        if exlength > ext.esize:
            cob_runtime_error("EXTERNAL item '%s' has size > %d",
                              exname, exlength)
            cob_stop_run(1)
        cob_initial_external = 0
        return ext.ext_alloc

    ext = cob_external()
    ext.esize = exlength
    ext.ename = exname
    ext.ext_alloc = bytearray(exlength)
    _externals[exname] = ext
    cob_initial_external = 1
    return ext.ext_alloc


# ===========================================================================
# Table sort (common.c L618-L640, L1218-L1244)
# ===========================================================================

class _cob_file_key(object):
    """Minimal SORT key descriptor (mirror of struct cob_file_key fields used)."""

    __slots__ = ("field", "flag", "offset")

    def __init__(self, field, flag, offset):
        self.field = field
        self.flag = flag
        self.offset = offset


def cob_table_sort_init(nkeys, collating_sequence):
    """Begin a table SORT, declaring *nkeys* keys (common.c L1218-1228)."""
    global _sort_nkeys, _sort_keys, _sort_collate
    _sort_nkeys = 0
    _sort_keys = [None] * nkeys
    if collating_sequence is not None:
        _sort_collate = collating_sequence
    else:
        _sort_collate = _collating()


def cob_table_sort_init_key(flag, field, offset):
    """Register one SORT key (ASCENDING/DESCENDING) at *offset* (common.c L1230-1237)."""
    global _sort_nkeys
    _sort_keys[_sort_nkeys] = _cob_file_key(field, flag, offset)
    _sort_nkeys += 1


def _sort_compare(rec1, rec2):
    """Comparison callback over the registered keys (common.c L618-640)."""
    for i in range(_sort_nkeys):
        key = _sort_keys[i]
        base = key.field
        f1 = cob_field(base.size, rec1[key.offset:key.offset + base.size],
                       base.attr)
        f2 = cob_field(base.size, rec2[key.offset:key.offset + base.size],
                       base.attr)
        if COB_FIELD_IS_NUMERIC(f1):
            cmp = _lazy_numeric_cmp(f1, f2)
        else:
            cmp = _common_cmps(f1.data, 0, f2.data, 0, f1.size, _sort_collate)
        if cmp != 0:
            return cmp if key.flag == COB_ASCENDING else -cmp
    return 0


def cob_table_sort(f, n):
    """Sort *n* fixed-size records held in table field *f* (common.c L1239-1244).

    Records are ``f.size`` bytes each.  The sort is stable and in-place over
    ``f.data`` using the keys registered via :func:`cob_table_sort_init_key`.
    """
    global _sort_keys
    rec_size = f.size
    records = [bytes(f.data[i * rec_size:(i + 1) * rec_size]) for i in range(n)]
    import functools
    records.sort(key=functools.cmp_to_key(_sort_compare))
    for i, rec in enumerate(records):
        f.data[i * rec_size:(i + 1) * rec_size] = rec
    _sort_keys = []


# ===========================================================================
# Extended ACCEPT/DISPLAY - DATE/TIME (common.c L1353-L1448)
# ===========================================================================

def cob_accept_date(f):
    """ACCEPT ... FROM DATE -> YYMMDD (common.c L1355-1364)."""
    s = time.strftime("%y%m%d", time.localtime())
    cob_memcpy(f, s.encode("latin-1"), 6)


def cob_accept_date_yyyymmdd(f):
    """ACCEPT ... FROM DATE YYYYMMDD (common.c L1366-1375)."""
    s = time.strftime("%Y%m%d", time.localtime())
    cob_memcpy(f, s.encode("latin-1"), 8)


def cob_accept_day(f):
    """ACCEPT ... FROM DAY -> YYDDD (common.c L1377-1386)."""
    s = time.strftime("%y%j", time.localtime())
    cob_memcpy(f, s.encode("latin-1"), 5)


def cob_accept_day_yyyyddd(f):
    """ACCEPT ... FROM DAY YYYYDDD (common.c L1388-1397)."""
    s = time.strftime("%Y%j", time.localtime())
    cob_memcpy(f, s.encode("latin-1"), 7)


def cob_accept_day_of_week(f):
    """ACCEPT ... FROM DAY-OF-WEEK -> 1=Mon .. 7=Sun (common.c L1399-1414)."""
    wday = time.localtime().tm_wday  # Python: Mon==0 .. Sun==6
    # C uses tm_wday with Sun==0; it maps Sun->'7', else digit.  Python's Monday
    # origin already yields 1..6 for Mon..Sat via (wday+1) and 7 for Sunday.
    digit = "7" if wday == 6 else str(wday + 1)
    cob_memcpy(f, digit.encode("latin-1"), 1)


def cob_accept_time(f):
    """ACCEPT ... FROM TIME -> HHMMSShh (hundredths) (common.c L1416-1448)."""
    now = time.time()
    local = time.localtime(now)
    hundredths = int((now - int(now)) * 100)
    s = time.strftime("%H%M%S", local) + ("%02d" % hundredths)
    cob_memcpy(f, s.encode("latin-1"), 8)


# ===========================================================================
# Command line (common.c L1450-L1535)
# ===========================================================================

def cob_display_command_line(f):
    """DISPLAY ... UPON COMMAND-LINE - capture the buffer (common.c L1450-1459)."""
    global _commln
    _commln = bytearray(f.data[:f.size])


def cob_accept_command_line(f):
    """ACCEPT ... FROM COMMAND-LINE (common.c L1461-1487).

    Returns the previously DISPLAYed command line if present, otherwise the
    space-separated program arguments.
    """
    if _commln is not None and len(_commln) > 0:
        cob_memcpy(f, _commln, len(_commln))
        return
    buff = bytearray()
    for i in range(1, _cob_argc):
        arg = _cob_argv[i].encode("latin-1") if isinstance(_cob_argv[i], str) \
            else bytes(_cob_argv[i])
        if len(buff) + len(arg) >= COB_MEDIUM_BUFF:
            break
        buff.extend(arg)
        buff.append(ord(" "))
    cob_memcpy(f, buff, len(buff))


def cob_display_arg_number(f):
    """DISPLAY ... UPON ARGUMENT-NUMBER - set the current argument (common.c L1493-1510)."""
    global _current_arg
    n = _lazy_get_int(f)
    if n < 0 or n >= _cob_argc:
        cob_set_exception(COB_EC_IMP_DISPLAY)
        return
    _current_arg = n


def cob_accept_arg_number(f):
    """ACCEPT ... FROM ARGUMENT-NUMBER -> argc-1 (common.c L1512-1524)."""
    _lazy_set_int(f, _cob_argc - 1)


def cob_accept_arg_value(f):
    """ACCEPT ... FROM ARGUMENT-VALUE -> next argument (common.c L1526-1535)."""
    global _current_arg
    if _current_arg >= _cob_argc:
        cob_set_exception(COB_EC_IMP_ACCEPT)
        return
    arg = _cob_argv[_current_arg]
    raw = arg.encode("latin-1") if isinstance(arg, str) else bytes(arg)
    cob_memcpy(f, raw, len(raw))
    _current_arg += 1


# ===========================================================================
# Environment variables (common.c L1537-L1642)
# ===========================================================================

def cob_display_environment(f):
    """DISPLAY ... UPON ENVIRONMENT-NAME - latch the variable name (common.c L1541-1552)."""
    global _cob_local_env
    if f.size > COB_SMALL_MAX:
        cob_set_exception(COB_EC_IMP_DISPLAY)
        return
    _cob_local_env = cob_field_to_string(f)


def cob_display_env_value(f):
    """DISPLAY ... UPON ENVIRONMENT-VALUE - set the latched variable (common.c L1554-1578)."""
    if not _cob_local_env:
        cob_set_exception(COB_EC_IMP_DISPLAY)
        return
    value = cob_field_to_string(f)
    try:
        os.environ[_cob_local_env] = value
    except (ValueError, OSError):
        cob_set_exception(COB_EC_IMP_DISPLAY)


def cob_set_environment(f1, f2):
    """SET ENVIRONMENT name (f1) TO value (f2) (common.c L1580-1585)."""
    cob_display_environment(f1)
    cob_display_env_value(f2)


def cob_get_environment(envname, envval):
    """ACCEPT ... FROM ENVIRONMENT envname INTO envval (common.c L1587-1608)."""
    if envname.size < COB_SMALL_BUFF:
        name = cob_field_to_string(envname)
        value = os.environ.get(name)
        if value is None:
            cob_set_exception(COB_EC_IMP_ACCEPT)
            value = " "
        raw = value.encode("latin-1", "replace")
        cob_memcpy(envval, raw, len(raw))
    else:
        cob_set_exception(COB_EC_IMP_ACCEPT)
        cob_memcpy(envval, b" ", 1)


def cob_accept_environment(f):
    """ACCEPT ... FROM ENVIRONMENT (latched name) (common.c L1610-1623)."""
    value = None
    if _cob_local_env:
        value = os.environ.get(_cob_local_env)
    if value is None:
        cob_set_exception(COB_EC_IMP_ACCEPT)
        value = " "
    raw = value.encode("latin-1", "replace")
    cob_memcpy(f, raw, len(raw))


def cob_chain_setup(data, parm, size):
    """CHAINING parameter setup (common.c L1625-1642).

    Fills *data* (a ``bytearray``) with argument *parm* space-padded to *size*
    bytes and updates :data:`cob_call_params`.
    """
    global cob_call_params
    for i in range(size):
        data[i] = ord(" ")
    if parm <= _cob_argc - 1:
        arg = _cob_argv[parm]
        raw = arg.encode("latin-1") if isinstance(arg, str) else bytes(arg)
        copy_len = len(raw) if len(raw) <= size else size
        data[0:copy_len] = raw[:copy_len]
    cob_call_params = _cob_argc - 1


# ===========================================================================
# Pointers / source location / trace (common.c L659-L706)
# ===========================================================================

#: ``sizeof(void *)`` on the host - the storage width of a USAGE POINTER /
#: PROGRAM-POINTER item.  A pointer item stores its address value as exactly
#: this many native-byte-order bytes in ``field.data``, byte-for-byte as the C
#: runtime did with ``memcpy(&tmptr, f->data, sizeof(void *))`` (common.c
#: L690-706) and as the emitted ``cob_pointer_manip`` helper does.
_POINTER_SIZE = struct.calcsize("P")

#: Bit mask reducing an integer to the host pointer width (C pointer wraparound).
_POINTER_MASK = (1 << (_POINTER_SIZE * 8)) - 1


def _ptr_buffer(obj):
    """Return the raw byte buffer backing *obj* (a cob_field or a buffer view).

    The pointer accessors are emitted with either a :class:`cob_field` object
    (``output_param`` - the POINTER/PROGRAM-POINTER read and ``SET p`` paths) or
    a raw ``memoryview`` data slice (``output_data`` - the ``SET ADDRESS OF``
    path), so a single helper normalises both to the byte run that holds the
    address.  ``None`` (an OMITTED / NULL operand) yields ``None``.
    """
    if obj is None:
        return None
    return getattr(obj, "data", obj)   # cob_field -> .data ; buffer -> itself


def _read_ptr(buf):
    """Decode the ``sizeof(void *)`` address bytes at the start of *buf*."""
    if buf is None:
        return 0
    return int.from_bytes(bytes(buf[:_POINTER_SIZE]), sys.byteorder, signed=False)


def _write_ptr(buf, addr):
    """Encode integer *addr* into the first ``sizeof(void *)`` bytes of *buf*."""
    buf[:_POINTER_SIZE] = (int(addr) & _POINTER_MASK).to_bytes(
        _POINTER_SIZE, sys.byteorder, signed=False)


def _ptr_to_int(val):
    """Normalise a pointer *value* (None / int / buffer) to an integer address.

    ``None`` -> ``0`` (NULL).  An ``int`` is the address itself (a pointer copied
    from another pointer, already decoded by :func:`cob_get_pointer`).  A buffer
    (a ``memoryview``/``bytes``/``bytearray`` produced by ``ADDRESS OF x``) has
    no real machine address under the Python backend, so a stable synthetic
    address is derived from the object identity; it round-trips through pointer
    storage and pointer comparison consistently.
    """
    if val is None:
        return 0
    if isinstance(val, int):
        return val & _POINTER_MASK
    return id(val) & _POINTER_MASK


def cob_get_pointer(srcptr):
    """Return the integer address stored in pointer item *srcptr* (common.c L690-697).

    Mirrors the C ``memcpy(&tmptr, srcptr, sizeof(void *)); return tmptr;`` - the
    ``sizeof(void *)`` bytes of the item are decoded as a native-byte-order
    integer.  This is exactly the value ``output_integer`` needs when a POINTER
    item is read into an integer expression, and it round-trips with
    :func:`cob_set_pointer` and :func:`cob_pointer_manip` (all three share the
    one raw-address-bytes model).  Accepts either a :class:`cob_field` or a raw
    buffer; ``None`` yields ``0``.
    """
    return _read_ptr(_ptr_buffer(srcptr))


def cob_get_prog_pointer(srcptr):
    """Return the integer address stored in PROGRAM-POINTER item *srcptr*.

    Program pointers are stored identically to data pointers (common.c
    L699-706), so this defers to the same decode path.
    """
    return _read_ptr(_ptr_buffer(srcptr))


def cob_set_pointer(fld, val):
    """Store pointer *val* into POINTER item *fld* (the ``SET pointer`` store path).

    The emitter (codegen.c CB_TAG_ASSIGN) lowers ``SET p TO ...`` to
    ``common.cob_set_pointer(p, <value>)`` so the storage that
    :func:`cob_get_pointer` reads is written with the same value, byte-for-byte.
    *val* may be ``None``/``0`` (NULL), an integer address (``SET p1 TO p2``) or a
    data buffer (``SET p TO ADDRESS OF x``); :func:`_ptr_to_int` normalises all
    three.  Returns *fld* so the call may also be used as an expression.
    """
    _write_ptr(fld.data, _ptr_to_int(val))
    return fld


def cob_set_prog_pointer(fld, val):
    """Store program-pointer *val* into PROGRAM-POINTER item *fld*.

    Identical storage to :func:`cob_set_pointer` (common.c L699-706).
    """
    _write_ptr(fld.data, _ptr_to_int(val))
    return fld


def cob_set_addr(data, val):
    """``SET ADDRESS OF x TO val`` - write address *val* into buffer *data*.

    The emitter (codegen.c CB_TAG_ASSIGN, CAST_ADDRESS var) passes ``data`` =
    ``output_data(x)`` (a writable ``memoryview`` over x's storage) and ``val`` =
    the new address expression.  This mirrors the C non-aligned store
    ``memcpy(<data>, &temp_ptr, sizeof(void *))``: the ``sizeof(void *)`` address
    bytes are written into the start of *data*, byte-for-byte, so a subsequent
    read decodes the same value.
    """
    _write_ptr(data, _ptr_to_int(val))


def cob_addr_of(data):
    """Model C ``&data`` (ADDRESS-OF-ADDRESS / pointer-to-pointer).

    The emitter wraps a data view in ``common.cob_addr_of(...)`` when it needs a
    *pointer to* that storage (codegen.c CB_CAST_ADDR_OF_ADDR, and the
    COB_NON_ALIGNED RETURNING-pointer ``memcpy`` source).  Python has no machine
    address, so a fresh writable ``sizeof(void *)``-byte buffer is returned
    holding a stable synthetic address (the object identity of *data*); it is a
    valid ``memcpy`` source and an updatable pointer-slot argument.  ``None``
    yields a zero (NULL) slot.
    """
    return bytearray(
        _ptr_to_int(data).to_bytes(_POINTER_SIZE, sys.byteorder, signed=False))


def cob_field_set_data(field, data):
    """Re-point *field*'s storage at *data* and return *field* (BASED/LINKAGE).

    Mirrors the C comma-expression ``(f->data = data, &f)`` (codegen.c L1625):
    a LOCAL / BASED / LINKAGE / ANY-LENGTH item is a cached field whose backing
    ``.data`` must be re-pointed at run time.  The view is **aliased** (never
    copied) so the item shares storage with the pointed-to data exactly as the C
    pointer assignment did; the field object is returned so it can be passed
    straight on as a call argument.
    """
    field.data = data
    return field


def cob_trunc_div(a, b):
    """C-style integer division: truncate the quotient toward zero.

    The emitter routes the integer ``'/'`` operator to this helper (codegen.c
    output_integer CB_TAG_BINARY_OP) because C integer division truncates toward
    zero whereas Python ``//`` floors toward negative infinity, so the two
    DISAGREE whenever the operands have opposite signs (C: ``-7 / 2 == -3`` but
    Python: ``-7 // 2 == -4``).  Computing the magnitude quotient and reapplying
    the sign reproduces C ``a / b`` exactly for all signs and magnitudes at
    arbitrary precision (no float), preserving byte-for-byte parity.  Division by
    zero propagates a :class:`ZeroDivisionError` (the C undefined-behaviour fault
    is surfaced rather than masked).
    """
    a = int(a)
    b = int(b)
    q = abs(a) // abs(b)
    if (a < 0) != (b < 0):
        q = -q
    return q


def cob_pointer_manip(f1, f2, addsub):
    """Add or subtract an integer offset to/from a USAGE POINTER item.

    Runtime home of the helper that the C backend used to *emit* as a static
    function inside every generated translation unit::

        static void
        cob_pointer_manip (cob_field *f1, cob_field *f2, size_t addsub)
        {
            unsigned char *tmptr;
            memcpy (&tmptr, f1->data, sizeof(void *));
            if (addsub) {
                tmptr -= cob_get_int (f2);
            } else {
                tmptr += cob_get_int (f2);
            }
            memcpy (f1->data, &tmptr, sizeof(void *));
        }

    (the original ``cobc/codegen.c`` ``gen_ptrmanip`` emission).  The front-end
    (``cobc/typeck.c`` L2660/L2702) emits ``common.cob_pointer_manip(f1, f2,
    flag)`` for ``SET pointer UP/DOWN BY n``; *addsub* is ``0`` (the ``cb_int0``
    UP / increment case) or ``1`` (the ``cb_int1`` DOWN / decrement case).

    The pointer value is read from *f1*'s data as a ``sizeof(void *)`` native
    -byte-order integer, adjusted by ``cob_get_int`` of *f2*, and written back -
    byte-for-byte equivalent to the two ``memcpy`` calls above (the write masks
    to the pointer width, mirroring C ``unsigned char *`` wraparound).
    """
    cur = _read_ptr(f1.data)
    amount = _lazy_get_int(f2)
    if addsub:
        cur -= amount
    else:
        cur += amount
    _write_ptr(f1.data, cur)


# ===========================================================================
# C standard-library buffer primitives (memcpy / memmove / memcmp / memset).
#
# The COBOL front-end (cobc/typeck.c) lowers small fixed-size MOVE / comparison
# operations directly to the C library functions ``memcpy`` / ``memcmp`` /
# ``memset`` (e.g. typeck.c L2471, L2505, L4510, L4524, ...), emitting them via
# ``cb_build_funcall_3`` with ``cob_build_cast_address`` / ``cob_build_cast_
# length`` operands.  The Python emitter routes those bare names to this module
# (``codegen_pymod`` rule for ``mem*`` -> ``common``), so generated code calls
# ``common.memcpy(...)`` etc.  Under the Python backend a "cast address" is a
# ``memoryview`` slice over the program's backing ``bytearray`` (or a ``bytes``
# literal for constant source data), and a "cast length" is a plain ``int``.
# These are therefore faithful, byte-exact re-implementations of the three C
# primitives operating on those buffers - NOT the field-aware ``cob_memcpy``
# (which builds a temp field and delegates to ``cob_move``); the two coexist
# because the emitter chooses the raw primitive only when both operands are
# fixed-size raw byte runs.
# ===========================================================================

def _readable_bytes(buf, n):
    """Return the first *n* bytes of *buf* as an immutable ``bytes`` snapshot.

    Accepts ``memoryview`` slices, ``bytes``/``bytearray`` and any other
    buffer-protocol object that the emitter may hand in for a cast-address
    operand.  Taking a snapshot also makes :func:`memcpy`/:func:`memmove`
    overlap-safe (the source is fully read before the destination is written).
    """
    mv = buf if isinstance(buf, memoryview) else memoryview(buf)
    return bytes(mv[:n])


def memcpy(dst, src, length):
    """C ``memcpy``: copy *length* bytes from *src* into *dst*; return *dst*.

    *dst* is a writable ``memoryview`` (over the program's ``bytearray``) or a
    ``bytearray``; *src* may be a ``memoryview``, ``bytes`` or ``bytearray``;
    *length* is an ``int``.  Semantics match the C library function for the
    non-overlapping case the compiler guarantees here.
    """
    n = int(length)
    if n > 0:
        dst[:n] = _readable_bytes(src, n)
    return dst


def memmove(dst, src, length):
    """C ``memmove``: like :func:`memcpy` but explicitly overlap-safe.

    The COBOL backend never actually emits ``memmove`` (only ``memcpy`` /
    ``memcmp`` / ``memset`` appear in ``typeck.c``), but ``codegen_pymod``
    routes the name to this module, so it is provided for completeness.  Because
    :func:`_readable_bytes` snapshots the source before writing, overlapping
    regions are handled correctly.
    """
    n = int(length)
    if n > 0:
        dst[:n] = _readable_bytes(src, n)
    return dst


def memset(dst, c, length):
    """C ``memset``: set *length* bytes of *dst* to byte value *c*; return *dst*.

    *dst* is a writable ``memoryview``/``bytearray``; *c* is an ``int`` byte
    value (used modulo 256, as in C); *length* is an ``int``.
    """
    n = int(length)
    if n > 0:
        dst[:n] = bytes((int(c) & 0xFF,)) * n
    return dst


def memcmp(a, b, size):
    """C ``memcmp``: compare the first *size* bytes of *a* and *b*.

    Returns a negative value, ``0``, or a positive value when *a* is
    respectively less than, equal to, or greater than *b* over the compared
    region - matching the C library contract used by the emitted comparison
    code.  Operands may be ``memoryview``/``bytes``/``bytearray``.
    """
    n = int(size)
    if n <= 0:
        return 0
    ba = _readable_bytes(a, n)
    bb = _readable_bytes(b, n)
    for i in range(n):
        diff = ba[i] - bb[i]
        if diff:
            return -1 if diff < 0 else 1
    return 0


def cob_set_location(progid, sfile, sline, csect, cpara, cstatement):
    """Record the current execution location for diagnostics (common.c L659-676)."""
    global cob_current_program_id, cob_source_file, cob_source_line
    global cob_current_section, cob_current_paragraph, cob_source_statement
    cob_current_program_id = progid
    cob_source_file = sfile
    cob_source_line = sline
    cob_current_section = csect
    cob_current_paragraph = cpara
    if cstatement:
        cob_source_statement = cstatement
    if _cob_line_trace:
        sys.stderr.write(
            "PROGRAM-ID: %s \tLine: %d \tStatement: %s\n"
            % (progid, sline, cstatement if cstatement else "Unknown"))
        sys.stderr.flush()


def cob_ready_trace():
    """READY TRACE - enable line tracing (common.c L678-682)."""
    global _cob_line_trace
    _cob_line_trace = 1


def cob_reset_trace():
    """RESET TRACE - disable line tracing (common.c L684-688)."""
    global _cob_line_trace
    _cob_line_trace = 0


# ===========================================================================
# C-level environment / command-line entry points (common.c L1707-L1750)
# ===========================================================================

def cobgetenv(name):
    """Return ``os.environ[name]`` or ``None`` (common.c L1707-1714)."""
    if name:
        return os.environ.get(name)
    return None


def cobputenv(s):
    """Set an environment variable from a ``NAME=VALUE`` string (common.c L1716-1723)."""
    if s and "=" in s:
        key, _, value = s.partition("=")
        os.environ[key] = value
        return 0
    return -1


def cobcommandline(flags, pargc, pargv, penvp, pname):
    """Re-seat the runtime's argc/argv (common.c L1732-1750).

    *pargc* / *pargv* are one-element out/in lists.  Returns ``None`` like the
    C routine (its return value is unspecified).
    """
    global _cob_argc, _cob_argv
    if not cob_initialized:
        cob_runtime_error(
            "'cobcommandline' - Runtime has not been initialized")
        cob_stop_run(1)
    if pargc is not None and pargv is not None:
        _cob_argc = pargc[0]
        _cob_argv = list(pargv[0])
    return None


# ===========================================================================
# System-routine helpers physically located in common.c (referenced by
# system.py).  C$GETPID / C$NARG / C$PARAMSIZE / C$SLEEP / C$JUSTIFY.
# (common.c L2167-L2292)
# ===========================================================================

def cob_acuw_getpid():
    """C$GETPID - return the current process id (common.c L2167-2171)."""
    return int(os.getpid())


def cob_return_args(data):
    """C$NARG - store the saved CALL parameter count (common.c L2173-2182)."""
    if cob_current_module is not None and cob_current_module.cob_procedure_parameters:
        param = cob_current_module.cob_procedure_parameters[0]
        if param is not None:
            _lazy_set_int(param, cob_save_call_params)
    return 0


def cob_parameter_size(data):
    """C$PARAMSIZE - size of the n-th caller parameter (common.c L2184-2202)."""
    if cob_current_module is None or not cob_current_module.cob_procedure_parameters:
        return 0
    param = cob_current_module.cob_procedure_parameters[0]
    if param is None:
        return 0
    n = _lazy_get_int(param)
    if 0 < n <= cob_save_call_params:
        n -= 1
        caller = cob_current_module.next
        if (caller is not None and caller.cob_procedure_parameters
                and n < len(caller.cob_procedure_parameters)
                and caller.cob_procedure_parameters[n] is not None):
            return caller.cob_procedure_parameters[n].size
    return 0


def cob_acuw_sleep(data):
    """C$SLEEP - sleep for n seconds (0 < n < 1 week) (common.c L2204-2222)."""
    if cob_current_module is not None and cob_current_module.cob_procedure_parameters:
        param = cob_current_module.cob_procedure_parameters[0]
        if param is not None:
            n = _lazy_get_int(param)
            if 0 < n < 3600 * 24 * 7:
                time.sleep(n)
    return 0


def cob_acuw_justify(data, *args):
    """C$JUSTIFY - left/right/centre justify *data* in place (common.c L2224-2292).

    *data* is a ``bytearray``.  The optional first extra argument is the
    direction byte: ``L`` (left), ``C`` (centre) or right-justify by default.
    """
    datalen = len(data)
    if datalen < 2:
        return 0
    if data[0] != ord(" ") and data[datalen - 1] != ord(" "):
        return 0

    left = 0
    while left < datalen and data[left] == ord(" "):
        left += 1
    if left == datalen:
        return 0

    right = 0
    n = datalen - 1
    while n >= 0 and data[n] == ord(" "):
        right += 1
        n -= 1

    movelen = datalen - left - right
    shifting = 0
    if cob_call_params > 1 and args:
        direction = args[0]
        dch = direction[0] if isinstance(direction, (bytes, bytearray)) else direction
        if dch == ord("L"):
            shifting = 1
        elif dch == ord("C"):
            shifting = 2

    body = bytes(data[left:left + movelen])
    if shifting == 1:  # left-justify
        data[0:movelen] = body
        for i in range(movelen, datalen):
            data[i] = ord(" ")
    elif shifting == 2:  # centre
        centrelen = (left + right) // 2
        for i in range(datalen):
            data[i] = ord(" ")
        data[centrelen:centrelen + movelen] = body
    else:  # right-justify (default)
        for i in range(datalen):
            data[i] = ord(" ")
        data[left + right:left + right + movelen] = body
    return 0


# ===========================================================================
# Public API surface.
#
# Export every ``COB_*`` constant, ``COB_EC_*`` exception id, ``cob_*`` /
# ``cob*`` runtime entry point, the field-model classes and the figurative
# constants - i.e. exactly the names the generated Python and the sibling
# runtime modules reference - while excluding the standard-library module
# objects imported above and the private helpers.
# ===========================================================================
import types as _types  # noqa: E402  (intentional late import for introspection)

__all__ = sorted(
    name for name, _val in list(globals().items())
    if not name.startswith("_")
    and not isinstance(_val, _types.ModuleType)
    and name not in ("namedtuple", "annotations")
)

