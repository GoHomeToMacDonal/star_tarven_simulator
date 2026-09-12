#!/usr/bin/env python3
"""A small interpreter for the subset of Galaxy that SC2 map scripts generate.

Star Tavern builds every card description at runtime: card registration stores a
flat "special string" and ``gf_根据特效字符串生成描述文本`` renders it against the
localization tables.  Re-implementing that renderer by hand is guesswork, so this
module runs the map's own code instead.

The supported subset is what the StarCraft II editor emits: local declarations,
assignments, ``if``/``else if``/``else``, ``while``, ``for``, ``return``,
``break``, ``continue``, calls, arrays, and structs.  Everything outside that
subset (or an unimplemented native) raises :class:`GalaxyError` rather than
guessing, so callers can record a diagnostic instead of shipping wrong text.
"""

from __future__ import annotations

import math
import random
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

# Galaxy frames cost several Python frames each; map functions nest deeply.
sys.setrecursionlimit(100_000)

NUMERIC_TYPES = {"int", "fixed", "byte"}
# Engine constants the description/registration path actually depends on.
ENGINE_CONSTANTS: dict[str, Any] = {
    "c_unitCostMinerals": 0,
    "c_unitCostVespene": 1,
    "c_unitCostTerrazine": 2,
    "c_unitCostCustom": 3,
    "c_unitCostSupply": 4,
}
COST_RESOURCES = {0: "Minerals", 1: "Vespene", 2: "Terrazine", 3: "Custom", 4: "Supply"}
DEFAULTS: dict[str, Any] = {
    "int": 0,
    "byte": 0,
    "fixed": 0.0,
    "bool": False,
    "string": "",
    # Galaxy leaves text variables null until assigned, and map code tests that
    # (`if ((lv_text != null) == false) return null;`) to mean "nothing to say".
    "text": None,
}


class GalaxyError(RuntimeError):
    """The interpreter refused to guess: unsupported syntax, native or value."""


class MissingNative(GalaxyError):
    """A native (engine-provided) function has no Python implementation."""


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Color:
    """Galaxy colours are percentages; SC2 markup wants 8-bit hex."""

    red: float
    green: float
    blue: float
    alpha: float = 100.0

    def hex(self) -> str:
        channels = (self.red, self.green, self.blue)
        return "".join(f"{max(0, min(255, round(value * 255.0 / 100.0))):02X}" for value in channels)


@dataclass
class Struct:
    type_name: str
    fields: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str) -> Any:
        if name not in self.fields:
            raise GalaxyError(f"Struct {self.type_name} has no field {name}")
        return self.fields[name]

    def set(self, name: str, value: Any) -> None:
        # Generated code never invents fields, so an unknown name is a real bug.
        if name not in self.fields:
            raise GalaxyError(f"Struct {self.type_name} has no field {name}")
        self.fields[name] = value


class Array:
    """Sparse Galaxy array: declared sizes may depend on runtime globals."""

    def __init__(self, factory: Callable[[], Any]):
        self.factory = factory
        self.values: dict[int, Any] = {}

    def get(self, index: int) -> Any:
        if index not in self.values:
            self.values[index] = self.factory()
        return self.values[index]

    def set(self, index: int, value: Any) -> None:
        self.values[index] = value


@dataclass(frozen=True)
class Trigger:
    """Triggers are opaque here; only their identity/name is ever inspected."""

    name: str


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

TOKEN_PATTERN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<line_comment>//[^\n]*)
  | (?P<block_comment>/\*.*?\*/)
  | (?P<string>"(?:[^"\\]|\\.)*")
  | (?P<fixed>(?:\d+\.\d*|\.\d+))
  | (?P<int>\d+)
  | (?P<name>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<op><<|>>|==|!=|<=|>=|&&|\|\||\+=|-=|\*=|/=|\|=|&=|\^=|[-+*/%<>=!()\[\]{},;.:?~^&|])
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass(frozen=True)
class Token:
    kind: str
    value: Any
    position: int


def _decode_string_literal(raw: str) -> str:
    escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
    output: list[str] = []
    index = 1
    while index < len(raw) - 1:
        char = raw[index]
        if char == "\\" and index + 1 < len(raw) - 1:
            output.append(escapes.get(raw[index + 1], raw[index + 1]))
            index += 2
        else:
            output.append(char)
            index += 1
    return "".join(output)


def tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    position = 0
    length = len(source)
    while position < length:
        match = TOKEN_PATTERN.match(source, position)
        if match is None:
            raise GalaxyError(f"Cannot tokenize Galaxy source at offset {position}: {source[position:position + 40]!r}")
        kind = match.lastgroup
        text = match.group()
        position = match.end()
        if kind in {"space", "line_comment", "block_comment"}:
            continue
        if kind == "string":
            tokens.append(Token("string", _decode_string_literal(text), match.start()))
        elif kind == "int":
            tokens.append(Token("int", int(text), match.start()))
        elif kind == "fixed":
            tokens.append(Token("fixed", float(text), match.start()))
        elif kind == "name":
            tokens.append(Token("name", text, match.start()))
        else:
            tokens.append(Token("op", text, match.start()))
    tokens.append(Token("eof", None, length))
    return tokens


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

TYPE_KEYWORDS = {
    "void", "bool", "int", "fixed", "byte", "string", "text", "trigger", "unit", "point", "region",
    "player", "playergroup", "unitgroup", "unitfilter", "order", "color", "actor", "actorscope",
    "doodad", "sound", "soundlink", "timer", "transmissionsource", "wave", "waveinfo", "wavetarget",
    "abilcmd", "aifilter", "bank", "camerainfo", "dialog", "control", "revealer", "marker", "objective",
    "ping", "portrait", "reply", "generichandle", "genericevent", "camerapath", "attachedmodel",
    "planet", "planetpanelcanvas", "layoutframe", "cutscene", "location", "unitref", "handle",
}

ASSIGN_OPERATORS = {"=", "+=", "-=", "*=", "/="}


class Parser:
    def __init__(self, tokens: Sequence[Token], structs: Iterable[str] = ()):
        self.tokens = tokens
        self.index = 0
        self.struct_names = set(structs)

    # -- token helpers ----------------------------------------------------
    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.index + offset, len(self.tokens) - 1)]

    def advance(self) -> Token:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def accept_op(self, value: str) -> bool:
        if self.current.kind == "op" and self.current.value == value:
            self.index += 1
            return True
        return False

    def expect_op(self, value: str) -> Token:
        if self.current.kind != "op" or self.current.value != value:
            raise GalaxyError(f"Expected {value!r} but found {self.current.kind}:{self.current.value!r}")
        return self.advance()

    def accept_name(self, value: str) -> bool:
        if self.current.kind == "name" and self.current.value == value:
            self.index += 1
            return True
        return False

    # -- declarations -----------------------------------------------------
    def looks_like_declaration(self) -> bool:
        token = self.current
        if token.kind != "name":
            return False
        if token.value == "const":
            return True
        if token.value not in TYPE_KEYWORDS and token.value not in self.struct_names:
            return False
        offset = 1
        # structref<gs_Foo>
        if self.peek(offset).kind == "op" and self.peek(offset).value == "<":
            while not (self.peek(offset).kind == "op" and self.peek(offset).value == ">"):
                offset += 1
            offset += 1
        while self.peek(offset).kind == "op" and self.peek(offset).value == "[":
            depth = 0
            while True:
                token = self.peek(offset)
                if token.kind == "op" and token.value == "[":
                    depth += 1
                elif token.kind == "op" and token.value == "]":
                    depth -= 1
                    if depth == 0:
                        offset += 1
                        break
                elif token.kind == "eof":
                    return False
                offset += 1
        return self.peek(offset).kind == "name"

    def parse_type(self) -> str:
        name = self.advance()
        if name.kind != "name":
            raise GalaxyError(f"Expected a type name, found {name.value!r}")
        type_name = name.value
        if self.accept_op("<"):
            inner = self.advance()
            self.expect_op(">")
            return inner.value  # structref<gs_Foo> behaves like gs_Foo by reference.
        return type_name

    def parse_declaration(self) -> tuple[str, str, str, list[Any], Any]:
        """Return (kind, type, name, dimensions, initializer)."""
        is_const = self.accept_name("const")
        type_name = self.parse_type()
        dimensions: list[Any] = []
        while self.accept_op("["):
            dimensions.append(self.parse_expression())
            self.expect_op("]")
        name_token = self.advance()
        if name_token.kind != "name":
            raise GalaxyError(f"Expected a declaration name, found {name_token.value!r}")
        initializer = self.parse_expression() if self.accept_op("=") else None
        self.expect_op(";")
        return ("const" if is_const else "var", type_name, name_token.value, dimensions, initializer)

    # -- statements -------------------------------------------------------
    def parse_block(self) -> list[Any]:
        self.expect_op("{")
        statements: list[Any] = []
        while not self.accept_op("}"):
            if self.current.kind == "eof":
                raise GalaxyError("Unterminated block")
            statements.append(self.parse_statement())
        return statements

    def parse_statement(self) -> Any:
        token = self.current
        if token.kind == "op" and token.value == "{":
            return ("block", self.parse_block())
        if token.kind == "op" and token.value == ";":
            self.advance()
            return ("empty",)
        if token.kind == "name":
            if token.value == "if":
                return self.parse_if()
            if token.value == "while":
                return self.parse_while()
            if token.value == "for":
                return self.parse_for()
            if token.value == "do":
                return self.parse_do_while()
            if token.value == "return":
                self.advance()
                if self.accept_op(";"):
                    return ("return", None)
                value = self.parse_expression()
                self.expect_op(";")
                return ("return", value)
            if token.value == "break":
                self.advance()
                self.expect_op(";")
                return ("break",)
            if token.value == "continue":
                self.advance()
                self.expect_op(";")
                return ("continue",)
            if self.looks_like_declaration():
                return ("declare", *self.parse_declaration())
        return self.parse_simple_statement(require_semicolon=True)

    def parse_simple_statement(self, *, require_semicolon: bool) -> Any:
        target = self.parse_expression()
        statement: Any
        if self.current.kind == "op" and self.current.value in ASSIGN_OPERATORS:
            operator = self.advance().value
            value = self.parse_expression()
            statement = ("assign", target, operator, value)
        else:
            statement = ("expression", target)
        if require_semicolon:
            self.expect_op(";")
        return statement

    def parse_if(self) -> Any:
        self.advance()
        self.expect_op("(")
        condition = self.parse_expression()
        self.expect_op(")")
        body = self.parse_block() if self.current.value == "{" else [self.parse_statement()]
        branches = [(condition, body)]
        otherwise: list[Any] = []
        if self.accept_name("else"):
            if self.current.kind == "name" and self.current.value == "if":
                nested = self.parse_if()
                branches.extend(nested[1])
                otherwise = nested[2]
            else:
                otherwise = self.parse_block() if self.current.value == "{" else [self.parse_statement()]
        return ("if", branches, otherwise)

    def parse_while(self) -> Any:
        self.advance()
        self.expect_op("(")
        condition = self.parse_expression()
        self.expect_op(")")
        body = self.parse_block() if self.current.value == "{" else [self.parse_statement()]
        return ("while", condition, body)

    def parse_do_while(self) -> Any:
        self.advance()
        body = self.parse_block()
        if not self.accept_name("while"):
            raise GalaxyError("Expected 'while' after 'do' block")
        self.expect_op("(")
        condition = self.parse_expression()
        self.expect_op(")")
        self.expect_op(";")
        return ("do_while", condition, body)

    def parse_for(self) -> Any:
        self.advance()
        self.expect_op("(")
        initializer = None if self.current.value == ";" else self.parse_simple_statement(require_semicolon=False)
        self.expect_op(";")
        condition = None if self.current.value == ";" else self.parse_expression()
        self.expect_op(";")
        step = None if self.current.value == ")" else self.parse_simple_statement(require_semicolon=False)
        self.expect_op(")")
        body = self.parse_block() if self.current.value == "{" else [self.parse_statement()]
        return ("for", initializer, condition, step, body)

    # -- expressions ------------------------------------------------------
    BINARY_LEVELS = (
        ("||",),
        ("&&",),
        ("==", "!="),
        ("<=", ">=", "<", ">"),
        ("+", "-"),
        ("*", "/", "%"),
    )

    def parse_expression(self, level: int = 0) -> Any:
        if level >= len(self.BINARY_LEVELS):
            return self.parse_unary()
        operators = self.BINARY_LEVELS[level]
        node = self.parse_expression(level + 1)
        while self.current.kind == "op" and self.current.value in operators:
            operator = self.advance().value
            right = self.parse_expression(level + 1)
            node = ("binary", operator, node, right)
        return node

    def parse_unary(self) -> Any:
        if self.current.kind == "op" and self.current.value in {"-", "!", "+"}:
            operator = self.advance().value
            operand = self.parse_unary()
            return operand if operator == "+" else ("unary", operator, operand)
        return self.parse_postfix()

    def parse_postfix(self) -> Any:
        node = self.parse_primary()
        while True:
            if self.accept_op("["):
                index = self.parse_expression()
                self.expect_op("]")
                node = ("index", node, index)
            elif self.accept_op("."):
                field_token = self.advance()
                if field_token.kind != "name":
                    raise GalaxyError("Expected a field name after '.'")
                node = ("member", node, field_token.value)
            else:
                return node

    def parse_primary(self) -> Any:
        token = self.advance()
        if token.kind == "int":
            return ("int", token.value)
        if token.kind == "fixed":
            return ("fixed", token.value)
        if token.kind == "string":
            return ("string", token.value)
        if token.kind == "op" and token.value == "(":
            node = self.parse_expression()
            self.expect_op(")")
            return node
        if token.kind == "name":
            if token.value == "true":
                return ("bool", True)
            if token.value == "false":
                return ("bool", False)
            if token.value == "null":
                return ("null",)
            if self.current.kind == "op" and self.current.value == "(":
                self.advance()
                arguments: list[Any] = []
                if not self.accept_op(")"):
                    while True:
                        arguments.append(self.parse_expression())
                        if self.accept_op(")"):
                            break
                        self.expect_op(",")
                return ("call", token.value, arguments)
            return ("name", token.value)
        raise GalaxyError(f"Unexpected token in expression: {token.kind}:{token.value!r}")


# ---------------------------------------------------------------------------
# Program model
# ---------------------------------------------------------------------------


@dataclass
class FunctionDef:
    name: str
    return_type: str
    parameters: list[tuple[str, str]]
    body_source: str
    body: list[Any] | None = None


@dataclass
class GlobalDecl:
    type_name: str
    name: str
    dimensions: list[str]
    initializer: str | None


DECLARATION_HEAD = re.compile(
    # Hand-written map sections write `trigger [N] name;`, so allow spaces
    # between the type and its array dimensions.
    r"(?:^|\n)[ \t]*(?:(const)\s+)?"
    r"(?P<type>[A-Za-z_][A-Za-z_0-9]*(?:<[A-Za-z_0-9]+>)?)"
    r"(?P<dims>(?:[ \t]*\[[^\];]*\])*)[ \t]+"
    r"(?P<name>[A-Za-z_][A-Za-z_0-9]*)[ \t]*(?P<tail>[=;])"
)
FUNCTION_HEAD = re.compile(
    r"(?:^|\n)(?P<ret>[A-Za-z_][A-Za-z_0-9]*(?:<[A-Za-z_0-9]+>)?)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z_0-9]*)\s*\("
)
STRUCT_HEAD = re.compile(r"(?:^|\n)struct\s+(?P<name>[A-Za-z_][A-Za-z_0-9]*)\s*\{(?P<body>[^}]*)\}\s*;")
STRUCT_FIELD = re.compile(
    r"^\s*(?P<type>[A-Za-z_][A-Za-z_0-9]*(?:<[A-Za-z_0-9]+>)?)"
    r"(?P<dims>(?:\[[^\]]*\])*)\s+(?P<name>[A-Za-z_][A-Za-z_0-9]*)\s*;"
)


def mask_comments_and_strings(source: str) -> str:
    """Blank comments and string bodies, preserving offsets and line counts."""
    result = list(source)
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char == '"':
            end = index + 1
            while end < length:
                if source[end] == "\\":
                    end += 2
                    continue
                if source[end] == '"':
                    break
                end += 1
            for position in range(index + 1, min(end, length)):
                if result[position] not in "\r\n":
                    result[position] = " "
            index = min(end + 1, length)
            continue
        if source.startswith("//", index):
            end = source.find("\n", index)
            end = length if end < 0 else end
            for position in range(index, end):
                result[position] = " "
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = length if end < 0 else end + 2
            for position in range(index, end):
                if result[position] not in "\r\n":
                    result[position] = " "
            index = end
            continue
        index += 1
    return "".join(result)


def _match_delimiter(masked: str, start: int, opening: str, closing: str) -> int:
    depth = 0
    for index in range(start, len(masked)):
        char = masked[index]
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
    raise GalaxyError(f"Unbalanced {opening}{closing} from offset {start}")


class Program:
    """Lazily parsed view of a generated MapScript.galaxy."""

    def __init__(self, source: str):
        self.source = source
        self.masked = mask_comments_and_strings(source)
        self.structs: dict[str, list[tuple[str, str, list[str]]]] = {}
        self.functions: dict[str, FunctionDef] = {}
        self.globals: dict[str, GlobalDecl] = {}
        self.constants: dict[str, GlobalDecl] = {}
        self._scan_structs()
        self._scan_functions()
        self._scan_globals()

    # -- scanning ---------------------------------------------------------
    def _scan_structs(self) -> None:
        for match in STRUCT_HEAD.finditer(self.masked):
            fields: list[tuple[str, str, list[str]]] = []
            body = self.source[match.start("body") : match.end("body")]
            for line in body.split(";"):
                field_match = STRUCT_FIELD.match(line + ";")
                if field_match is None:
                    continue
                dims = re.findall(r"\[([^\]]*)\]", field_match.group("dims"))
                fields.append((field_match.group("type"), field_match.group("name"), dims))
            self.structs[match.group("name")] = fields

    def _scan_functions(self) -> None:
        spans: list[tuple[int, int]] = []
        for match in FUNCTION_HEAD.finditer(self.masked):
            if match.group("ret") in {"struct", "include", "const", "return", "if", "while", "for", "else"}:
                continue
            open_parenthesis = self.masked.index("(", match.end() - 1)
            close_parenthesis = _match_delimiter(self.masked, open_parenthesis, "(", ")")
            cursor = close_parenthesis + 1
            while cursor < len(self.masked) and self.masked[cursor].isspace():
                cursor += 1
            # Generated scripts emit forward declarations; only '{' means a body.
            if cursor >= len(self.masked) or self.masked[cursor] != "{":
                continue
            body_end = _match_delimiter(self.masked, cursor, "{", "}")
            parameters: list[tuple[str, str]] = []
            raw_parameters = self.source[open_parenthesis + 1 : close_parenthesis].strip()
            if raw_parameters:
                for part in raw_parameters.split(","):
                    pieces = part.strip().split()
                    if len(pieces) < 2:
                        continue
                    parameters.append((" ".join(pieces[:-1]), pieces[-1]))
            name = match.group("name")
            self.functions[name] = FunctionDef(
                name=name,
                return_type=match.group("ret"),
                parameters=parameters,
                body_source=self.source[cursor + 1 : body_end],
            )
            spans.append((match.start(), body_end + 1))
        self._function_spans = spans

    def _top_level_masked(self) -> str:
        """Blank every function body so declaration scanning stays top level."""
        buffer = list(self.masked)
        for start, end in self._function_spans:
            for position in range(start, end):
                if buffer[position] not in "\r\n":
                    buffer[position] = " "
        return "".join(buffer)

    def _scan_globals(self) -> None:
        top_level = self._top_level_masked()
        for match in DECLARATION_HEAD.finditer(top_level):
            type_name = match.group("type")
            if type_name in {"struct", "include"} or type_name not in TYPE_KEYWORDS and type_name not in self.structs:
                continue
            name = match.group("name")
            dims = re.findall(r"\[([^\]]*)\]", match.group("dims"))
            initializer: str | None = None
            if match.group("tail") == "=":
                end = top_level.index(";", match.end("tail"))
                initializer = self.source[match.end("tail") : end]
            declaration = GlobalDecl(type_name, name, dims, initializer)
            if match.group(1) == "const":
                self.constants[name] = declaration
            else:
                self.globals[name] = declaration

    # -- parsing ----------------------------------------------------------
    def function_body(self, name: str) -> list[Any]:
        definition = self.functions[name]
        if definition.body is None:
            parser = Parser(tokenize(definition.body_source), self.structs)
            statements: list[Any] = []
            while parser.current.kind != "eof":
                statements.append(parser.parse_statement())
            definition.body = statements
        return definition.body

    def parse_expression_source(self, source: str) -> Any:
        parser = Parser(tokenize(source), self.structs)
        node = parser.parse_expression()
        if parser.current.kind != "eof":
            raise GalaxyError(f"Trailing tokens in expression: {source[:80]!r}")
        return node


# ---------------------------------------------------------------------------
# Interpreter
# ---------------------------------------------------------------------------


class ReturnSignal(Exception):
    def __init__(self, value: Any):
        self.value = value


class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


class Scope:
    __slots__ = ("values", "types")

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.types: dict[str, str] = {}


class Interpreter:
    MAX_STEPS = 20_000_000

    def __init__(
        self,
        program: Program,
        *,
        localization: dict[str, str] | None = None,
        unit_names: dict[str, str] | None = None,
        unit_costs: Callable[[str], dict[str, float] | None] | None = None,
        game_attribute_values: dict[str, str] | None = None,
        strict_unit_names: bool = False,
        seed: int = 0,
    ):
        self.program = program
        self.localization = localization or {}
        self.unit_names = unit_names or {}
        self.unit_costs = unit_costs
        # Lobby attribute id -> chosen value ("0001", "0002", …).  Anything not
        # listed reads back as the empty string, so its checks fall through.
        self.game_attribute_values = dict(game_attribute_values or {})
        self.strict_unit_names = strict_unit_names
        self.globals = Scope()
        self.data_table: dict[tuple[bool, str], Any] = {}
        self.expression_tokens: dict[str, dict[str, str]] = {}
        self.natives: dict[str, Callable[..., Any]] = {}
        self.hooks: dict[str, Callable[..., Any]] = {}
        self.missing_unit_names: set[str] = set()
        self.engine_constants: set[str] = set()
        self.game_attributes: set[str] = set()
        self.random_draws: list[tuple[Any, Any]] = []
        self.rng = random.Random(seed)
        self.steps = 0
        self._install_natives()

    # -- setup ------------------------------------------------------------
    def declare_globals(self) -> None:
        for name, declaration in self.program.constants.items():
            self.globals.types[name] = declaration.type_name
            self.globals.values[name] = self._initial_value(declaration)
        for name, declaration in self.program.globals.items():
            self.globals.types[name] = declaration.type_name
            self.globals.values[name] = self._initial_value(declaration)
        for name, declaration in list(self.program.functions.items()):
            if name.endswith("_Func"):
                continue
        # Trigger globals are declared as `trigger gt_x;`, which default to None.
        for name in self.program.globals:
            if name.startswith(("gt_", "lib1_gt_", "lib2_gt_")) and self.globals.values.get(name) is None:
                self.globals.values[name] = Trigger(name)

    def _initial_value(self, declaration: GlobalDecl) -> Any:
        if declaration.dimensions:
            return self._array_for(declaration.type_name, len(declaration.dimensions))
        if declaration.initializer is not None:
            try:
                return self.evaluate(self.program.parse_expression_source(declaration.initializer), self.globals)
            except GalaxyError:
                return self.default_value(declaration.type_name)
        return self.default_value(declaration.type_name)

    def _array_for(self, type_name: str, depth: int) -> Array:
        if depth <= 1:
            return Array(lambda: self.default_value(type_name))
        return Array(lambda: self._array_for(type_name, depth - 1))

    def default_value(self, type_name: str) -> Any:
        if type_name in DEFAULTS:
            return DEFAULTS[type_name]
        if type_name in self.program.structs:
            return self.new_struct(type_name)
        return None

    def new_struct(self, type_name: str) -> Struct:
        instance = Struct(type_name)
        for field_type, field_name, dims in self.program.structs[type_name]:
            instance.fields[field_name] = (
                self._array_for(field_type, len(dims)) if dims else self.default_value(field_type)
            )
        return instance

    def run_global_initialization(self, *, tolerant: bool = True) -> list[str]:
        """Execute InitGlobals; unsupported statements are reported, not fatal."""
        self.declare_globals()
        failures: list[str] = []
        if "InitGlobals" not in self.program.functions:
            return failures
        body = self.program.function_body("InitGlobals")
        scope = Scope()
        for statement in body:
            try:
                self.execute(statement, scope)
            except (GalaxyError, ReturnSignal, ZeroDivisionError, RecursionError) as error:
                if not tolerant:
                    raise
                failures.append(f"{type(error).__name__}: {error}")
        return failures

    # -- function calls ---------------------------------------------------
    def call_function(self, name: str, arguments: Sequence[Any]) -> Any:
        if name in self.hooks:
            return self.hooks[name](*arguments)
        definition = self.program.functions.get(name)
        if definition is None:
            native = self.natives.get(name)
            if native is None:
                raise MissingNative(f"No implementation for native {name!r}")
            return native(*arguments)
        scope = Scope()
        for (parameter_type, parameter_name), value in zip(definition.parameters, arguments):
            scope.values[parameter_name] = value
            scope.types[parameter_name] = parameter_type.split()[-1]
        if len(arguments) != len(definition.parameters):
            raise GalaxyError(
                f"{name} expects {len(definition.parameters)} arguments, received {len(arguments)}"
            )
        try:
            for statement in self.program.function_body(name):
                self.execute(statement, scope)
        except ReturnSignal as signal:
            return signal.value
        return self.default_value(definition.return_type) if definition.return_type != "void" else None

    # -- statements -------------------------------------------------------
    def execute(self, statement: Any, scope: Scope) -> None:
        self.steps += 1
        if self.steps > self.MAX_STEPS:
            raise GalaxyError("Interpreter step budget exhausted (possible infinite loop)")
        kind = statement[0]
        if kind == "expression":
            self.evaluate(statement[1], scope)
        elif kind == "assign":
            self.assign(statement[1], statement[2], statement[3], scope)
        elif kind == "declare":
            _, _, type_name, name, dimensions, initializer = statement
            scope.types[name] = type_name
            if dimensions:
                scope.values[name] = self._array_for(type_name, len(dimensions))
            elif initializer is not None:
                scope.values[name] = self.evaluate(initializer, scope)
            else:
                scope.values[name] = self.default_value(type_name)
        elif kind == "if":
            for condition, body in statement[1]:
                if self.truthy(self.evaluate(condition, scope)):
                    self.execute_block(body, scope)
                    return
            self.execute_block(statement[2], scope)
        elif kind == "while":
            while self.truthy(self.evaluate(statement[1], scope)):
                try:
                    self.execute_block(statement[2], scope)
                except BreakSignal:
                    break
                except ContinueSignal:
                    continue
        elif kind == "do_while":
            while True:
                try:
                    self.execute_block(statement[2], scope)
                except BreakSignal:
                    break
                except ContinueSignal:
                    pass
                if not self.truthy(self.evaluate(statement[1], scope)):
                    break
        elif kind == "for":
            _, initializer, condition, step, body = statement
            if initializer is not None:
                self.execute(initializer, scope)
            while condition is None or self.truthy(self.evaluate(condition, scope)):
                try:
                    self.execute_block(body, scope)
                except BreakSignal:
                    break
                except ContinueSignal:
                    pass
                if step is not None:
                    self.execute(step, scope)
        elif kind == "block":
            self.execute_block(statement[1], scope)
        elif kind == "return":
            raise ReturnSignal(None if statement[1] is None else self.evaluate(statement[1], scope))
        elif kind == "break":
            raise BreakSignal()
        elif kind == "continue":
            raise ContinueSignal()
        elif kind == "empty":
            return
        else:
            raise GalaxyError(f"Unsupported statement: {kind}")

    def execute_block(self, statements: Sequence[Any], scope: Scope) -> None:
        # Galaxy scopes are function-wide, so blocks reuse the caller's scope.
        for statement in statements:
            self.execute(statement, scope)

    def assign(self, target: Any, operator: str, value_node: Any, scope: Scope) -> None:
        value = self.evaluate(value_node, scope)
        if operator != "=":
            current = self.evaluate(target, scope)
            value = self.binary(operator[0], current, value)
        kind = target[0]
        if kind == "name":
            name = target[1]
            container = scope if name in scope.values else self.globals
            if name not in container.values and name not in self.globals.values:
                container = scope
            container.values[name] = value
        elif kind == "index":
            array = self.evaluate(target[1], scope)
            if not isinstance(array, Array):
                raise GalaxyError("Indexed assignment on a non-array value")
            array.set(self.as_int(self.evaluate(target[2], scope)), value)
        elif kind == "member":
            owner = self.evaluate(target[1], scope)
            if not isinstance(owner, Struct):
                raise GalaxyError("Field assignment on a non-struct value")
            owner.set(target[2], value)
        else:
            raise GalaxyError(f"Cannot assign to expression of kind {kind}")

    # -- expressions ------------------------------------------------------
    def evaluate(self, node: Any, scope: Scope) -> Any:
        kind = node[0]
        if kind in {"int", "fixed", "string", "bool"}:
            return node[1]
        if kind == "null":
            return None
        if kind == "name":
            name = node[1]
            if name in scope.values:
                return scope.values[name]
            if name in self.globals.values:
                return self.globals.values[name]
            if name in ENGINE_CONSTANTS:
                return ENGINE_CONSTANTS[name]
            if name.startswith("c_"):
                # Remaining engine constants live in the unshipped NativeLib
                # includes and only appear in UI code, so a sentinel keeps going.
                self.engine_constants.add(name)
                return 0
            raise GalaxyError(f"Unknown identifier {name!r}")
        if kind == "call":
            arguments = [self.evaluate(argument, scope) for argument in node[2]]
            return self.call_function(node[1], arguments)
        if kind == "index":
            array = self.evaluate(node[1], scope)
            if not isinstance(array, Array):
                raise GalaxyError("Indexing a non-array value")
            return array.get(self.as_int(self.evaluate(node[2], scope)))
        if kind == "member":
            owner = self.evaluate(node[1], scope)
            if not isinstance(owner, Struct):
                raise GalaxyError(f"Field access on a non-struct value ({owner!r})")
            return owner.get(node[2])
        if kind == "unary":
            operand = self.evaluate(node[2], scope)
            if node[1] == "-":
                return -operand
            if node[1] == "!":
                return not self.truthy(operand)
            raise GalaxyError(f"Unsupported unary operator {node[1]}")
        if kind == "binary":
            operator = node[1]
            if operator == "&&":
                return self.truthy(self.evaluate(node[2], scope)) and self.truthy(self.evaluate(node[3], scope))
            if operator == "||":
                return self.truthy(self.evaluate(node[2], scope)) or self.truthy(self.evaluate(node[3], scope))
            return self.binary(operator, self.evaluate(node[2], scope), self.evaluate(node[3], scope))
        raise GalaxyError(f"Unsupported expression node {kind}")

    def binary(self, operator: str, left: Any, right: Any) -> Any:
        if operator == "+":
            # Concatenating onto a null text yields the other operand, which is
            # how map code accumulates description fragments from a null start.
            if left is None and right is None:
                return None
            if isinstance(left, str) or isinstance(right, str):
                return self.as_text(left) + self.as_text(right)
            if left is None:
                return right
            if right is None:
                return left
            return left + right
        if operator == "-":
            return left - right
        if operator == "*":
            return left * right
        if operator == "/":
            if isinstance(left, int) and isinstance(right, int) and not isinstance(left, bool):
                if right == 0:
                    raise GalaxyError("Integer division by zero")
                # Galaxy truncates integer division toward zero.
                return int(left / right)
            if right == 0:
                raise GalaxyError("Division by zero")
            return left / right
        if operator == "%":
            return left % right
        if operator == "==":
            return self.equal(left, right)
        if operator == "!=":
            return not self.equal(left, right)
        if operator == "<":
            return self.compare(left, right) < 0
        if operator == ">":
            return self.compare(left, right) > 0
        if operator == "<=":
            return self.compare(left, right) <= 0
        if operator == ">=":
            return self.compare(left, right) >= 0
        raise GalaxyError(f"Unsupported binary operator {operator}")

    @staticmethod
    def equal(left: Any, right: Any) -> bool:
        if left is None or right is None:
            return left is right or (left is None and right is None)
        if isinstance(left, str) != isinstance(right, str):
            return False
        return left == right

    @staticmethod
    def compare(left: Any, right: Any) -> int:
        if left is None:
            left = 0
        if right is None:
            right = 0
        return -1 if left < right else (0 if left == right else 1)

    @staticmethod
    def truthy(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return bool(value)
        return True

    @staticmethod
    def as_int(value: Any) -> int:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if value is None:
            return 0
        raise GalaxyError(f"Cannot use {value!r} as an array index")

    @staticmethod
    def as_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return value
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return f"{value:.6f}".rstrip("0").rstrip(".")
        raise GalaxyError(f"Cannot convert {value!r} to text")

    # -- natives ----------------------------------------------------------
    def _install_natives(self) -> None:
        def string_external(key: Any) -> str:
            if key is None:
                return ""
            if key not in self.localization:
                raise GalaxyError(f"Missing localization entry {key!r}")
            return self.localization[key]

        def string_length(value: Any) -> int:
            return len(value or "")

        def string_sub(value: Any, begin: Any, end: Any) -> str | None:
            text = value or ""
            begin_index = max(1, self.as_int(begin))
            end_index = min(len(text), self.as_int(end))
            # SC2 returns null (not "") for an empty or invalid range, and map
            # code relies on that to detect absent keyword slots.
            if end_index < begin_index:
                return None
            return text[begin_index - 1 : end_index]

        def string_find(value: Any, search: Any, exact: Any = True) -> int:
            text = value or ""
            needle = search or ""
            if not exact:
                text, needle = text.casefold(), needle.casefold()
            index = text.find(needle)
            # SC2 reports a 1-based position and -1 when the needle is absent.
            return -1 if index < 0 else index + 1

        def string_replace(value: Any, replacement: Any, begin: Any, end: Any) -> str:
            text = value or ""
            begin_index = max(1, self.as_int(begin))
            end_index = min(len(text), self.as_int(end))
            if end_index < begin_index:
                return text
            return text[: begin_index - 1] + (replacement or "") + text[end_index:]

        def string_replace_word(value: Any, search: Any, replacement: Any, occurrence: Any, exact: Any) -> str:
            text = value or ""
            needle = search or ""
            if not needle:
                return text
            count = self.as_int(occurrence)
            if not self.truthy(exact):
                # Case-insensitive replacement walks the casefolded copy.
                pattern = re.compile(re.escape(needle), re.IGNORECASE)
                return pattern.sub(replacement or "", text, count=0 if count <= 0 else count)
            if count <= 0:
                return text.replace(needle, replacement or "")
            pieces = text.split(needle)
            if len(pieces) <= count:
                return text
            return needle.join(pieces[:count]) + (replacement or "") + needle.join(pieces[count:])

        def string_case(value: Any, upper: Any) -> str:
            return (value or "").upper() if self.truthy(upper) else (value or "").lower()

        def string_equal(left: Any, right: Any, exact: Any = True) -> bool:
            if self.truthy(exact):
                return (left or "") == (right or "")
            return (left or "").casefold() == (right or "").casefold()

        def string_contains(value: Any, search: Any, location: Any = 0, exact: Any = True) -> bool:
            return string_find(value, search, exact) > 0

        def string_word(value: Any, index: Any) -> str:
            words = (value or "").split()
            position = self.as_int(index)
            return words[position - 1] if 1 <= position <= len(words) else ""

        def text_with_color(value: Any, color: Any) -> str:
            if not isinstance(color, Color):
                raise GalaxyError(f"TextWithColor expects a color, received {color!r}")
            return f'<c val="{color.hex()}">{self.as_text(value)}</c>'

        def text_expression_set_token(expression: Any, token: Any, value: Any) -> None:
            self.expression_tokens.setdefault(expression, {})[token] = self.as_text(value)

        def text_expression_assemble(expression: Any) -> str:
            if expression not in self.localization:
                raise GalaxyError(f"Missing localization expression {expression!r}")
            assembled = self.localization[expression]
            tokens = self.expression_tokens.get(expression, {})
            for token in re.findall(r"~([A-Za-z0-9_]+)~", assembled):
                if token not in tokens:
                    raise GalaxyError(f"Expression {expression!r} is missing token ~{token}~")
                assembled = assembled.replace(f"~{token}~", tokens[token])
            return assembled

        def unit_type_get_name(unit_type: Any) -> str:
            if unit_type is None:
                return ""
            # The map's own localization wins; the supplement only fills in base
            # game units, whose strings do not ship inside the map.
            key = f"Unit/Name/{unit_type}"
            if key in self.localization:
                return self.localization[key]
            if unit_type in self.unit_names:
                return self.unit_names[unit_type]
            self.missing_unit_names.add(unit_type)
            if self.strict_unit_names:
                raise GalaxyError(f"Missing unit name for {unit_type!r}")
            return unit_type

        def random_int(low: Any, high: Any) -> int:
            # Registration should be deterministic; record every draw so callers
            # can tell whether a card was randomised.
            low_value, high_value = self.as_int(low), self.as_int(high)
            self.random_draws.append((low_value, high_value))
            return self.rng.randint(low_value, high_value) if high_value >= low_value else low_value

        def random_fixed(low: Any, high: Any) -> float:
            self.random_draws.append((float(low), float(high)))
            return self.rng.uniform(float(low), float(high))

        def game_attribute(name: Any, *_: Any) -> str:
            attribute = self.as_text(name)
            self.game_attributes.add(attribute)
            return self.game_attribute_values.get(attribute, "")

        def unit_type_get_cost(unit_type: Any, cost_type: Any) -> int:
            if self.unit_costs is None:
                raise MissingNative("UnitTypeGetCost needs a unit cost catalog")
            costs = self.unit_costs(unit_type) if unit_type else None
            resource = COST_RESOURCES.get(self.as_int(cost_type))
            if not costs or resource is None:
                return 0
            return int(costs.get(resource, 0.0))

        def data_table_key(is_global: Any, name: Any) -> tuple[bool, str]:
            return (bool(self.truthy(is_global)), name or "")

        def data_table_set(is_global: Any, name: Any, value: Any) -> None:
            self.data_table[data_table_key(is_global, name)] = value

        def data_table_get(default: Any) -> Callable[..., Any]:
            def getter(is_global: Any, name: Any) -> Any:
                # Galaxy returns the type's null/zero value for unset entries and
                # map code relies on that to test existence.
                return self.data_table.get(data_table_key(is_global, name), default)

            return getter

        def data_table_remove(is_global: Any, name: Any) -> None:
            self.data_table.pop(data_table_key(is_global, name), None)

        def data_table_value_exists(is_global: Any, name: Any) -> bool:
            return data_table_key(is_global, name) in self.data_table

        def fixed_to_string(value: Any, precision: Any) -> str:
            digits = self.as_int(precision)
            if digits < 0:
                return f"{float(value):g}"
            return f"{float(value):.{digits}f}"

        self.natives.update(
            {
                "StringExternal": string_external,
                "StringLength": string_length,
                "StringSub": string_sub,
                "StringFind": string_find,
                "StringReplace": string_replace,
                "StringCase": string_case,
                "StringReplaceWord": string_replace_word,
                "StringEqual": string_equal,
                "StringContains": string_contains,
                "StringWord": string_word,
                "StringToText": lambda value: self.as_text(value),
                "TextToString": lambda value: self.as_text(value),
                "StringToInt": lambda value: int(float(value)) if _is_number(value) else 0,
                "StringToFixed": lambda value: float(value) if _is_number(value) else 0.0,
                "IntToString": lambda value: str(self.as_int(value)),
                "IntToText": lambda value: str(self.as_int(value)),
                "FixedToString": fixed_to_string,
                "FixedToText": fixed_to_string,
                "FixedToInt": lambda value: int(value or 0),
                "IntToFixed": lambda value: float(self.as_int(value)),
                "RoundI": lambda value: int(math.floor(float(value or 0) + 0.5)),
                "AbsI": lambda value: abs(self.as_int(value)),
                "AbsF": lambda value: abs(float(value or 0)),
                "MinI": lambda left, right: min(self.as_int(left), self.as_int(right)),
                "MaxI": lambda left, right: max(self.as_int(left), self.as_int(right)),
                "MinF": lambda left, right: min(float(left or 0), float(right or 0)),
                "MaxF": lambda left, right: max(float(left or 0), float(right or 0)),
                "ModI": lambda left, right: self.as_int(left) % self.as_int(right) if self.as_int(right) else 0,
                "SquareRoot": lambda value: math.sqrt(float(value or 0)),
                "CeilingI": lambda value: math.ceil(float(value or 0)),
                "FloorI": lambda value: math.floor(float(value or 0)),
                "Pow": lambda base, exponent: float(base) ** float(exponent),
                "RandomInt": random_int,
                "RandomFixed": random_fixed,
                # Lobby attributes are unavailable offline.  Anything the caller
                # did not pin reads back as the empty string, so its checks
                # (team mode, PvE, …) fall through to false.
                "GameAttributeGameValue": game_attribute,
                "TextWithColor": text_with_color,
                "Color": lambda red, green, blue: Color(float(red), float(green), float(blue)),
                "ColorWithAlpha": lambda red, green, blue, alpha: Color(
                    float(red), float(green), float(blue), float(alpha)
                ),
                "TextExpressionSetToken": text_expression_set_token,
                "TextExpressionAssemble": text_expression_assemble,
                "UnitTypeGetName": unit_type_get_name,
                "UnitTypeGetCost": unit_type_get_cost,
                # Unit types are plain strings in Galaxy, so the conversion is a
                # validity check that behaves as identity for known ids.
                "UnitTypeFromString": lambda value: value,
                "DataTableSetText": data_table_set,
                "DataTableSetString": data_table_set,
                "DataTableSetInt": data_table_set,
                "DataTableSetFixed": data_table_set,
                "DataTableSetBool": data_table_set,
                "DataTableValueRemove": data_table_remove,
                "DataTableGetText": data_table_get(None),
                "DataTableGetString": data_table_get(None),
                "DataTableGetInt": data_table_get(0),
                "DataTableGetFixed": data_table_get(0.0),
                "DataTableGetBool": data_table_get(False),
                "DataTableValueExists": data_table_value_exists,
                "GameIsTestMap": lambda *_: False,
                "TriggerDebugOutput": lambda *_: None,
                "TriggerCreate": lambda name: Trigger(name),
                "TriggerGetFunction": lambda trigger: trigger.name if isinstance(trigger, Trigger) else "",
                # NativeLib helpers (TriggerLibs/NativeLib.galaxy is not shipped
                # inside the map, so the two string codecs are reimplemented).
                "libNtve_gf_ConvertBooleanToString": lambda value: "true" if self.truthy(value) else "false",
                "libNtve_gf_ConvertStringToBoolean": lambda value: value == "true",
            }
        )


def _is_number(value: Any) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    if not isinstance(value, str):
        return False
    return bool(re.fullmatch(r"\s*[-+]?(?:\d+\.?\d*|\.\d+)\s*", value))
