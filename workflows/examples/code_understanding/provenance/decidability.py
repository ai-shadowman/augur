"""Which questions about code a parser can settle, per language.

``assets/sdghub/code_generation/prompts/data_from_code.yaml`` used to ask a
model for six things about every method and one about every file. Some of them
a parser answers exactly, some it answers partly, and some are not decidable
from one file by anything. The epic's operating rule turns on that
distinction: name the deterministic thing that could answer the same question,
and if one exists, use it and delete the prompt. The prompt now asks for the
one field that survived, and this table is the record of why the other six did
not.

That argument kept getting made in prose, in a commit message or a planning
file, where it cannot be checked and has to be re-derived every time someone
proposes a schema change. This module is the same argument as a table a test
can import. A verdict here is a claim about the language, not about this
codebase's current parsers, which is why :data:`PARSEABLE` and :data:`PARSED`
are separate: Java's ``throws`` clause is decidable today and unparsed today,
and conflating the two would read as an argument for asking a model.

The table is deliberately short. It records the five languages there is
grounded reason to make a claim about, and returns :data:`UNRECORDED` for
everything else, so an unassessed pair cannot be mistaken for a permissive
one. :mod:`provenance.languages` takes the same shape for the same reason: it
names the two languages whose paths the key folding handles out of the 28 the
mapping lists, rather than guessing at the other 26.

Four of the five have a registered dialect in :mod:`provenance.dialects`, and
every count a reason cites comes from the checkout
``tests/test_provenance_dialects.py`` names and re-measures on each run. A
number here that drifts from one there is a row that stopped being true.
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

#: The questions ``data_from_code.yaml`` currently puts to a model, which since
#: Phase 5 is one. Everything else in :data:`QUESTIONS` is a field the prompt
#: no longer asks about: the five the header stitches in, :data:`EXCEPTIONS`,
#: which Phase 4 deleted, and the rest of the ``methods[]`` object, which
#: Phase 5 deleted on the strength of the same rows. The table keeps assessing
#: all of them on purpose, so that re-adding one has to argue past a verdict
#: rather than past an absence.
#:
#: :func:`model_justified` is what shrank this tuple, not an opinion about it:
#: of the seven fields the prompt used to ask, ``purpose`` is the only one it
#: returns True for in any language the table records.
#:
#: Being justified is not being worth asking. Since 2026-10-24 the prompt runs
#: only when ``AUGUR_STAGE_ONE_PURPOSE`` is set, because Arm A measured the
#: graph no better for the answer; see ``provenance/arms.py``. This tuple
#: still pins what the prompt asks when it does run.
PROMPTED = (PURPOSE,)


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
        "import statements are syntax. ast and tree-sitter return the same 621 "
        "names on the 187 celery files both read",
    ),
    CLASSES: (
        PARSED,
        "provenance.symbols.symbols_in",
        "a class statement is syntax. The two parsers return the same 371 "
        "classes on the 187 celery files both read, and tree-sitter reads the "
        "other 50; see tests/test_provenance_treesitter_agreement.py",
    ),
    FUNCTIONS: (
        PARSED,
        "provenance.symbols.symbols_in",
        "the same 226 module-level functions under both parsers, including the "
        "rule that a closure's locals are not addressable and are not emitted",
    ),
    METHODS: (
        PARSED,
        "provenance.symbols.symbols_in",
        "the same 1,661 qualified method names under both parsers. What a class "
        "declares is in the file; what it inherits is a different question and "
        "is not claimed here",
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
        PARSED,
        "provenance.dialects.java.package_of",
        "declared in the file, so it is better evidence than the path "
        "heuristic Python needs, and the gap is not hypothetical: the "
        "declared name differs from the folded path on all 629 commons-lang "
        "files, because Maven puts the tree under src/main/java",
    ),
    IMPORTS: (
        PARSED,
        "provenance.imports.imports_in",
        "3,569 names off 629 files, and unlike Python's they are all at the "
        "top of the file and fully qualified, so there is no relative form to "
        "resolve. What the name does not give is the artifact: "
        "org.apache.commons.lang3 ships in commons-lang3 and "
        "org.apache.commons.io in commons-io, which is a lookup rather than a "
        "transform and is not claimed here",
    ),
    CLASSES: (
        PARSED,
        "provenance.symbols.symbols_in",
        "1,139 named types off 629 files. The section holds every named type "
        "rather than only the class keyword: an interface, an enum, a record "
        "and an @interface each declare a name and hold methods, the header "
        "offers four buckets and this is the closest, and omitting them would "
        "drop most of what a Java repository declares",
    ),
    FUNCTIONS: (
        NOT_APPLICABLE,
        "",
        "Java has no free functions. Every method belongs to a type, so a "
        "Functions: section on a Java file would be empty or invented, and "
        "the parser agrees: zero across all 629 commons-lang files",
    ),
    METHODS: (
        PARSED,
        "provenance.symbols.symbols_in",
        "9,160 qualified names off 629 files, constructors among them, since "
        "a constructor is part of the API the inventory vouches for. Always "
        "nested in a type, so the qualifier the header writes arrives from "
        "the grammar rather than being reconstructed from indentation",
    ),
}

#: TypeScript is the language where the same question has two answers
#: depending on which half of the file you ask. A named declaration is close
#: to Java; an inline arrow function is close to untyped JavaScript. Rows that
#: turn on that split cite both populations rather than averaging them, since
#: the average describes neither.
_TYPESCRIPT = {
    PURPOSE: (
        UNDECIDABLE,
        "",
        "a TSDoc block and a decorator argument both state something an "
        "author chose to write down, which is a claim about the code sitting "
        "next to the code rather than the thing itself",
    ),
    METHOD_CODE: (
        PARSEABLE,
        "a tree-sitter method_definition or arrow_function node's byte range",
        "on disk already, and the inline forms make it worse to ask for: an "
        "arrow function body is frequently one expression, so a model has "
        "every opportunity to return something that looks right",
    ),
    CALLERS: (
        NOT_IN_FILE,
        "",
        "cross-file, and the module graph is not even fixed by the source: "
        "path mapping in tsconfig, barrel re-exports and dynamic import() "
        "all decide at build or run time which file a specifier reaches",
    ),
    PARAMETERS: (
        PARTIAL,
        "the formal parameter list, which carries a type where one is written",
        "the names are exact everywhere and the types are not: 92.8% of the "
        "6,097 parameters on nest's named declarations are typed, against "
        "41.8% of the 3,386 on its arrow functions. Both numbers are rates "
        "rather than guarantees, so the field cannot be treated as settled "
        "the way Java's can",
    ),
    TEMPLATE_PARAMETERS: (
        PARSEABLE,
        "the type_parameters node on the declaration",
        "generics are written in the signature, 552 of them in nest, and the "
        "constraint and default clauses are in the same node",
    ),
    ANNOTATIONS: (
        PARSEABLE,
        "decorator nodes on the declaration",
        "a TypeScript decorator is the form the prompt's 'annotations' names, "
        "and nest leans on them: 3,566 occurrences, which is how the "
        "framework spells almost every structural fact about itself",
    ),
    RETURN_VALUE: (
        PARTIAL,
        "the declared return type, where the signature carries one",
        "declaring it is optional and inference covers the rest, so the "
        "habit splits: 42.7% of nest's 5,296 named declarations annotate a "
        "return, against 0.6% of its 16,161 arrow functions. Recovering the "
        "inferred type needs the type checker and the whole program, which is "
        "neither this parser nor a model shown one file",
    ),
    EXCEPTIONS: (
        NOT_IN_FILE,
        "",
        "there is no throws clause to read and the type system deliberately "
        "declines to track it. A throw may carry any value rather than an "
        "Error, and a rejected promise is a throw at an await somewhere else "
        "entirely, so the lexical throws in a body are a subset twice over",
    ),
    PACKAGE: (
        PARSED,
        "provenance.declarations.module_key",
        "the path is the module's identity here rather than a convention "
        "about it: an import specifier resolves to a file, so the folded path "
        "is the name the module system itself uses",
    ),
    IMPORTS: (
        PARSED,
        "provenance.imports.imports_in",
        "2,946 names off 2,141 files across the four forms a specifier takes: "
        "import, export-from, dynamic import() and require(). The last two "
        "are call expressions rather than statements, which is why matching "
        "only the import keyword would have missed 50 of them in nest",
    ),
    CLASSES: (
        PARSED,
        "provenance.symbols.symbols_in",
        "2,054 off 2,141 files, counting interfaces, enums and type aliases "
        "for the reason Java's interfaces are counted. A type alias earns its "
        "place twice over, since it is frequently the only declaration a "
        "types-only module makes",
    ),
    FUNCTIONS: (
        PARSED,
        "provenance.symbols.symbols_in",
        "552 top-level declarations off 2,141 files, which is the structural "
        "difference from Java rather than a difference of degree: the "
        "language has free functions and a header that omitted the section "
        "would be lying about the file",
    ),
    METHODS: (
        PARSED,
        "provenance.symbols.symbols_in",
        "3,654 qualified names off 2,141 files. Overload signatures repeat a "
        "name the implementation also declares and are collapsed onto it, so "
        "the count is of methods rather than of declarations",
    ),
}

#: Ruby is the one here with no type syntax at all, and the rows say so once
#: rather than five times. Where Python's annotations are optional and
#: TypeScript's are usual, Ruby's live in a separate .rbs file or nowhere;
#: jekyll ships neither an .rbs nor a Sorbet signature, so there is nothing in
#: the files this parser reads for a type question to be answered from.
_RUBY = {
    PURPOSE: (
        UNDECIDABLE,
        "",
        "the one row where a reader beats a parser, and Ruby makes the gap "
        "wider than most: a block-heavy DSL reads as English and compiles to "
        "method calls, so what the syntax says and what the file is for are "
        "further apart than usual",
    ),
    METHOD_CODE: (
        PARSEABLE,
        "a tree-sitter method or singleton_method node's byte range",
        "bytes between def and end, delimited by the grammar. The echo "
        "argument holds whatever the language: a slice taken is a slice, and "
        "a slice asked for is a reconstruction",
    ),
    CALLERS: (
        NOT_IN_FILE,
        "",
        "cross-file, and Ruby keeps the door open after load: method_missing, "
        "send, define_method and reopening a class all create call sites that "
        "no amount of reading the whole program would have shown",
    ),
    PARAMETERS: (
        PARTIAL,
        "the method_parameters node: names, defaults, splat, block and keyword forms",
        "every form a signature can take is syntax and is read exactly. The "
        "prompt also asks for types, and a .rb file carries none, so there "
        "the undecidable remainder is the whole of the type question rather "
        "than the thin edge of it",
    ),
    TEMPLATE_PARAMETERS: (
        NOT_APPLICABLE,
        "",
        "Ruby has no generics. A collection is not parameterised by a type, "
        "so there is no type parameter to be right or wrong about",
    ),
    ANNOTATIONS: (
        NOT_APPLICABLE,
        "",
        "no decorator syntax. The nearest thing is a class-level method call "
        "such as validates or before_action, which is an ordinary call in the "
        "body and is already an entry in Methods: wherever it is defined",
    ),
    RETURN_VALUE: (
        NOT_IN_FILE,
        "",
        "nothing declares one, and the implicit return makes the answer the "
        "last expression evaluated, which is usually a call into another "
        "file. Reading this file gives the shape of the final expression and "
        "not what it evaluates to, so an answer here is a confident guess "
        "about code the model was not shown",
    ),
    EXCEPTIONS: (
        NOT_IN_FILE,
        "",
        "raise takes any object that responds to exception, the class is "
        "frequently built at the raise site, and rescue matches by ancestry "
        "at run time. The transitive problem Python has, with a dynamic class "
        "on top of it",
    ),
    PACKAGE: (
        PARSED,
        "provenance.declarations.module_key",
        "the folded path is the document's address and is what the header "
        "states. The constant path a file declares, Jekyll::Site, is a "
        "different fact that need not match the directory at all, and it is "
        "already carried in Classes: rather than being asserted here",
    ),
    IMPORTS: (
        PARSED,
        "provenance.imports.imports_in",
        "135 names off 160 files, from require, load and autoload. All three "
        "are ordinary method calls rather than statements, so the method name "
        "is what is matched; require_relative is excluded on purpose, since "
        "it names a path in this repository and not a dependency",
    ),
    CLASSES: (
        PARSED,
        "provenance.symbols.symbols_in",
        "330 off 160 files, modules included. A module both namespaces and "
        "mixes in, so it is the nearest of the four buckets, and leaving it "
        "out would make Jekyll itself absent from a repository where it "
        "encloses nearly everything",
    ),
    FUNCTIONS: (
        PARSED,
        "provenance.symbols.symbols_in",
        "32 off 160 files. A def at the top level is a private method on "
        "Object rather than a free function in the C sense, and it is "
        "addressable by bare name from anywhere that loads the file, which is "
        "what the section is for",
    ),
    METHODS: (
        PARSED,
        "provenance.symbols.symbols_in",
        "1,027 qualified names off 160 files, including the bodies of "
        "class << self, which the walk reaches by declining to treat a "
        "singleton class as a named scope. What define_method builds at load "
        "time is not here and is not claimed",
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
    "typescript": _TYPESCRIPT,
    "ruby": _RUBY,
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
