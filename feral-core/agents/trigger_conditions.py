"""
FERAL manifest trigger conditions: parse and evaluate, never execute.
======================================================================

A skill manifest can declare ``triggers[].condition``, a string like::

    biometric.heart_rate_bpm > 160 && biometric.inferred_state == 'stressed'

Until this module existed, nothing in the tree ever read that string.
``skills/registry.py`` copied it into a cron payload and
``api/server.py`` dispatched the payload, so a ``JobType.TRIGGERED``
routine was created with cron_expr "every 1m" and ran its action
unconditionally, once a minute, forever. Two such routines on this
install accumulated 4,766 runs each since 2026-06-24, and one of them
was a Telegram *send* gated on the stress condition above. It stayed
quiet only because ``messaging_sms`` was never registered as a skill.

So this module has exactly one job: decide whether a condition holds,
right now, against a typed namespace. It has no execution path of any
kind. It imports no skill executor, no actuator, no messaging client,
and it must stay that way. The whole point of the 4,766-run incident
is that "fire the action" and "check whether the action should fire"
were never separated.

Design rules, all of them consequences of that incident:

1. **No eval/exec/ast.literal_eval on the condition text.** The
   condition is tokenized with a regex and parsed with a hand-written
   recursive-descent parser. There is no code path by which a
   condition string reaches a Python evaluator, so ``__import__(...)``
   and ``os.system('rm -rf ~')`` are not "sandboxed", they simply do
   not tokenize into anything the parser accepts.

2. **Unparseable means NOT satisfied, loudly.** A condition we cannot
   parse never falls back to "permissive". It returns
   ``satisfied=False`` and logs at WARNING, because a manifest whose
   gate silently evaporates is how you get an ungated action.

3. **Unknown means NOT satisfied.** Missing namespace fields evaluate
   to UNKNOWN under three-valued (Kleene) logic, and only a definite
   TRUE fires. On this install ``biometric.spo2_pct`` is absent
   whenever the last SpO2 sample is stale (the newest one is from
   2026-07-07), so a condition mentioning SpO2 must not fire on a
   guess about what the missing value would have been.

Grammar (everything else is a parse error):

    condition  := or_expr
    or_expr    := and_expr ( "||" and_expr )*
    and_expr   := primary ( "&&" primary )*
    primary    := "(" or_expr ")" | comparison
    comparison := operand OP operand
    OP         := "==" | "!=" | ">" | ">=" | "<" | "<="
    operand    := NUMBER | STRING | BOOL | "null" | REFERENCE
    NUMBER     := -?digits[.digits]
    STRING     := '...' or "..." with no backslashes
    BOOL       := "true" | "false"
    REFERENCE  := <namespace>.<field>, namespace in ALLOWED_NAMESPACES

Deliberately NOT supported, each because a permissive reading of it
would let a condition do something other than compare two values:
function calls, attribute chains deeper than ``namespace.field``,
arithmetic, indexing/slicing, ``not`` / ``in`` / ``is``, regex
matching, bare truthiness (``biometric.heart_rate_bpm`` alone is a
parse error, since coercing a value to a bool is exactly the kind of
silent pass this module exists to prevent), identifiers outside the
allowed namespaces, and any field or namespace segment starting with
an underscore.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Union

logger = logging.getLogger("feral.triggers")


# The only namespace roots a condition may reference. A reference to
# anything else (``os.system``, ``state.secrets``) is a parse error, not
# a lookup miss, so a typo cannot quietly become "unknown → never fires"
# and hide a broken manifest forever.
ALLOWED_NAMESPACES: frozenset[str] = frozenset({"biometric"})


class ConditionParseError(ValueError):
    """The condition text is not in the supported grammar.

    Never caught-and-ignored: callers must treat it as NOT satisfied and
    say so at WARNING.
    """


# ---------------------------------------------------------------------------
# Three-valued logic
# ---------------------------------------------------------------------------

class _Unknown:
    """Sentinel for "we do not know", distinct from False.

    A missing biometric field is not evidence that the comparison is
    false, it is the absence of evidence, and an autonomy primitive that
    treats those as the same thing is one that fires on absent data.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "UNKNOWN"

    def __bool__(self) -> bool:
        raise TypeError("UNKNOWN must not be coerced to bool")


UNKNOWN = _Unknown()

Tri = Union[bool, _Unknown]


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Literal:
    value: Any

    def render(self) -> str:
        if isinstance(self.value, str):
            return repr(self.value)
        if self.value is True:
            return "true"
        if self.value is False:
            return "false"
        if self.value is None:
            return "null"
        return str(self.value)


@dataclass(frozen=True)
class Reference:
    path: str  # always "<namespace>.<field>"

    def render(self) -> str:
        return self.path


Operand = Union[Literal, Reference]


@dataclass(frozen=True)
class Comparison:
    left: Operand
    op: str
    right: Operand


@dataclass(frozen=True)
class And:
    parts: tuple[Any, ...]


@dataclass(frozen=True)
class Or:
    parts: tuple[Any, ...]


Node = Union[Comparison, And, Or]


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

# Note the ordering: two-character operators must precede their
# one-character prefixes or ">=" tokenizes as ">" followed by garbage.
# Strings deliberately forbid backslashes; there is no escape handling
# to get wrong, and a condition that needs one is a condition we refuse
# rather than guess at.
_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<op>==|!=|>=|<=|>|<)
    | (?P<and>&&)
    | (?P<or>\|\|)
    | (?P<lparen>\()
    | (?P<rparen>\))
    | (?P<number>-?\d+(?:\.\d+)?)
    | (?P<string>'[^'\\]*'|"[^"\\]*")
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    pos: int


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    end = len(text)
    while pos < end:
        match = _TOKEN_RE.match(text, pos)
        if match is None:
            raise ConditionParseError(
                f"unexpected character {text[pos]!r} at position {pos}"
            )
        kind = match.lastgroup or ""
        if kind != "ws":
            tokens.append(_Token(kind, match.group(), pos))
        pos = match.end()
        if match.end() == match.start():  # pragma: no cover - regex safety net
            raise ConditionParseError(f"tokenizer stalled at position {pos}")
    return tokens


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, tokens: list[_Token], text: str):
        self._tokens = tokens
        self._text = text
        self._i = 0

    def parse(self) -> Node:
        node = self._parse_or()
        if self._i < len(self._tokens):
            tok = self._tokens[self._i]
            raise ConditionParseError(
                f"unexpected {tok.text!r} at position {tok.pos}"
            )
        return node

    def _peek(self) -> Optional[_Token]:
        return self._tokens[self._i] if self._i < len(self._tokens) else None

    def _take(self) -> _Token:
        tok = self._peek()
        if tok is None:
            raise ConditionParseError("unexpected end of condition")
        self._i += 1
        return tok

    def _parse_or(self) -> Node:
        parts = [self._parse_and()]
        while (tok := self._peek()) is not None and tok.kind == "or":
            self._take()
            parts.append(self._parse_and())
        return parts[0] if len(parts) == 1 else Or(tuple(parts))

    def _parse_and(self) -> Node:
        parts = [self._parse_primary()]
        while (tok := self._peek()) is not None and tok.kind == "and":
            self._take()
            parts.append(self._parse_primary())
        return parts[0] if len(parts) == 1 else And(tuple(parts))

    def _parse_primary(self) -> Node:
        tok = self._peek()
        if tok is None:
            raise ConditionParseError("unexpected end of condition")
        if tok.kind == "lparen":
            self._take()
            node = self._parse_or()
            closing = self._peek()
            if closing is None or closing.kind != "rparen":
                raise ConditionParseError(
                    f"unbalanced '(' opened at position {tok.pos}"
                )
            self._take()
            return node
        return self._parse_comparison()

    def _parse_comparison(self) -> Comparison:
        left = self._parse_operand()
        tok = self._peek()
        if tok is None or tok.kind != "op":
            # Bare operands are refused on purpose: truthiness coercion is
            # how a gate turns into "always fires" without anyone noticing.
            where = f"at position {tok.pos}" if tok else "at end of condition"
            raise ConditionParseError(
                f"expected a comparison operator {where}; bare values are not "
                "conditions (write an explicit comparison)"
            )
        self._take()
        right = self._parse_operand()
        return Comparison(left, tok.text, right)

    def _parse_operand(self) -> Operand:
        tok = self._take()
        if tok.kind == "number":
            text = tok.text
            return Literal(float(text) if "." in text else int(text))
        if tok.kind == "string":
            return Literal(tok.text[1:-1])
        if tok.kind == "ident":
            if tok.text == "true":
                return Literal(True)
            if tok.text == "false":
                return Literal(False)
            if tok.text == "null":
                return Literal(None)
            return Reference(_validated_reference(tok))
        raise ConditionParseError(
            f"expected a value at position {tok.pos}, found {tok.text!r}"
        )


def _validated_reference(tok: _Token) -> str:
    parts = tok.text.split(".")
    if len(parts) != 2:
        raise ConditionParseError(
            f"reference {tok.text!r} at position {tok.pos} must be exactly "
            "'<namespace>.<field>'"
        )
    namespace, field_name = parts
    if namespace not in ALLOWED_NAMESPACES:
        raise ConditionParseError(
            f"unknown namespace {namespace!r} at position {tok.pos}; "
            f"allowed: {sorted(ALLOWED_NAMESPACES)}"
        )
    if field_name.startswith("_") or namespace.startswith("_"):
        raise ConditionParseError(
            f"reference {tok.text!r} at position {tok.pos} uses a private name"
        )
    return tok.text


def parse_condition(text: str) -> Node:
    """Parse *text* into a comparison tree, or raise ConditionParseError."""
    if not isinstance(text, str) or not text.strip():
        raise ConditionParseError("condition is empty")
    return _Parser(_tokenize(text), text).parse()


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

@dataclass
class ConditionResult:
    """Outcome of one evaluation. ``satisfied`` is the only firing gate."""

    condition: str
    satisfied: bool
    parse_error: Optional[str] = None
    # Why the answer was UNKNOWN rather than True/False. Missing fields
    # are normal (a stale sensor); type errors are manifest bugs.
    missing: list[str] = field(default_factory=list)
    type_errors: list[str] = field(default_factory=list)
    # Every reference the tree touched, with the value it resolved to, so
    # a fired alert can say what it actually saw instead of asking the
    # operator to reconstruct it from logs.
    resolved: dict[str, Any] = field(default_factory=dict)

    @property
    def unknown(self) -> bool:
        return not self.satisfied and self.parse_error is None and bool(
            self.missing or self.type_errors
        )

    def describe(self) -> str:
        if self.parse_error:
            return f"unparseable: {self.parse_error}"
        if self.resolved:
            seen = ", ".join(f"{k}={v!r}" for k, v in sorted(self.resolved.items()))
        else:
            seen = "no fields resolved"
        if self.missing:
            seen += f"; missing: {', '.join(sorted(set(self.missing)))}"
        if self.type_errors:
            seen += f"; type errors: {'; '.join(self.type_errors)}"
        return seen


def _is_number(value: Any) -> bool:
    # bool is an int subclass in Python; treating True as 1 in a numeric
    # comparison is precisely the silent coercion this module refuses.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _compare(left: Any, op: str, right: Any, result: ConditionResult) -> Tri:
    if _is_number(left) and _is_number(right):
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        if op == "<":
            return left < right
        return left <= right

    same_kind = (
        (isinstance(left, str) and isinstance(right, str))
        or (isinstance(left, bool) and isinstance(right, bool))
        or (left is None and right is None)
    )
    if same_kind:
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        result.type_errors.append(
            f"operator {op!r} is not defined for {type(left).__name__} values"
        )
        return UNKNOWN

    # Mixed types. Returning False here would look like a clean "condition
    # did not hold" and hide a manifest that compares a string field to a
    # number forever, so this is UNKNOWN and shouted about instead.
    result.type_errors.append(
        f"cannot compare {type(left).__name__} with {type(right).__name__} "
        f"using {op!r}"
    )
    return UNKNOWN


def _resolve(operand: Operand, namespace: dict, result: ConditionResult) -> Any:
    if isinstance(operand, Literal):
        return operand.value
    value = namespace.get(operand.path, UNKNOWN)
    if value is UNKNOWN:
        result.missing.append(operand.path)
    else:
        result.resolved[operand.path] = value
    return value


def _eval_node(node: Node, namespace: dict, result: ConditionResult) -> Tri:
    if isinstance(node, Comparison):
        left = _resolve(node.left, namespace, result)
        right = _resolve(node.right, namespace, result)
        if left is UNKNOWN or right is UNKNOWN:
            return UNKNOWN
        return _compare(left, node.op, right, result)

    # Kleene logic. AND: one definite False decides it even when a sibling
    # is unknown; OR: one definite True decides it. Otherwise unknown wins
    # over a guess.
    if isinstance(node, And):
        saw_unknown = False
        for part in node.parts:
            value = _eval_node(part, namespace, result)
            if value is UNKNOWN:
                saw_unknown = True
            elif value is False:
                return False
        return UNKNOWN if saw_unknown else True

    if isinstance(node, Or):
        saw_unknown = False
        for part in node.parts:
            value = _eval_node(part, namespace, result)
            if value is UNKNOWN:
                saw_unknown = True
            elif value is True:
                return True
        return UNKNOWN if saw_unknown else False

    raise ConditionParseError(f"unsupported node {type(node).__name__}")


def evaluate_condition(
    text: str,
    namespace: dict,
    *,
    trigger_id: str = "",
) -> ConditionResult:
    """Evaluate *text* against *namespace*. Never raises.

    Returns a ConditionResult whose ``satisfied`` is True only when the
    tree evaluates to a definite True. Parse failures and type errors are
    logged at WARNING: a gate that cannot be evaluated is a gate that is
    not protecting anything, and the 4,766-run incident is what silence
    here costs.
    """
    label = trigger_id or "<anonymous>"
    result = ConditionResult(condition=text, satisfied=False)
    try:
        node = parse_condition(text)
    except ConditionParseError as exc:
        result.parse_error = str(exc)
        logger.warning(
            "Trigger %s condition is unparseable and will NOT fire: %r (%s)",
            label, text, exc,
        )
        return result

    value = _eval_node(node, namespace, result)
    if result.type_errors:
        logger.warning(
            "Trigger %s condition %r has type errors and will NOT fire: %s",
            label, text, "; ".join(result.type_errors),
        )
    elif result.missing:
        logger.debug(
            "Trigger %s condition %r is undecidable, missing %s",
            label, text, sorted(set(result.missing)),
        )
    result.satisfied = value is True
    return result


# ---------------------------------------------------------------------------
# The biometric namespace
# ---------------------------------------------------------------------------

# Only fields that genuinely exist on this install are published. There is
# no hrv and no sleep metric here: ~/.feral/baselines.db has 1,286 `hr`
# samples and 149 `spo2` samples from jw_health_glasses, plus `steps`, and
# nothing else. Publishing `biometric.hrv_ms` because other products have
# one would give manifest authors a field that is permanently missing, and
# a permanently-undecidable condition is worse than an absent one.
BIOMETRIC_FIELDS: dict[str, str] = {
    "biometric.heart_rate_bpm":
        "Latest heart rate in bpm from a live (non cloud-mirror) wearable, "
        "only when the sample is fresher than the freshness window.",
    "biometric.heart_rate_age_s":
        "Age in seconds of the heart-rate sample above.",
    "biometric.heart_rate_source":
        "Sensor that produced it, e.g. 'jw_health_glasses'.",
    "biometric.heart_rate_baseline_mean":
        "Rolling baseline mean for this source's resting HR, when the "
        "baseline engine has at least 3 observations.",
    "biometric.heart_rate_deviation_sigma":
        "How many standard deviations the current HR sits from that "
        "baseline. Computed read-only; it does NOT persist an alert.",
    "biometric.spo2_pct":
        "Latest SpO2 percentage, same freshness and live-source rules. "
        "Absent on this install: the newest sample is from 2026-07-07.",
    "biometric.spo2_age_s": "Age in seconds of the SpO2 sample above.",
    "biometric.spo2_source": "Sensor that produced the SpO2 sample.",
    "biometric.skin_temperature_c":
        "Skin temperature in Celsius, when the sensor reports a non-zero "
        "value.",
    "biometric.activity_state":
        "Fused activity state. models/protocol.py documents the domain as "
        "resting, walking, running, stressed.",
    "biometric.inferred_state":
        "Same value as biometric.activity_state, not a second inference. "
        "The sensor wire protocol calls this field `inferred_state` "
        "(models/protocol.py:80) and perception/fusion.py:516 stores it as "
        "`frame.activity_state`, so both shipped manifests, which were "
        "written against the wire name, resolve correctly.",
}


def describe_namespace() -> dict[str, str]:
    """Field → meaning, for manifest authors and the /triggers surface."""
    return dict(BIOMETRIC_FIELDS)


def build_biometric_namespace(
    frames: Optional[list] = None,
    baseline_engine: Any = None,
    now: Optional[float] = None,
    fresh_window_s: float = 120.0,
) -> dict[str, Any]:
    """Build the ``biometric.*`` namespace from real sensor state.

    Freshness and source rules are deliberately identical to the ones the
    ProactiveEngine's hardcoded health triggers already enforce (operator
    reports 2026-05-09 and 2026-06-08): a reading only counts when its
    ``*_sample_ts`` is inside *fresh_window_s* AND its source is not a
    cloud mirror, because Apple HealthKit relabels an hours-old workout
    sample's endDate to "now" and that fired hr_elevated at a resting 60
    bpm. A field that fails those checks is OMITTED rather than zeroed:
    zero would compare as a real number.
    """
    now = time.time() if now is None else now
    ns: dict[str, Any] = {}

    try:
        from perception.fusion import _is_lagging_source
    except Exception:  # pragma: no cover - perception is always importable here
        def _is_lagging_source(_source: str) -> bool:
            return False

    def _age(frame: Any, attr: str) -> float:
        ts = float(getattr(frame, attr, 0.0) or 0.0)
        return (now - ts) if ts > 0 else float("inf")

    best_hr = None
    best_hr_age = float("inf")
    best_spo2 = None
    best_spo2_age = float("inf")

    for frame in frames or []:
        hr = getattr(frame, "heart_rate", 0) or 0
        hr_age = _age(frame, "heart_rate_sample_ts")
        hr_source = getattr(frame, "heart_rate_source", "") or ""
        if hr > 0 and hr_age <= fresh_window_s and not _is_lagging_source(hr_source):
            if hr_age < best_hr_age:
                best_hr, best_hr_age = frame, hr_age

        spo2 = getattr(frame, "spo2_pct", 0) or 0
        spo2_age = _age(frame, "spo2_sample_ts")
        spo2_source = getattr(frame, "spo2_source", "") or ""
        if spo2 > 0 and spo2_age <= fresh_window_s and not _is_lagging_source(spo2_source):
            if spo2_age < best_spo2_age:
                best_spo2, best_spo2_age = frame, spo2_age

    if best_hr is not None:
        ns["biometric.heart_rate_bpm"] = int(best_hr.heart_rate)
        ns["biometric.heart_rate_age_s"] = round(best_hr_age, 1)
        source = (getattr(best_hr, "heart_rate_source", "") or "").strip().lower()
        if source:
            ns["biometric.heart_rate_source"] = source
        _attach_hr_baseline(ns, baseline_engine, int(best_hr.heart_rate), source)

    if best_spo2 is not None:
        ns["biometric.spo2_pct"] = int(best_spo2.spo2_pct)
        ns["biometric.spo2_age_s"] = round(best_spo2_age, 1)
        source = (getattr(best_spo2, "spo2_source", "") or "").strip().lower()
        if source:
            ns["biometric.spo2_source"] = source

    # Activity state comes from the freshest frame that has one; "unknown"
    # is the fusion layer's way of saying it has no idea, so it is omitted
    # rather than published as a comparable string.
    for frame in sorted(
        (f for f in (frames or [])),
        key=lambda f: float(getattr(f, "timestamp", 0.0) or 0.0),
        reverse=True,
    ):
        state = (getattr(frame, "activity_state", "") or "").strip().lower()
        if state and state != "unknown":
            ns["biometric.activity_state"] = state
            ns["biometric.inferred_state"] = state
            break

    for frame in frames or []:
        temp = float(getattr(frame, "skin_temperature_c", 0.0) or 0.0)
        if temp > 0:
            ns["biometric.skin_temperature_c"] = temp
            break

    return ns


def _attach_hr_baseline(
    ns: dict[str, Any],
    baseline_engine: Any,
    heart_rate: int,
    source: str,
) -> None:
    """Add read-only baseline stats for the current HR, if any exist.

    Uses ``get_baseline`` and computes the deviation here. It must NOT
    call ``check_anomaly``: that method persists a baseline_alert row and
    fans out to the IdeasEngine listeners, and a namespace builder that
    runs every 15s and writes rows is the 78-duplicate-alert bug from
    operator report 2026-06-07 wearing a new hat.

    Metric id preference mirrors the proactive engine's Fix #5: the
    per-source baseline (``hr_resting:jw_health_glasses``) before the
    legacy bare ``hr_resting``, so two wearables keep separate means.
    """
    if baseline_engine is None:
        return
    candidates = ([f"hr_resting:{source}"] if source else []) + ["hr_resting"]
    for metric_id in candidates:
        try:
            metric = baseline_engine.get_baseline(metric_id)
        except Exception as exc:
            logger.debug("baseline lookup failed for %s: %s", metric_id, exc)
            continue
        if metric is None or len(getattr(metric, "values", []) or []) < 3:
            continue
        ns["biometric.heart_rate_baseline_mean"] = round(float(metric.mean), 2)
        std = float(getattr(metric, "std_dev", 0.0) or 0.0)
        if std > 0:
            ns["biometric.heart_rate_deviation_sigma"] = round(
                abs(heart_rate - float(metric.mean)) / std, 2
            )
        return
