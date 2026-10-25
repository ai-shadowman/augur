"""Java: resolving tree-sitter's captures into edges, with no oracle.

There is no second Java reader to hold this to, so
``tests/test_provenance_edges_java.py`` is constructed cases and a regression
pin on a named checkout, and says so. What makes resolution tractable is that
Java's scoping is static and the language states its precedence (JLS 6.4.1):
a type declared in an enclosing scope shadows a single-type or single-static
import, which shadows a type in the same package, which shadows an on-demand
import, ``java.lang`` included.

**Evidence.** Every class the checkout declares is indexed by its
fully-qualified name in real case (Java is case-sensitive, and folded, the
package ``org.ex.util`` and the class ``org.ex.Util`` are one key), read from
the declarations rather than file stems, so a secondary top-level type and a
member type are both found. A candidate counts only if the referring file
could compile against it: the same source tree, or ``src/main/java`` seen
from the ``src/test/java`` beside it. Anything else that matches makes the
edge unresolved rather than letting a test class stand in for a library one.

**Keys.** A type's key is its declared package plus its walker name, the
composition :data:`provenance.declarations.NAMESPACE_RULES` records for Java,
so ``import org.ex.Outer.Inner`` lands on the symbol node for ``Inner``. An
on-demand import targets the package or type with ``.*`` appended, which no
node carries, so a package cannot fold onto a class of the same spelling.

**Inherited member types.** A member type a class inherits is in scope in its
nested classes and shadows everything outside, so an enclosing class's
supertypes are enumerated: an internal one by its declared members and,
recursively, its own supertypes, read in its own file's scope, keeping only
what JLS 8.5 says is inherited (not private, and package-private only within
the declaring package); a JDK one by :mod:`provenance.edges.jdk`, generated
from the JDK image, where 701 of 3,972 public types have member types to
inherit. Any other supertype has members this cannot see, and a name not settled before reaching
it is unresolved rather than answered by a rule the member would have
shadowed. A dotted base whose member part the checkout does not declare is
unresolved for the same reason. A name an external on-demand import could supply is
unresolved, except a ``java.lang`` name that existed in Java 8; the seven
added since would be shadowed on an older release, so they are settled only
where no external on-demand import is present.
"""

from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath

from ..corpus import dotted_key
from ..dialects import JAVA
from ..symbols import package_for
from ..treesitter import (
    base_captures_in_tree,
    class_nodes_in_tree,
    import_captures_in_tree,
    node_text,
    parse,
)
from .jdk import JDK_PACKAGES, JDK_WITH_MEMBERS
from .model import (
    EXTERNAL,
    IMPORTS,
    INHERITS,
    INTERNAL,
    UNRESOLVED,
    Edge,
    Inventory,
    Resolution,
    unresolved,
)

logger = logging.getLogger(__name__)

JAVA_LANG = frozenset({
    "AbstractMethodError", "Appendable", "ArithmeticException",
    "ArrayIndexOutOfBoundsException", "ArrayStoreException",
    "AssertionError", "AutoCloseable", "Boolean", "BootstrapMethodError",
    "Byte", "Character", "CharSequence", "Class", "ClassCastException",
    "ClassCircularityError", "ClassFormatError", "ClassLoader",
    "ClassNotFoundException", "ClassValue", "Cloneable",
    "CloneNotSupportedException", "Comparable", "Compiler", "Deprecated",
    "Double", "Enum", "EnumConstantNotPresentException", "Error",
    "Exception", "ExceptionInInitializerError", "Float",
    "FunctionalInterface", "IllegalAccessError", "IllegalAccessException",
    "IllegalArgumentException", "IllegalCallerException",
    "IllegalMonitorStateException", "IllegalStateException",
    "IllegalThreadStateException", "IncompatibleClassChangeError",
    "IndexOutOfBoundsException", "InheritableThreadLocal",
    "InstantiationError", "InstantiationException", "Integer",
    "InternalError", "InterruptedException", "Iterable",
    "LayerInstantiationException", "LinkageError", "Long", "Math", "Module",
    "ModuleLayer", "NegativeArraySizeException", "NoClassDefFoundError",
    "NoSuchFieldError", "NoSuchFieldException", "NoSuchMethodError",
    "NoSuchMethodException", "NullPointerException", "Number",
    "NumberFormatException", "Object", "OutOfMemoryError", "Override",
    "Package", "Process", "ProcessBuilder", "ProcessHandle", "Readable",
    "Record", "ReflectiveOperationException", "Runnable", "Runtime",
    "RuntimeException", "RuntimePermission", "SafeVarargs",
    "SecurityException", "SecurityManager", "Short", "StackOverflowError",
    "StackTraceElement", "StackWalker", "StrictMath", "String",
    "StringBuffer", "StringBuilder", "StringIndexOutOfBoundsException",
    "SuppressWarnings", "System", "Thread", "ThreadDeath", "ThreadGroup",
    "ThreadLocal", "Throwable", "TypeNotPresentException", "UnknownError",
    "UnsatisfiedLinkError", "UnsupportedClassVersionError",
    "UnsupportedOperationException", "VerifyError", "VirtualMachineError",
    "Void",
})

#: The members of :data:`JAVA_LANG` whose ``@since`` in the JDK 17 sources is
#: 9 or later. On Java 8, ``import com.google.inject.*`` and ``Module`` is
#: Guice's, so for these the compile-error argument does not hold.
JAVA_LANG_SINCE_9 = frozenset({
    "IllegalCallerException", "LayerInstantiationException", "Module",
    "ModuleLayer", "ProcessHandle", "Record", "StackWalker",
})

_TYPE_ARGUMENTS = re.compile(r"<[^<>]*>")
MAIN = "src/main/java"
#: ``src/test/java``, ``src/integrationTest/java``, ``src/testFixtures/java``:
#: Maven and Gradle source sets that compile against the main one beside them.
_SOURCE_SET = re.compile(r"^(.*?)src/[^/]+/java$")
#: First segments a fully-qualified name may start with beyond the checkout's
#: own packages and the file's imports: the JDK's.
JDK_ROOTS = frozenset(p.split(".")[0] for p in JDK_PACKAGES)


def _type_name(written: str) -> str:
    """The type a base names: every type-argument group removed.

    ``AAClass<String>.BBClass<Number>`` is ``AAClass.BBClass``; cutting at the
    first ``<`` named the outer class instead. ``""`` for an annotated type,
    which is left unresolved rather than parsing annotations here; an
    annotation inside the type arguments (``Comparable<@Nullable M>``) is
    stripped with them and does not count.
    """
    while (stripped := _TYPE_ARGUMENTS.sub("", written)) != written:
        written = stripped
    return "" if "@" in written else written


def _member_free(qualified: str) -> bool:
    """Whether an external supertype is a JDK type with no member type to inherit."""
    parts = qualified.split(".")
    in_jdk = any(".".join(parts[:i]) in JDK_PACKAGES for i in range(1, len(parts)))
    return in_jdk and qualified not in JDK_WITH_MEMBERS


def _access(node, enclosing_type: str | None) -> str:
    """``private``, ``package`` or ``public``; protected counts as public here."""
    modifiers = next((c for c in node.named_children if c.type == "modifiers"), None)
    words = set(node_text(modifiers).split()) if modifiers is not None else set()
    if "private" in words:
        return "private"
    if words & {"public", "protected"} or enclosing_type in ("interface_declaration", "annotation_type_declaration"):
        return "public"
    return "package"


class JavaResolver:
    """Edges for one Java file, resolved against one checkout."""

    def __init__(self, inventory: Inventory):
        if inventory.root is None:
            raise ValueError("Java resolution reads every file's declarations; the inventory needs a root")
        #: Fully-qualified class name, real case, to the files declaring it.
        self.classes: dict[str, set[str]] = {}
        self.packages: set[str] = set()
        #: Class to ``private``, ``package`` or ``public``, and to its package.
        self.access: dict[str, str] = {}
        self.package_of: dict[str, str] = {}
        #: File to the source tree it compiles in.
        self.trees: dict[str, str] = {}
        for path in sorted(inventory.paths):
            if not path.lower().endswith(JAVA.suffixes):
                continue
            source = inventory.read(path)
            if source is None:
                logger.debug("could not read %s", path)
                continue
            stated = JAVA.package_of(path, parse(source, JAVA).root_node)
            package = stated.rpartition(".")[0] if stated else ""
            if package:
                self.packages.add(package)
            kinds: dict[str, str] = {}
            for name, node in class_nodes_in_tree(source, JAVA):
                qualified = f"{package}.{name}" if package else name
                kinds[name] = node.type
                self.classes.setdefault(qualified, set()).add(path)
                self.access[qualified] = _access(node, kinds.get(name.rpartition(".")[0]))
                self.package_of[qualified] = package
            directory = PurePosixPath(path).parent.as_posix()
            layout = package.replace(".", "/")
            if layout and (directory == layout or directory.endswith(f"/{layout}")):
                self.trees[path] = directory[: -len(layout)].rstrip("/")
            else:
                self.trees[path] = directory
        self.inventory = inventory
        #: Class to its declared member types, simple name to qualified.
        self.children: dict[str, dict[str, str]] = {}
        for name in self.classes:
            parent, _, simple = name.rpartition(".")
            if parent in self.classes:
                self.children.setdefault(parent, {})[simple] = name
        self._referrer = ""
        self._scopes: dict[str, _Scope] = {}
        self._inherited: dict[str, dict[str, str | None] | None] = {}
        self._in_progress: set[str] = set()

    # ------------------------------------------------------------------- types

    def _visible(self, path: str) -> bool:
        mine, theirs = self.trees.get(self._referrer, ""), self.trees.get(path, "")
        if mine == theirs:
            return True
        source_set = _SOURCE_SET.match(mine)
        return bool(source_set) and theirs == f"{source_set.group(1)}{MAIN}"

    def _lookup(self, name: str) -> Resolution:
        """A name the class index holds, resolved by what this file can see.

        A name more than one file declares stays unresolved even where only one
        copy is visible: the edge would be right in Java and still carry a key
        two declarations share, which the node layer drops as ambiguous, so it
        would join nothing or the wrong thing.
        """
        paths = self.classes[name]
        visible = sorted(p for p in paths if self._visible(p))
        if len(paths) > 1:
            return unresolved(
                f"{name} is declared by {len(paths)} files ({', '.join(sorted(paths))}), so its "
                f"key does not identify one node"
            )
        if visible:
            return Resolution(INTERNAL, dotted_key(name))
        return unresolved(
            f"{name} is declared only in {', '.join(sorted(paths))}, which this file's source "
            f"tree does not compile against"
        )

    def qualified(self, name: str) -> Resolution:
        """A fully-qualified name: a declared class, or nothing in the checkout."""
        if name in self.classes:
            return self._lookup(name)
        parts = name.split(".")
        for i in range(len(parts) - 1, 0, -1):
            head = ".".join(parts[:i])
            if head in self.classes:
                return unresolved(f"{head} declares no member type {'.'.join(parts[i:])}; it may be inherited")
            if head in self.packages:
                return unresolved(f"package {head} is in the checkout and declares no {name}")
        return Resolution(EXTERNAL, dotted_key(name))

    # ------------------------------------------------------------------- edges

    def _scope_of(self, path: str, source: str) -> _Scope:
        """What one file binds, read once: imports, package, classes, bases."""
        if path in self._scopes:
            return self._scopes[path]
        scope = _Scope()
        for capture in import_captures_in_tree(source, JAVA):
            written = capture.written
            if capture.static:
                if capture.names:
                    scope.static_on_demand.append(written)
                else:
                    scope.static_single[written.rpartition(".")[2]] = written.rpartition(".")[0]
            elif capture.names:
                scope.on_demand.append(written)
            else:
                scope.single[written.rpartition(".")[2]] = written
            scope.imports.append(capture)
        stated = JAVA.package_of(path, parse(source, JAVA).root_node)
        scope.package = stated.rpartition(".")[0] if stated else ""
        scope.declared = {name for name, _node in class_nodes_in_tree(source, JAVA)}
        for capture in base_captures_in_tree(source, JAVA):
            scope.bases.append(capture)
            scope.extending.setdefault(capture.owner, []).append(_type_name(capture.written))
        self._scopes[path] = scope
        return scope

    def edges(self, path: str, source: str) -> tuple[set[Edge], bool]:
        """Every IMPORTS and INHERITS edge one file states. Never read by ``ast``."""
        self._referrer = path
        scope = self._scope_of(path, source)
        file_key = dotted_key(package_for(path, source, JAVA))
        edges: set[Edge] = set()

        for capture in scope.imports:
            written = capture.written
            if capture.static:
                # ``import static T.m`` and ``import static T.*`` both depend
                # on the type ``T`` and are written as it, the way
                # ``dialects.java._imported_names`` reduces them: one edge per
                # type, not one per member imported from it.
                owner = written if capture.names else written.rpartition(".")[0]
                edges.add(self.qualified(owner).edge(IMPORTS, file_key, owner, path))
            elif capture.names:
                if written in self.packages:
                    resolved = Resolution(INTERNAL, f"{dotted_key(written)}.*")
                elif written in self.classes:
                    found = self._lookup(written)
                    resolved = Resolution(found.target_class, found.key and f"{found.key}.*", found.reason)
                else:
                    resolved = self.qualified(written)
                    if resolved.target_class == EXTERNAL:
                        resolved = Resolution(EXTERNAL, f"{resolved.key}.*")
                edges.add(resolved.edge(IMPORTS, file_key, f"{written}.*", path))
            else:
                edges.add(self.qualified(written).edge(IMPORTS, file_key, written, path))

        namespace = dotted_key(scope.package)
        for capture in scope.bases:
            owner = f"{namespace}.{capture.owner}" if namespace else capture.owner
            resolved, _ = self._base(capture.owner, _type_name(capture.written), scope)
            edges.add(resolved.edge(INHERITS, dotted_key(owner), capture.written, path,
                                    detail=capture.detail))
        return edges, False

    def _base(self, owner: str, name: str, scope: _Scope) -> tuple[Resolution, str | None]:
        """One base, and the qualified class it names where the checkout declares it."""
        if not name:
            return unresolved("base is annotated; annotations are not parsed"), None
        head, _, rest = name.partition(".")
        found = self._simple(owner, head, scope)
        if found is None:
            if not rest:
                return unresolved(f"{head} is not declared, imported or in java.lang"), None
            # Nothing binds the head, so a fully-qualified name, but only where
            # the head is a package root something states: nothing binding it
            # in the checkout's Java is not proof it is a package, since a
            # generated or Kotlin class in this package would be invisible here.
            roots = {p.split(".")[0] for p in self.packages} | JDK_ROOTS | {
                c.written.split(".")[0] for c in scope.imports
            }
            if head not in roots:
                return unresolved(f"{head} is bound by nothing visible here and is no known package root"), None
            resolved = self.qualified(name)
            return resolved, name if resolved.target_class != UNRESOLVED else None
        resolved, qualified = found
        if not rest or resolved.target_class != INTERNAL:
            if resolved.target_class == EXTERNAL and rest:
                return Resolution(EXTERNAL, f"{resolved.key}.{dotted_key(rest)}"), f"{qualified}.{rest}"
            return resolved, qualified
        member = f"{qualified}.{rest}"
        if member in self.classes:
            return self._lookup(member), member
        return unresolved(f"{qualified} declares no member type {rest}; it may be inherited"), None

    def inherited(self, name: str) -> dict[str, str | None] | None:
        """Member types class ``name`` inherits, simple to qualified; ``None`` if unknowable.

        Each supertype contributes its declared member types and, recursively,
        what it inherits, each resolved in its own file's scope. A name two
        supertypes supply maps to ``None``. Unknowable when a supertype is
        outside the checkout and not a member-free ``java.lang`` type, is
        declared by more than one file, or sits on an inheritance cycle.
        """
        if name in self._inherited:
            return self._inherited[name]
        paths = self.classes.get(name, set())
        if name in self._in_progress or len(paths) != 1:
            return None
        path = next(iter(paths))
        source = self.inventory.read(path)
        if source is None:
            return None
        self._in_progress.add(name)
        referrer, self._referrer = self._referrer, path
        try:
            scope = self._scope_of(path, source)
            local = name[len(scope.package) + 1:] if scope.package else name
            found: dict[str, str | None] | None = {}
            for base in scope.extending.get(local, []):
                resolved, qualified = self._base(local, base, scope)
                if resolved.target_class == INTERNAL and qualified in self.classes:
                    further = self.inherited(qualified)
                    if further is None:
                        found = None
                        break
                    members = {**further, **self.children.get(qualified, {})}
                elif resolved.target_class == EXTERNAL and qualified and _member_free(qualified):
                    members = {}
                else:
                    found = None
                    break
                # JLS 8.5: a private member type is not inherited, and a
                # package-private one only within its declaring package.
                members = {
                    simple: member for simple, member in members.items()
                    if member is None or self._inheritable(member, scope.package)
                }
                for simple, qualified_member in members.items():
                    if simple in found and found[simple] != qualified_member:
                        found[simple] = None
                    else:
                        found[simple] = qualified_member
        finally:
            self._referrer = referrer
            self._in_progress.discard(name)
        self._inherited[name] = found
        return found

    def _inheritable(self, member: str, package: str) -> bool:
        access = self.access.get(member, "public")
        return access == "public" or (access == "package" and self.package_of.get(member) == package)

    def _simple(self, owner: str, head: str, scope: _Scope) -> tuple[Resolution, str | None] | None:
        """A simple type name by JLS 6.4.1 precedence; ``None`` if nothing binds it."""
        package = scope.package
        # 1. Each enclosing class, innermost first: the member types it declares,
        # then those it inherits, which shadow anything further out. Then the
        # types the compilation unit declares at top level.
        enclosing = owner.split(".")[:-1]
        for i in range(len(enclosing), -1, -1):
            local = ".".join([*enclosing[:i], head])
            if local in scope.declared:
                qualified = f"{package}.{local}" if package else local
                return Resolution(INTERNAL, dotted_key(qualified)), qualified
            container = ".".join(enclosing[:i])
            if not container or container not in scope.extending:
                continue
            inherited = self.inherited(f"{package}.{container}" if package else container)
            if inherited is None:
                return unresolved(
                    f"{head} is not declared here, and {container} inherits member types from a "
                    f"supertype whose members are not in the checkout"
                ), None
            if head in inherited:
                if (qualified := inherited[head]) is None:
                    return unresolved(f"{container} inherits more than one member type {head}"), None
                return self._lookup(qualified), qualified
        # 2. A single-type import, or a single-static import of a member type.
        if head in scope.single:
            single = self.qualified(scope.single[head])
            return single, scope.single[head] if single.target_class != UNRESOLVED else None
        if head in scope.static_single:
            member = f"{scope.static_single[head]}.{head}"
            if member in self.classes:
                return self._lookup(member), member
            return unresolved(f"{head} is imported statically from {scope.static_single[head]}, "
                              f"which the checkout does not declare it in"), None
        # 3. The same package, anywhere in the checkout this file can see.
        same = f"{package}.{head}" if package else head
        if same in self.classes:
            return self._lookup(same), same
        # 4. On-demand imports of packages and of types' members, then java.lang.
        supplied = sorted({
            candidate
            for candidate in (f"{p}.{head}" for p in (*scope.on_demand, *scope.static_on_demand))
            if candidate in self.classes
        })
        if len(supplied) > 1:
            return unresolved(f"{head} is supplied by {len(supplied)} on-demand imports"), None
        if supplied:
            return self._lookup(supplied[0]), supplied[0]
        external = [
            p for p in (*scope.on_demand, *scope.static_on_demand)
            if p not in self.packages and p not in self.classes
        ]
        if head in JAVA_LANG and not (external and head in JAVA_LANG_SINCE_9):
            # Settled even beside an external on-demand import: had that
            # supplied the name too, the use would not compile.
            return Resolution(EXTERNAL, f"java.lang.{dotted_key(head)}"), f"java.lang.{head}"
        if external:
            return unresolved(f"{head} may come from on-demand import {external[0]}.*"), None
        return None


class _Scope:
    """What one file binds, gathered once and read for each of its bases."""

    def __init__(self):
        self.package = ""
        self.declared: set[str] = set()
        #: Class, as the walker names it, to the type names it extends or implements.
        self.extending: dict[str, list[str]] = {}
        self.bases: list = []
        self.imports: list = []
        self.single: dict[str, str] = {}
        self.static_single: dict[str, str] = {}
        self.on_demand: list[str] = []
        self.static_on_demand: list[str] = []
