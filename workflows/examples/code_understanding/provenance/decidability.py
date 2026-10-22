"""Which questions about code a parser can settle, per language.

``assets/sdghub/code_generation/prompts/data_from_code.yaml`` asks a model for
six things about every method and one about every file. Some of them a parser
answers exactly, some it answers partly, and some are not decidable from one
file by anything. The epic's operating rule turns on that distinction: name the
deterministic thing that could answer the same question, and if one exists, use
it and delete the prompt.

That argument kept getting made in prose, in a commit message or a planning
file, where it cannot be checked and has to be re-derived every time someone
proposes a schema change. This module is the same argument as a table a test
can import. A verdict here is a claim about the language, not about this
codebase's current parsers, which is why :data:`PARSEABLE` and :data:`PARSED`
are separate: Java's ``throws`` clause is decidable today and unparsed today,
and conflating the two would read as an argument for asking a model.

The table is deliberately short. It records the three languages there is
grounded reason to make a claim about, and returns :data:`UNRECORDED` for
everything else, so an unassessed pair cannot be mistaken for a permissive
one. :mod:`provenance.languages` takes the same shape for the same reason: it
names the two languages whose paths the key folding handles out of the 28 the
mapping lists, rather than guessing at the other 26.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------

#: A parser in this repository answers it today. The answer is exact and no
#: model should be asked for it.
PARSED = "parsed"

#: Decidable from the syntax, and nothing here parses it yet. The distinction
#: from :data:`PARSED` is a gap in this codebase, not a reason to call a model.
PARSEABLE = "parseable"

#: A parser settles a well-defined part and the remainder is not decidable.
#: ``settled_by`` names the decidable part. A prompt for one of these should
#: be narrowed to the remainder with the parsed part stitched in, rather than
#: asking for the whole field and hoping the model reproduces the easy half.
PARTIAL = "partial"

#: The answer is present in the file but not mechanically extractable from it.
#: A reader can settle it and a parser cannot, which is the one shape where
#: the operating rule's "unless AI can do the job better" actually holds.
UNDECIDABLE = "undecidable"

#: The fact is not in the file at all. It is cross-file, transitive, or
#: decided at runtime, so a model shown this file is working from a subset of
#: the program and cannot know it is a subset. The honest answer is not a
#: better model but a deleted field: an answer drawn from partial evidence
#: arrives with the same confidence as a complete one, which is how the
#: celery corpus acquired 102 misattributed symbols.
NOT_IN_FILE = "not-in-file"

#: The language has no such construct, so the question has no answer to get
#: right or wrong. A prompt that asks anyway invites a plausible fabrication.
NOT_APPLICABLE = "not-applicable"

#: No claim has been assessed for this pair. Returned rather than raised so a
#: caller can enumerate the table, and refused by :func:`model_justified` so it
#: cannot quietly license an inference call.
UNRECORDED = "unrecorded"

# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------

#: The fields ``data_from_code.yaml`` puts to a model, spelled as the prompt
#: spells them except ``return value``, which is not an identifier.
#: :data:`EXCEPTIONS` stays after Phase 4 deleted it from the prompt, so that
#: the verdict against it outlives the field.
PURPOSE = "purpose"
METHOD_CODE = "code"
CALLERS = "callers"
PARAMETERS = "parameters"
TEMPLATE_PARAMETERS = "template_parameters"
ANNOTATIONS = "annotations"
RETURN_VALUE = "return_value"
EXCEPTIONS = "exceptions"

#: The header fields Phase 3 moved to parsers. Here so the table shows what
#: deciding a question actually bought, rather than only what is still open.
PACKAGE = "package"
IMPORTS = "imports"
CLASSES = "classes"
FUNCTIONS = "functions"
METHODS = "methods"

QUESTIONS = (
    PURPOSE,
    METHOD_CODE,
    CALLERS,
    PARAMETERS,
    TEMPLATE_PARAMETERS,
    ANNOTATIONS,
    RETURN_VALUE,
    EXCEPTIONS,
    PACKAGE,
    IMPORTS,
    CLASSES,
    FUNCTIONS,
    METHODS,
)

#: The questions ``data_from_code.yaml`` currently puts to a model. The rest of
#: :data:`QUESTIONS` are fields the prompt no longer asks about: the five the
#: header now stitches in, and :data:`EXCEPTIONS`, which Phase 4 deleted on the
#: strength of the rows below. The table keeps assessing a deleted field on
#: purpose, so that re-adding it has to argue past a verdict rather than past
#: an absence.
PROMPTED = (
    PURPOSE,
    METHOD_CODE,
    CALLERS,
    PARAMETERS,
    TEMPLATE_PARAMETERS,
    ANNOTATIONS,
    RETURN_VALUE,
)


@dataclass(frozen=True)
class Entry:
    """One cell: what can be known about ``question`` in ``language``."""

    language: str
    question: str
    verdict: str
    reason: str
    #: What settles it, or settles the decidable part of it. Empty when
    #: nothing does, which is every :data:`UNDECIDABLE` and
    #: :data:`NOT_APPLICABLE` row.
    settled_by: str = ""


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------

# (verdict, settled_by, reason), keyed by language then question. Reasons are
# the argument, not a restatement of the verdict; a row whose reason could be
# swapped with another row's is not pulling its weight.
_PYTHON = {
    PURPOSE: (
        UNDECIDABLE,
        "",
        "intent is not in the syntax. ast.get_docstring settles what the file "
        "says about itself, exactly, which is a different question and a "
        "deterministic input to hand a model rather than one to ask it for",
    ),
    METHOD_CODE: (
        PARSEABLE,
        "ast node line range, or ast.get_source_segment",
        "the source of a method is a slice of a file already on disk. Asking "
        "a model to return it is the echo failure: a value stitched in comes "
        "back whole every time and a value asked for does not",
    ),
    CALLERS: (
        NOT_IN_FILE,
        "",
        "the question is cross-file and the model is shown one file, so every "
        "answer is drawn from a subset of the program. Python's dispatch is "
        "dynamic besides: getattr, decorators and the registry rebinding "
        "celery itself does all move a call site out of the syntax",
    ),
    PARAMETERS: (
        PARTIAL,
        "ast.arguments: names, defaults, *args, **kwargs, and any annotation",
        "the signature is exact. The prompt asks for types as well, and a "
        "type is only present where someone annotated it; celery 2.5.5 "
        "annotates nothing, so there the undecidable part is all of it",
    ),
    TEMPLATE_PARAMETERS: (
        PARSEABLE,
        "ast type_params on 3.12 and later, empty before it",
        "PEP 695 type parameters are syntax where they exist and absent where "
        "they do not. Either way a parser is right and a model can only guess "
        "at what the generic was meant to range over",
    ),
    ANNOTATIONS: (
        PARSEABLE,
        "ast decorator_list",
        "decorators are the Python form the prompt's 'annotations' names, and "
        "they are attached to the node",
    ),
    RETURN_VALUE: (
        PARTIAL,
        "ast returns, the return annotation",
        "the annotation is exact where present. A description of what the "
        "value means is not in the source, and inferring the type where "
        "nothing declared it is inference, not parsing",
    ),
    EXCEPTIONS: (
        NOT_IN_FILE,
        "",
        "Python has no throws clause, so the raise set of a method is "
        "transitive through every callee and terminates in C extensions with "
        "no Python source to read. The raise statements lexically inside the "
        "body are a subset, and a model reading one file returns that subset "
        "at best while presenting it as the answer",
    ),
    PACKAGE: (
        PARSED,
        "provenance.declarations.module_key",
        "the dotted module path is a function of the file path",
    ),
    IMPORTS: (
        PARSED,
        "provenance.imports.imports_in",
        "import statements are syntax. Measured 0.992 precision even on the "
        "line-scan fallback",
    ),
    CLASSES: (
        PARSED,
        "provenance.symbols.symbols_in",
        "exact under ast, and emitted on the line-scan fallback too at 0.997 "
        "precision; see provenance.symbols.SCAN_EMITS",
    ),
    FUNCTIONS: (
        PARSED,
        "provenance.symbols.symbols_in",
        "exact under ast, and emitted on the line-scan fallback too at 0.995",
    ),
    METHODS: (
        PARSED,
        "provenance.symbols.symbols_in",
        "exact under ast, and emitted on the line-scan fallback too at 0.999",
    ),
}

_JAVA = {
    PURPOSE: (
        UNDECIDABLE,
        "",
        "as in every language here. A Javadoc comment is evidence of intent "
        "and not the same fact",
    ),
    METHOD_CODE: (
        PARSEABLE,
        "a tree-sitter method_declaration node's byte range",
        "a slice of a file on disk, the same echo argument as Python",
    ),
    CALLERS: (
        NOT_IN_FILE,
        "",
        "cross-file, and reflection, dynamic proxies and dependency injection "
        "put real call sites outside the syntax even with the whole program",
    ),
    PARAMETERS: (
        PARTIAL,
        "the formal parameter list, which carries declared types",
        "better than Python: the type is mandatory, so names and types are "
        "both exact. The prompt's 'description' is still not in the source",
    ),
    TEMPLATE_PARAMETERS: (
        PARSEABLE,
        "the type parameter list on the method or its enclosing type",
        "generics are declared in the signature",
    ),
    ANNOTATIONS: (
        PARSEABLE,
        "annotation nodes on the declaration",
        "Java annotations are syntax attached to the declaration",
    ),
    RETURN_VALUE: (
        PARTIAL,
        "the declared return type",
        "the type is mandatory and exact; what the value means is not stated",
    ),
    EXCEPTIONS: (
        PARTIAL,
        "the throws clause",
        "the case that makes this question look decidable in general, and it "
        "is not. Checked exceptions are declared, so a parser reads them off "
        "the signature exactly. Unchecked ones are not declared and are as "
        "transitive as Python's, so 'exceptions thrown' stays open while "
        "'checked exceptions declared' is closed. Narrowing the field to the "
        "decidable half is the whole gain",
    ),
    PACKAGE: (
        PARSEABLE,
        "the package declaration",
        "declared in the file, so it is better evidence than the path "
        "heuristic Python needs; the two disagree whenever the directory "
        "layout is wrong",
    ),
    IMPORTS: (
        PARSEABLE,
        "import declarations",
        "syntax, and unlike Python's they are all at the top of the file and "
        "fully qualified, so there is no relative form to resolve",
    ),
    CLASSES: (
        PARSEABLE,
        "type declaration nodes",
        "syntax, and the file name constrains it further: a public type has "
        "to match the file it is in. Phase 6 lands the parser",
    ),
    FUNCTIONS: (
        NOT_APPLICABLE,
        "",
        "Java has no free functions. Every method belongs to a type, so a "
        "Functions: section on a Java file would be empty or invented",
    ),
    METHODS: (
        PARSEABLE,
        "method declaration nodes",
        "syntax, and always nested in a type, so the qualifier the header "
        "writes arrives for free rather than being reconstructed from "
        "indentation. Phase 6 lands the parser",
    ),
}

#: Ansible is in the table because it is the other language
#: :data:`provenance.languages.FOLDED_LANGUAGES` names, and because it is the
#: clearest case of a prompt asking questions a language cannot answer. A
#: playbook declares plays, tasks and roles. It declares no methods at all, so
#: every ``methods[]`` field the prompt asks for has no referent.
_ANSIBLE_NO_METHODS = (
    "a playbook declares plays, tasks and roles, not methods, so this field "
    "has no referent to be right or wrong about"
)
_ANSIBLE = {
    PURPOSE: (
        UNDECIDABLE,
        "",
        "a play's name field states what its author called it, which is "
        "evidence and not the fact the prompt asks for",
    ),
    **dict.fromkeys(
        (
            METHOD_CODE,
            CALLERS,
            PARAMETERS,
            TEMPLATE_PARAMETERS,
            ANNOTATIONS,
            RETURN_VALUE,
            EXCEPTIONS,
            CLASSES,
            FUNCTIONS,
            METHODS,
        ),
        (NOT_APPLICABLE, "", _ANSIBLE_NO_METHODS),
    ),
    # package and imports are deliberately absent. Roles and include_tasks are
    # a real dependency edge and nothing here has looked at them, so the table
    # says unrecorded rather than guessing.
}

TABLE: dict[str, dict[str, tuple[str, str, str]]] = {
    "python": _PYTHON,
    "java": _JAVA,
    "ansible": _ANSIBLE,
}

#: The languages with a recorded claim. Every other language the mapping names
#: comes back :data:`UNRECORDED`, which is a statement about this table rather
#: than about the language.
LANGUAGES = tuple(sorted(TABLE))


def decide(language: str, question: str) -> Entry:
    """What can be known about ``question`` in ``language``.

    Unknown pairs come back :data:`UNRECORDED` rather than raising, so a
    caller can walk every language the mapping names and get an answer for
    each. Raise on an unknown *question*, though: that is a typo or a schema
    change, and silently reporting it unassessed would hide both.
    """
    if question not in QUESTIONS:
        raise KeyError(
            f"{question!r} is not a question this table records; expected one of "
            f"{', '.join(QUESTIONS)}"
        )

    folded = language.strip().lower()
    row = TABLE.get(folded, {}).get(question)
    if row is None:
        return Entry(
            language=folded,
            question=question,
            verdict=UNRECORDED,
            reason=f"no claim has been assessed for {question!r} in {folded!r}",
        )

    verdict, settled_by, reason = row
    return Entry(
        language=folded,
        question=question,
        verdict=verdict,
        reason=reason,
        settled_by=settled_by,
    )


def model_justified(language: str, question: str) -> bool:
    """Whether asking a model this is defensible, by the operating rule.

    True for :data:`UNDECIDABLE` alone: the answer is in the file and no
    parser gets it out, which is the only shape where a model does the job
    better than something we already have.

    The four falses are each false for their own reason, and collapsing them
    would lose the reason. :data:`PARSED` and :data:`PARSEABLE` have a
    deterministic answer, so asking is the echo failure. :data:`NOT_IN_FILE`
    has no answer the file supports, so asking buys a confident subset.
    :data:`NOT_APPLICABLE` has no answer at all. :data:`PARTIAL` is false
    because the field as written is the wrong unit: narrow it, stitch the
    decidable half in, and re-ask this about whatever genuinely remains.

    :data:`UNRECORDED` raises. "We never looked" is the one answer that must
    not read as permission, since that is how a prompt field survives a review
    it was never actually given.
    """
    entry = decide(language, question)
    if entry.verdict == UNRECORDED:
        raise LookupError(
            f"decidability of {question!r} in {language!r} has not been assessed, so "
            f"whether a model is justified cannot be answered; add a row to "
            f"provenance.decidability.TABLE"
        )
    return entry.verdict == UNDECIDABLE
