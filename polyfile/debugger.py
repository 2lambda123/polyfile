from abc import ABC, abstractmethod
import atexit
from enum import Enum
from io import StringIO
from pathlib import Path
from pdb import Pdb
import readline
import sys
from typing import Any, Callable, ContextManager, Generic, Iterable, Iterator, List, Optional, Type, TypeVar, Union

from .polyfile import __copyright__, __license__, __version__, PARSERS, Match, Parser, ParserFunction, Submatch
from .logger import getStatusLogger
from .magic import (
    AbsoluteOffset, FailedTest, InvalidOffsetError, MagicMatcher, MagicTest, Offset, TestResult, TEST_TYPES
)
from .wildcards import Wildcard


log = getStatusLogger("polyfile")


HISTORY_PATH = Path.home() / ".polyfile_history"


class ANSIColor(Enum):
    BLACK = 30
    RED = 31
    GREEN = 32
    YELLOW = 33
    BLUE = 34
    MAGENTA = 35
    CYAN = 36
    WHITE = 37

    def to_code(self) -> str:
        return f"\u001b[{self.value}m"


B = TypeVar("B", bound="Breakpoint")
T = TypeVar("T")


BREAKPOINT_TYPES: List[Type["Breakpoint"]] = []


class Breakpoint(ABC):
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.__name__ not in ("FailedBreakpoint", "MatchedBreakpoint"):
            BREAKPOINT_TYPES.append(cls)

    @abstractmethod
    def should_break(
            self,
            test: MagicTest,
            data: bytes,
            absolute_offset: int,
            parent_match: Optional[TestResult],
            result: Optional[TestResult]
    ) -> bool:
        raise NotImplementedError()

    @classmethod
    @abstractmethod
    def parse(cls: Type[B], command: str) -> Optional[B]:
        raise NotImplementedError()

    @staticmethod
    def from_str(command: str) -> Optional["Breakpoint"]:
        if command.startswith("!"):
            return FailedBreakpoint.parse(command)
        elif command.startswith("="):
            return MatchedBreakpoint.parse(command)
        for b_type in BREAKPOINT_TYPES:
            parsed = b_type.parse(command)
            if parsed is not None:
                return parsed
        return None

    @classmethod
    @abstractmethod
    def print_usage(cls, debugger: "Debugger"):
        raise NotImplementedError()

    @abstractmethod
    def __str__(self):
        raise NotImplementedError()


class FailedBreakpoint(Breakpoint):
    def __init__(self, parent: Breakpoint):
        self.parent: Breakpoint = parent

    def should_break(
            self,
            test: MagicTest,
            data: bytes,
            absolute_offset: int,
            parent_match: Optional[TestResult],
            result: Optional[TestResult]
    ) -> bool:
        return (result is None or isinstance(result, FailedTest)) and self.parent.should_break(
            test, data, absolute_offset, parent_match, result
        )

    @classmethod
    def parse(cls: B, command: str) -> Optional[B]:
        if not command.startswith("!"):
            return None
        parent = Breakpoint.from_str(command[1:])
        if parent is not None:
            return FailedBreakpoint(parent)
        else:
            return None

    @classmethod
    def print_usage(cls, debugger: "Debugger"):
        pass

    def __str__(self):
        return f"[FAILED] {self.parent!s}"


class MatchedBreakpoint(Breakpoint):
    def __init__(self, parent: Breakpoint):
        self.parent: Breakpoint = parent

    def should_break(
            self,
            test: MagicTest,
            data: bytes,
            absolute_offset: int,
            parent_match: Optional[TestResult],
            result: Optional[TestResult]
    ) -> bool:
        return result is not None and not isinstance(result, FailedTest) and self.parent.should_break(
            test, data, absolute_offset, parent_match, result
        )

    @classmethod
    def parse(cls: B, command: str) -> Optional[B]:
        if not command.startswith("="):
            return None
        parent = Breakpoint.from_str(command[1:])
        if parent is not None:
            return MatchedBreakpoint(parent)
        else:
            return None

    @classmethod
    def print_usage(cls, debugger: "Debugger"):
        pass

    def __str__(self):
        return f"[MATCHED] {self.parent!s}"


class MimeBreakpoint(Breakpoint):
    def __init__(self, mimetype: str):
        self.mimetype: str = mimetype
        self.pattern: Wildcard = Wildcard.parse(mimetype)

    def should_break(
            self,
            test: MagicTest,
            data: bytes,
            absolute_offset: int,
            parent_match: Optional[TestResult],
            result: Optional[TestResult]
    ) -> bool:
        return self.pattern.is_contained_in(test.mimetypes())

    @classmethod
    def parse(cls: Type[B], command: str) -> Optional[B]:
        if command.lower().startswith("mime:"):
            return MimeBreakpoint(command[len("mime:"):])
        return None

    @classmethod
    def print_usage(cls, debugger: "Debugger"):
        debugger.write("b MIME:MIMETYPE", color=ANSIColor.MAGENTA)
        debugger.write(" to break when a test is capable of matching that mimetype.\nThe ")
        debugger.write("MIMETYPE", color=ANSIColor.MAGENTA)
        debugger.write(" can include the ")
        debugger.write("*", color=ANSIColor.MAGENTA)
        debugger.write(" and ")
        debugger.write("?", color=ANSIColor.MAGENTA)
        debugger.write(" wildcards.\nFor example:\n")
        debugger.write("    b MIME:application/pdf\n    b MIME:*pdf\n", color=ANSIColor.MAGENTA)

    def __str__(self):
        return f"Breakpoint: Matching for MIME {self.mimetype}"


class ExtensionBreakpoint(Breakpoint):
    def __init__(self, ext: str):
        self.ext: str = ext

    def should_break(
            self,
            test: MagicTest,
            data: bytes,
            absolute_offset: int,
            parent_match: Optional[TestResult],
            result: Optional[TestResult]
    ) -> bool:
        return self.ext in test.all_extensions()

    @classmethod
    def parse(cls: Type[B], command: str) -> Optional[B]:
        if command.lower().startswith("ext:"):
            return ExtensionBreakpoint(command[len("ext:"):])
        return None

    @classmethod
    def print_usage(cls, debugger: "Debugger") -> str:
        debugger.write("b EXT:EXTENSION", color=ANSIColor.MAGENTA)
        debugger.write(" to break when a test is capable of matching that extension.\nFor example:\n")
        debugger.write("    b EXT:pdf\n", color=ANSIColor.MAGENTA)

    def __str__(self):
        return f"Breakpoint: Matching for extension {self.ext}"


class FileBreakpoint(Breakpoint):
    def __init__(self, filename: str, line: int):
        self.filename: str = filename
        self.line: int = line

    def should_break(
            self,
            test: MagicTest,
            data: bytes,
            absolute_offset: int,
            parent_match: Optional[TestResult],
            result: TestResult
    ) -> bool:
        if test.source_info is None or test.source_info.line != self.line:
            return False
        if "/" in self.filename:
            # it is a file path
            return str(test.source_info.path) == self.filename
        else:
            # treat it like a filename
            return test.source_info.path.name == self.filename

    @classmethod
    def parse(cls: Type[B], command: str) -> Optional[B]:
        filename, *remainder = command.split(":")
        if not remainder:
            return None
        try:
            line = int("".join(remainder))
        except ValueError:
            return None
        if line <= 0:
            return None
        return FileBreakpoint(filename, line)

    @classmethod
    def print_usage(cls, debugger: "Debugger"):
        debugger.write("b FILENAME:LINE_NO", color=ANSIColor.MAGENTA)
        debugger.write(" to break when the line of the given magic file is reached.\nFor example:\n")
        debugger.write("    b archive:525\n", color=ANSIColor.MAGENTA)
        debugger.write("will break on the test at line 525 of the archive DSL file.\n")

    def __str__(self):
        return f"Breakpoint: {self.filename} line {self.line}"


class InstrumentedTest:
    def __init__(self, test: Type[MagicTest], debugger: "Debugger"):
        self.test: Type[MagicTest] = test
        self.debugger: Debugger = debugger
        if "test" in test.__dict__:
            self.original_test: Optional[Callable[[...], Optional[TestResult]]] = test.test

            def wrapper(test_instance, *args, **kwargs) -> Optional[TestResult]:
                # if self.original_test is None:
                #     # this is a NOOP
                #     return self.test.test(test_instance, *args, **kwargs)
                return self.debugger.debug(self, test_instance, *args, **kwargs)

            test.test = wrapper
        else:
            self.original_test = None

    @property
    def enabled(self) -> bool:
        return self.original_test is not None

    def uninstrument(self):
        if self.original_test is not None:
            # we are still assigned to the test function, so reset it
            self.test.test = self.original_test
        self.original_test = None


class InstrumentedParser:
    def __init__(self, parser: Parser, debugger: "Debugger"):
        self.parser: Type[Parser] = parser
        self.debugger: Debugger = debugger
        self.original_parser: Optional[ParserFunction] = parser.parse

        def wrapper(parser_instance, *args, **kwargs) -> Iterator[Submatch]:
            yield from self.debugger.debug_parse(self, parser_instance, *args, **kwargs)

        parser.parse = wrapper

    @property
    def enalbed(self) -> bool:
        return self.original_parser is not None

    def uninstrument(self):
        if self.original_parser is not None:
            self.parser.parse = self.original_parser
        self.original_parser = None


def string_escape(data: Union[bytes, int]) -> str:
    if not isinstance(data, int):
        return "".join(string_escape(d) for d in data)
    elif data == ord('\n'):
        return "\\n"
    elif data == ord('\t'):
        return "\\t"
    elif data == ord('\r'):
        return "\\r"
    elif data == 0:
        return "\\0"
    elif data == ord('\\'):
        return "\\\\"
    elif 32 <= data <= 126:
        return chr(data)
    else:
        return f"\\x{data:02X}"


class StepMode(Enum):
    RUNNING = 0
    SINGLE_STEPPING = 1
    NEXT = 2


class Variable(Generic[T]):
    def __init__(self, possibilities: Iterable[T], value: T):
        self.possibilities: List[T] = list(possibilities)
        self._value: T = value
        self.value = value

    @property
    def value(self) -> T:
        return self._value

    @value.setter
    def value(self, new_value: T):
        if new_value not in self.possibilities:
            raise ValueError(f"invalid value {new_value!r}; must be one of {self.possibilities!r}")
        self._value = new_value

    def parse(self, value: str) -> T:
        value = value.strip().lower()
        for p in self.possibilities:
            if str(p).lower() == value:
                return p
        raise ValueError(f"Invalid value {value!r}; must be one of {', '.join(map(str, self.possibilities))}")

    def __str__(self):
        return str(self.value)

    def __repr__(self):
        return f"{self.__class__.__name__}()"


class BooleanVariable(Variable[bool]):
    def __init__(self, value: bool):
        super().__init__((True, False), value)

    def parse(self, value: str) -> T:
        try:
            return super().parse(value)
        except ValueError:
            pass
        value = value.strip().lower()
        if value == "0" or value == "f":
            return False
        return bool(value)

    def __bool__(self):
        return self.value


class BreakOnSubmatching(BooleanVariable):
    def __init__(self, value: bool, debugger: "Debugger"):
        self.debugger: Debugger = debugger
        super().__init__(value)

    @Variable.value.setter
    def value(self, new_value):
        Variable.value.fset(self, new_value)
        if self.debugger.enabled:
            # disable and re-enable the debugger to update the instrumentation
            self.debugger._uninstrument()
            self.debugger._instrument()


class ANSIWriter:
    def __init__(self, use_ansi: bool = True, escape_for_readline: bool = False):
        self.use_ansi: bool = use_ansi
        self.escape_for_readline: bool = escape_for_readline
        self.data = StringIO()

    @staticmethod
    def format(
            message: Any, bold: bool = False, dim: bool = False, color: Optional[ANSIColor] = None,
            escape_for_readline: bool = False
    ) -> str:
        prefixes: List[str] = []
        if bold and not dim:
            prefixes.append("\u001b[1m")
        elif dim and not bold:
            prefixes.append("\u001b[2m")
        if color is not None:
            prefixes.append(color.to_code())
        if prefixes:
            if escape_for_readline:
                message = f"\001{''.join(prefixes)}\002{message!s}\001\u001b[0m\002"
            else:
                message = f"{''.join(prefixes)}{message!s}\u001b[0m"
        else:
            message = str(message)
        return message

    def write(self, message: Any, bold: bool = False, dim: bool = False, color: Optional[ANSIColor] = None,
              escape_for_readline: Optional[bool] = None) -> str:
        if self.use_ansi:
            if escape_for_readline is None:
                escape_for_readline = self.escape_for_readline
            self.data.write(self.format(message=message, bold=bold, dim=dim, color=color,
                                        escape_for_readline=escape_for_readline))
        else:
            self.data.write(str(message))

    def __str__(self):
        return self.data.getvalue()


def _disable(debugger: "Debugger"):
    debugger.enabled = False


class Debugger(ContextManager["Debugger"]):
    def __init__(self, break_on_parsing: bool = True):
        self.instrumented_tests: List[InstrumentedTest] = []
        self.breakpoints: List[Breakpoint] = []
        self._entries: int = 0
        self.step_mode: StepMode = StepMode.RUNNING
        self.last_command: Optional[str] = None
        self.last_test: Optional[MagicTest] = None
        self.last_parent_match: Optional[MagicTest] = None
        self.data: bytes = b""
        self.last_offset: int = 0
        self.last_result: Optional[TestResult] = None
        self.repl_test: Optional[MagicTest] = None
        if sys.stderr.isatty():
            self.repl_prompt: str = ANSIWriter.format("(polyfile) ", bold=True, escape_for_readline=True)
        else:
            self.repl_prompt = "(polyfile) "
        self.instrumented_parsers: List[InstrumentedParser] = []
        self.break_on_submatching: BreakOnSubmatching = BreakOnSubmatching(break_on_parsing, self)
        self.variables_by_name: Dict[str, Variable] = {
            "break_on_parsing": self.break_on_submatching
        }
        self.variable_descriptions: Dict[str, str] = {
            "break_on_parsing": "Break when a PolyFile parser is about to be invoked and debug using PDB (default=True;"
                                " disable from the command line with `--no-debug-python`)"
        }
        self._pdb: Optional[Pdb] = None
        self._prev_history_length: int = 0
        atexit.register(_disable, self)

    def save_context(self):
        class DebugContext:
            def __init__(self, debugger: Debugger):
                self.debugger: Debugger = debugger
                self.__saved_state = {}

            def __enter__(self) -> Debugger:
                self.__saved_state = dict(self.debugger.__dict__)
                return self.debugger

            def __exit__(self, exc_type, exc_val, exc_tb):
                self.debugger.__dict__ = self.__saved_state

        return DebugContext(self)

    @property
    def enabled(self) -> bool:
        return any(t.enabled for t in self.instrumented_tests)

    def _uninstrument(self):
        # Uninstrument any existing instrumentation:
        for t in self.instrumented_tests:
            t.uninstrument()
        self.instrumented_tests = []
        for m in self.instrumented_parsers:
            m.uninstrument()
        self.instrumented_parsers = []

    def _instrument(self):
        # Instrument all of the MagicTest.test functions:
        for test in TEST_TYPES:
            if "test" in test.__dict__:
                # this class actually implements the test() function
                self.instrumented_tests.append(InstrumentedTest(test, self))
        if self.break_on_submatching:
            for parsers in PARSERS.values():
                for parser in parsers:
                    self.instrumented_parsers.append(InstrumentedParser(parser, self))

    @enabled.setter
    def enabled(self, is_enabled: bool):
        was_enabled = self.enabled
        self._uninstrument()
        if is_enabled:
            self._instrument()
            try:
                readline.read_history_file(HISTORY_PATH)
                self._prev_history_length = readline.get_current_history_length()
            except FileNotFoundError:
                open(HISTORY_PATH, 'wb').close()
                self._prev_history_length = 0
            # default history len is -1 (infinite), which may grow unruly
            readline.set_history_length(2048)
            self.write(f"PolyFile {__version__}\n", color=ANSIColor.MAGENTA, bold=True)
            self.write(f"{__copyright__}\n{__license__}\n\nFor help, type \"help\".\n")
            self.repl()
        elif was_enabled:
            # we are now disabled, so store our history
            new_length = readline.get_current_history_length()
            try:
                readline.append_history_file(max(new_length - self._prev_history_length, 0), HISTORY_PATH)
                self._prev_history_length = readline.get_current_history_length()
            except IOError as e:
                log.warning(f"Unable to save history to {HISTORY_PATH!s}: {e!s}")

    def __enter__(self) -> "Debugger":
        self._entries += 1
        if self._entries == 1:
            self.enabled = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._entries -= 1
        if self._entries == 0:
            self.enabled = False

    def should_break(self) -> bool:
        return self.step_mode == StepMode.SINGLE_STEPPING or (
            self.step_mode == StepMode.NEXT and self.last_result
        ) or any(
            b.should_break(self.last_test, self.data, self.last_offset, self.last_parent_match, self.last_result)
            for b in self.breakpoints
        )

    def write_test(self, test: MagicTest, is_current_test: bool = False):
        for comment in test.comments:
            if comment.source_info is not None and comment.source_info.original_line is not None:
                self.write(f"  {comment.source_info.path.name}", dim=True, color=ANSIColor.CYAN)
                self.write(":", dim=True)
                self.write(f"{comment.source_info.line}\t", dim=True, color=ANSIColor.CYAN)
                self.write(comment.source_info.original_line.strip(), dim=True)
                self.write("\n")
            else:
                self.write(f"  # {comment!s}\n", dim=True)
        if is_current_test:
            self.write("→ ", bold=True)
        else:
            self.write("  ")
        if test.source_info is not None and test.source_info.original_line is not None:
            source_prefix = f"{test.source_info.path.name}:{test.source_info.line}"
            indent = f"{' ' * len(source_prefix)}\t"
            self.write(test.source_info.path.name, dim=True, color=ANSIColor.CYAN)
            self.write(":", dim=True)
            self.write(test.source_info.line, dim=True, color=ANSIColor.CYAN)
            self.write("\t")
            self.write(test.source_info.original_line.strip(), color=ANSIColor.BLUE, bold=True)
        else:
            indent = ""
            self.write(f"{'>' * test.level}{test.offset!s}\t")
            self.write(test.message, color=ANSIColor.BLUE, bold=True)
        if test.mime is not None:
            self.write(f"\n  {indent}!:mime ", dim=True)
            self.write(test.mime, color=ANSIColor.BLUE)
        for e in test.extensions:
            self.write(f"\n  {indent}!:ext  ", dim=True)
            self.write(str(e), color=ANSIColor.BLUE)
        self.write("\n")

    def write(self, message: Any, bold: bool = False, dim: bool = False, color: Optional[ANSIColor] = None):
        if sys.stdout.isatty():
            if isinstance(message, MagicTest):
                self.write_test(message)
                return
            message = ANSIWriter.format(message=message, bold=bold, dim=dim, color=color)
        sys.stdout.write(str(message))

    def prompt(self, message: str, default: bool = True) -> bool:
        while True:
            buffer = ANSIWriter(use_ansi=sys.stderr.isatty(), escape_for_readline=True)
            buffer.write(f"{message} ", bold=True)
            buffer.write("[", dim=True)
            if default:
                buffer.write("Y", bold=True, color=ANSIColor.GREEN)
                buffer.write("n", dim=True, color=ANSIColor.RED)
            else:
                buffer.write("y", dim=True, color=ANSIColor.GREEN)
                buffer.write("N", bold=True, color=ANSIColor.RED)
            buffer.write("] ", dim=True)
            try:
                answer = input(str(buffer)).strip().lower()
            except EOFError:
                raise KeyboardInterrupt()
            if not answer:
                return default
            elif answer == "n":
                return False
            elif answer == "y":
                return True

    def print_context(self, data: bytes, offset: int, context_bytes: int = 32, num_bytes: int = 1):
        bytes_before = min(offset, context_bytes)
        context_before = string_escape(data[offset - bytes_before:offset])
        current_byte = string_escape(data[offset:offset+num_bytes])
        context_after = string_escape(data[offset + num_bytes:offset + num_bytes + context_bytes])
        self.write(context_before)
        self.write(current_byte, bold=True)
        self.write(context_after)
        self.write("\n")
        self.write(f"{' ' * len(context_before)}")
        self.write(f"{'^' * len(current_byte)}", bold=True)
        self.write(f"{' ' * len(context_after)}\n")

    def debug(
            self,
            instrumented_test: InstrumentedTest,
            test: MagicTest,
            data: bytes,
            absolute_offset: int,
            parent_match: Optional[TestResult]
    ) -> Optional[TestResult]:
        if instrumented_test.original_test is None:
            result = instrumented_test.test.test(test, data, absolute_offset, parent_match)
        else:
            result = instrumented_test.original_test(test, data, absolute_offset, parent_match)
        if self.repl_test is test:
            # this is a one-off test run from the REPL, so do not save its results
            return result
        self.last_result = result
        self.last_test = test
        self.data = data
        self.last_offset = absolute_offset
        self.last_parent_match = parent_match
        if self.should_break():
            self.repl()
        return self.last_result

    def print_where(
            self,
            test: Optional[MagicTest] = None,
            offset: Optional[int] = None,
            parent_match: Optional[TestResult] = None,
            result: Optional[TestResult] = None
    ):
        if test is None:
            test = self.last_test
        if test is None:
            self.write("The first test has not yet been run.\n", color=ANSIColor.RED)
            self.write("Use `step`, `next`, or `run` to start testing.\n")
            return
        if offset is None:
            offset = self.last_offset
        if parent_match is None:
            parent_match = self.last_parent_match
        if result is None:
            result = self.last_result
        wrote_breakpoints = False
        for b in self.breakpoints:
            if b.should_break(test, self.data, offset, parent_match, result):
                self.write(b, color=ANSIColor.MAGENTA)
                self.write("\n")
                wrote_breakpoints = True
        if wrote_breakpoints:
            self.write("\n")
        test_stack = [test]
        while test_stack[-1].parent is not None:
            test_stack.append(test_stack[-1].parent)
        for i, t in enumerate(reversed(test_stack)):
            if i == len(test_stack) - 1:
                self.write_test(t, is_current_test=True)
            else:
                self.write_test(t)
        test_stack = list(reversed(test.children))
        descendants = []
        while test_stack:
            descendant = test_stack.pop()
            if descendant.can_match_mime:
                descendants.append(descendant)
                test_stack.extend(reversed(descendant.children))
        for t in descendants:
            self.write_test(t)
        self.write("\n")
        data_offset = offset
        if not isinstance(test.offset, AbsoluteOffset):
            try:
                data_offset = test.offset.to_absolute(self.data, parent_match)
                self.write(str(test.offset), color=ANSIColor.BLUE)
                self.write(" = byte offset ", dim=True)
                self.write(f"{data_offset!s}\n", bold=True)
            except InvalidOffsetError as e:
                self.write(f"{e!s}\n", color=ANSIColor.RED)
        if result is not None and hasattr(result, "length"):
            context_bytes = result.length
        else:
            context_bytes = 1
        self.print_context(self.data, data_offset, num_bytes=context_bytes)
        if result is not None:
            if not result:
                self.write("Test failed.\n", color=ANSIColor.RED)
                if isinstance(result, FailedTest):
                    self.write(result.message)
                    self.write("\n")
            else:
                self.write("Test succeeded.\n", color=ANSIColor.GREEN)

    def print_match(self, match: Match):
        obj = match.to_obj()
        self.write("{\n", bold=True)
        for key, value in obj.items():
            if isinstance(value, list):
                # TODO: Maybe implement list printing later.
                #       I don't think there will be lists here currently, thouh.
                continue
            self.write(f"  {key!r}", color=ANSIColor.BLUE)
            self.write(": ", bold=True)
            if isinstance(value, int) or isinstance(value, float):
                self.write(str(value))
            else:
                self.write(repr(value), color=ANSIColor.GREEN)
            self.write(",\n", bold=True)
        self.write("}\n", bold=True)

    def debug_parse(self, instrumented_parser: InstrumentedParser, file_stream, match: Match) -> Iterator[Submatch]:
        log.clear_status()

        if instrumented_parser.original_parser is None:
            parse = instrumented_parser.parser.parse
        else:
            parse = instrumented_parser.original_parser

        def print_location():
            self.write(f"{file_stream.name}", dim=True, color=ANSIColor.CYAN)
            self.write(":", dim=True)
            self.write(f"{file_stream.tell()} ", dim=True, color=ANSIColor.CYAN)

        if self._pdb is not None:
            # We are already debugging!
            print_location()
            self.write(f"Parsing for submatches using {instrumented_parser.parser!s}.\n")
            yield from parse(file_stream, match)
            return
        self.print_match(match)
        print_location()
        self.write(f"About to parse for submatches using {instrumented_parser.parser!s}.\n")
        buffer = ANSIWriter(use_ansi=sys.stderr.isatty(), escape_for_readline=True)
        buffer.write("Debug using PDB? ")
        buffer.write("(disable this prompt with `", dim=True)
        buffer.write("set ", color=ANSIColor.BLUE)
        buffer.write("break_on_parsing ", color=ANSIColor.GREEN)
        buffer.write("False", color=ANSIColor.CYAN)
        buffer.write("`)", dim=True)
        if not self.prompt(str(buffer), default=False):
            yield from parse(file_stream, match)
            return
        try:
            self._pdb = Pdb(skip=["polyfile.magic_debugger", "polyfile.magic"])
            if sys.stderr.isatty():
                self._pdb.prompt = "\001\u001b[1m\002(polyfile-Pdb)\001\u001b[0m\002 "
            else:
                self._pdb.prompt = "(polyfile-Pdb) "
            generator = parse(file_stream, match)
            while True:
                try:
                    result = self._pdb.runcall(next, generator)
                    self.write(f"Got a submatch:\n", dim=True)
                    self.print_match(result)
                    yield result
                except StopIteration:
                    self.write(f"Yielded all submatches from {match.__class__.__name__} at offset {match.offset}.\n")
                    break
                print_location()
                if not self.prompt("Continue debugging the next submatch?", default=True):
                    if self.prompt("Print the remaining submatches?", default=False):
                        for result in generator:
                            self.print_match(result)
                            yield result
                    else:
                        yield from generator
                    break
        finally:
            self._pdb = None

    def repl(self):
        log.clear_status()
        if self.last_test is not None:
            self.print_where()
        while True:
            try:
                command = input(self.repl_prompt)
            except EOFError:
                # the user pressed ^D to quit
                exit(0)
            if not command:
                if self.last_command is None:
                    continue
                command = self.last_command
            command = command.lstrip()
            space_index = command.find(" ")
            if space_index > 0:
                command, args = command[:space_index], command[space_index+1:].strip()
            else:
                args = ""
            if "help".startswith(command):
                usage = [
                    ("help", "print this message"),
                    ("continue", "continue execution until the next breakpoint is hit"),
                    ("step", "step through a single magic test"),
                    ("next", "continue execution until the next test that matches"),
                    ("where", "print the context of the current magic test"),
                    ("test", "test the following libmagic DSL test at the current position"),
                    ("print", "print the computed absolute offset of the following libmagic DSL offset"),
                    ("breakpoint", "list the current breakpoints or add a new one"),
                    ("delete", "delete a breakpoint"),
                    ("set", "modifies part of the debugger environment"),
                    ("show", "prints part of the debugger environment"),
                    ("quit", "exit the debugger"),
                ]
                aliases = {
                    "where": ("info stack", "backtrace")
                }
                left_col_width = max(len(u[0]) for u in usage)
                left_col_width = max(left_col_width, max(len(c) for a in aliases.values() for c in a))
                left_col_width += 3
                for command, msg in usage:
                    self.write(command, bold=True, color=ANSIColor.BLUE)
                    self.write(f" {'.' * (left_col_width - len(command) - 2)} ")
                    self.write(msg)
                    if command in aliases:
                        self.write(" (aliases: ", dim=True)
                        alternatives = aliases[command]
                        for i, alt in enumerate(alternatives):
                            if i > 0 and len(alternatives) > 2:
                                self.write(", ", dim=True)
                            if i == len(alternatives) - 1 and len(alternatives) > 1:
                                self.write(" and ", dim=True)
                            self.write(alt, bold=True, color=ANSIColor.BLUE)
                        self.write(")", dim=True)
                    self.write("\n")

            elif "continue".startswith(command) or "run".startswith(command):
                self.step_mode = StepMode.RUNNING
                self.last_command = command
                return
            elif "step".startswith(command):
                self.step_mode = StepMode.SINGLE_STEPPING
                self.last_command = command
                return
            elif "next".startswith(command):
                self.step_mode = StepMode.NEXT
                self.last_command = command
                return
            elif "quit".startswith(command):
                exit(0)
            elif "delete".startswith(command):
                if args:
                    try:
                        breakpoint_num = int(args)
                    except ValueError:
                        breakpoint_num = -1
                    if not (0 <= breakpoint_num < len(self.breakpoints)):
                        print(f"Error: Invalid breakpoint \"{args}\"")
                        continue
                    b = self.breakpoints[breakpoint_num]
                    self.breakpoints = self.breakpoints[:breakpoint_num] + self.breakpoints[breakpoint_num + 1:]
                    self.write(f"Deleted {b!s}\n")
            elif "test".startswith(command):
                if args:
                    if self.last_test is None:
                        self.write("The first test has not yet been run.\n", color=ANSIColor.RED)
                        self.write("Use `step`, `next`, or `run` to start testing.\n")
                        continue
                    try:
                        test = MagicMatcher.parse_test(args, Path("STDIN"), 1, parent=self.last_test)
                        if test is None:
                            self.write("Error parsing test\n", color=ANSIColor.RED)
                            continue
                        try:
                            with self.save_context():
                                self.repl_test = test
                                if test.parent is None:
                                    self.last_result = None
                                    self.last_offset = 0
                                result = test.test(self.data, self.last_offset, parent_match=self.last_result)
                        finally:
                            if test.parent is not None:
                                test.parent.children.remove(test)
                        self.print_where(
                           test=test, offset=self.last_offset, parent_match=self.last_result, result=result
                        )
                    except ValueError as e:
                        self.write(f"{e!s}\n", color=ANSIColor.RED)
                else:
                    self.write("Usage: ", dim=True)
                    self.write("test", bold=True, color=ANSIColor.BLUE)
                    self.write(" LIBMAGIC DSL TEST\n", bold=True)
                    self.write("Attempt to run the given test.\n\nExample:\n")
                    self.write("test", bold=True, color=ANSIColor.BLUE)
                    self.write(" 0 search \\x50\\x4b\\x05\\x06 ZIP EOCD record\n", bold=True)
            elif "breakpoint".startswith(command):
                if args:
                    parsed = Breakpoint.from_str(args)
                    if parsed is None:
                        self.write("Error: Invalid breakpoint pattern\n", color=ANSIColor.RED)
                    else:
                        self.write(parsed, color=ANSIColor.MAGENTA)
                        self.write("\n")
                        self.breakpoints.append(parsed)
                else:
                    if self.breakpoints:
                        for i, b in enumerate(self.breakpoints):
                            self.write(f"{i}:\t", dim=True)
                            self.write(b, color=ANSIColor.MAGENTA)
                            self.write("\n")
                    else:
                        self.write("No breakpoints set.\n", color=ANSIColor.RED)
                        for b_type in BREAKPOINT_TYPES:
                            b_type.print_usage(self)
                            self.write("\n")
                        self.write("\nBy default, breakpoints will trigger whenever a matching test is run.\n\n"
                                   "Prepend a breakpoint with ")
                        self.write("!", bold=True)
                        self.write(" to only trigger the breakpoint when the test fails.\nFor Example:\n")
                        self.write("    b !MIME:application/zip\n", color=ANSIColor.MAGENTA)
                        self.write("will only trigger if a test that could match a ZIP file failed.\n\n"
                                   "Prepend a breakpoint with ")
                        self.write("=", bold=True)
                        self.write(" to only trigger the breakpoint when the test passes.\n For example:\n")
                        self.write("    b =archive:1337\n", color=ANSIColor.MAGENTA)
                        self.write("will only trigger if the test on line 1337 of the archive DSL matched.\n\n")

            elif "print".startswith(command):
                if args:
                    if self.last_test is None:
                        self.write("The first test has not yet been run.\n", color=ANSIColor.RED)
                        self.write("Use `step`, `next`, or `run` to start testing.\n")
                        continue
                    try:
                        dsl_offset = Offset.parse(args)
                    except ValueError as e:
                        self.write(f"{e!s}\n", color=ANSIColor.RED)
                        continue
                    try:
                        absolute = dsl_offset.to_absolute(self.data, self.last_result)
                        self.write(f"{absolute}\n", bold=True)
                        self.print_context(self.data, absolute)
                    except InvalidOffsetError as e:
                        self.write(f"{e!s}\n", color=ANSIColor.RED)
                        continue
                else:
                    self.write("Usage: ", dim=True)
                    self.write("print", bold=True, color=ANSIColor.BLUE)
                    self.write(" LIBMAGIC DSL OFFSET\n", bold=True)
                    self.write("Calculate the absolute offset for the given DSL offset.\n\nExample:\n")
                    self.write("print", bold=True, color=ANSIColor.BLUE)
                    self.write(" (&0x7c.l+0x26)\n", bold=True)
            elif "where".startswith(command) or "info stack".startswith(command) or "backtrace".startswith(command):
                self.print_where()
            elif command == "set":
                parsed = args.strip().split()
                if len(parsed) == 3 and parsed[1].strip() == "=":
                    parsed = [parsed[0], parsed[1]]
                if len(parsed) != 2:
                    self.write("Usage: ", dim=True)
                    self.write("set", bold=True, color=ANSIColor.BLUE)
                    self.write(" VARIABLE ", bold=True, color=ANSIColor.GREEN)
                    self.write("VALUE\n\n", bold=True, color=ANSIColor.CYAN)
                    self.write("Options:\n\n", bold=True)
                    for name, var in self.variables_by_name.items():
                        self.write(f"    {name} ", bold=True, color=ANSIColor.GREEN)
                        self.write("[", dim=True)
                        for i, value in enumerate(var.possibilities):
                            if i > 0:
                                self.write("|", dim=True)
                            self.write(str(value), bold=True, color=ANSIColor.CYAN)
                        self.write("]\n    ", dim=True)
                        self.write(self.variable_descriptions[name])
                        self.write("\n\n")
                elif parsed[0] not in self.variables_by_name:
                    self.write("Error: Unknown variable ", bold=True, color=ANSIColor.RED)
                    self.write(parsed[0], bold=True)
                    self.write("\n")
                else:
                    try:
                        var = self.variables_by_name[parsed[0]]
                        var.value = var.parse(parsed[1])
                    except ValueError as e:
                        self.write(f"{e!s}\n", bold=True, color=ANSIColor.RED)
            elif command == "show":
                parsed = args.strip().split()
                if len(parsed) > 2:
                    self.write("Usage: ", dim=True)
                    self.write("show", bold=True, color=ANSIColor.BLUE)
                    self.write(" VARIABLE\n\n", bold=True, color=ANSIColor.GREEN)
                    self.write("Options:\n", bold=True)
                    for name, var in self.variables_by_name.items():
                        self.write(f"\n    {name}\n    ", bold=True, color=ANSIColor.GREEN)
                        self.write(self.variable_descriptions[name])
                        self.write("\n")
                elif not parsed:
                    for i, (name, var) in enumerate(self.variables_by_name.items()):
                        if i > 0:
                            self.write("\n")
                        self.write(name, bold=True, color=ANSIColor.GREEN)
                        self.write(" = ", dim=True)
                        self.write(str(var.value), bold=True, color=ANSIColor.CYAN)
                        self.write("\n")
                        self.write(self.variable_descriptions[name])
                        self.write("\n")
                elif parsed[0] not in self.variables_by_name:
                    self.write("Error: Unknown variable ", bold=True, color=ANSIColor.RED)
                    self.write(parsed[0], bold=True)
                    self.write("\n")
                else:
                    self.write(parsed[0], bold=True, color=ANSIColor.GREEN)
                    self.write(" = ", dim=True)
                    self.write(str(variables_by_name[parsed[0]].value), bold=True, color=ANSIColor.CYAN)
                    self.write("\n")
                    self.write(self.variable_descriptions[parsed[0]])
            else:
                self.write(f"Undefined command: {command!r}. Try \"help\".\n", color=ANSIColor.RED)
                self.last_command = None
                continue
            self.last_command = command
