"""jq module loading: include/import directives and JSON data imports.

A module file contains a sequence of `def`s and include/import directives
with no trailing expression. Each loaded module gets one flat scope (like
the prelude) in which its own functions see each other and everything the
module itself pulled in:

* ``include "a";`` merges a module's own definitions under their bare names.
* ``import "a" as x;`` exposes them under ``x::`` qualified names.
* ``import "data" as $d;`` binds the JSON values of a file (always wrapped
  in an array) to ``$d`` and ``$d::d``.

Definitions a module imports or includes for its own use stay private to
that module: they never appear in the namespace exported to importers.
"""
from __future__ import annotations

import json
import os

from .compiler import Env, FuncVal, collect_defined_names
from .errors import JqParseError
from .parser import parse


class _Unit:
    """A loaded .jq module ready to be consumed by an importer."""
    __slots__ = ("path", "exports")

    def __init__(self, path, exports):
        self.path = path
        # list of ((name, arity), FuncVal) for this module's own definitions
        self.exports = exports


class Loader:
    def __init__(self, search_dirs):
        self.search_dirs = list(search_dirs)
        self._cache = {}        # resolved path -> _Unit
        self._loading = set()   # resolved paths currently being loaded

    # ------------------------------------------------------------------
    # AST rewriting: include/import nodes -> env-installing "use" nodes
    # ------------------------------------------------------------------
    def _rewrite(self, node, search_dirs, exposed):
        """Rewrite every directive in an expression tree.

        Directive names used by the tree are added to `exposed`, so calls
        they bind stay dynamic. Loading happens here, hence all file and
        cycle errors surface during the rewrite (Program creation).
        """
        if isinstance(node, tuple) and node:
            tag = node[0]
            if tag == "include":
                _, path, rest = node
                funcs = {}
                unit = self._load_module(path, search_dirs)
                for key, fv in unit.exports:
                    funcs[key] = fv
                    exposed.add(key[0])
                rest = self._rewrite(rest, search_dirs, exposed)
                return ("use", funcs, None, rest)
            if tag == "import":
                _, path, is_data, alias, rest = node
                funcs, variables = {}, {}
                if is_data:
                    data = self._load_data(path, search_dirs)
                    variables[alias] = data
                    variables[alias + "::" + alias] = data
                else:
                    unit = self._load_module(path, search_dirs)
                    for (name, arity), fv in unit.exports:
                        key = (alias + "::" + name, arity)
                        funcs[key] = fv
                        exposed.add(key[0])
                rest = self._rewrite(rest, search_dirs, exposed)
                return ("use", funcs, variables, rest)
            return tuple(self._rewrite(sub, search_dirs, exposed)
                         for sub in node)
        if isinstance(node, list):
            return [self._rewrite(sub, search_dirs, exposed) for sub in node]
        return node

    def build(self, source):
        """Parse a top-level program and resolve every directive in it."""
        ast = parse(source)
        exposed = set()
        ast = self._rewrite(ast, self.search_dirs, exposed)
        return ast, exposed

    # ------------------------------------------------------------------
    # Module loading
    # ------------------------------------------------------------------
    def _load_module(self, path, search_dirs):
        fname = self._resolve(path, search_dirs, ".jq")
        unit = self._cache.get(fname)
        if unit is not None:
            return unit
        if fname in self._loading:
            raise JqParseError(
                "module %r is part of an import cycle" % path)
        self._loading.add(fname)
        try:
            try:
                with open(fname, encoding="utf-8") as f:
                    source = f.read()
            except OSError:
                raise JqParseError("module not found: %s" % path) from None
            ast = parse(source, library=True)
            unit = self._build_unit(fname, ast)
        finally:
            self._loading.discard(fname)
        self._cache[fname] = unit
        return unit

    def _build_unit(self, fname, ast):
        # A module's own includes/imports resolve in its own directory first.
        mod_dir = os.path.dirname(fname)
        search_dirs = ([mod_dir] if mod_dir else []) + self.search_dirs

        exposed = set()
        ops = []            # replayed to build the flat scope (later wins)
        node = ast
        while node[0] in ("funcdef", "include", "import"):
            tag = node[0]
            if tag == "funcdef":
                _, name, params, body, rest = node
                # Directives nested inside a function body are local to it.
                body = self._rewrite(body, search_dirs, exposed)
                ops.append(("own", (name, len(params)), params, body))
                node = rest
            elif tag == "include":
                _, path, rest = node
                dep = self._load_module(path, search_dirs)
                funcs = {}
                for key, fv in dep.exports:
                    funcs[key] = fv
                    exposed.add(key[0])
                ops.append(("funcs", funcs))
                node = rest
            else:
                _, path, is_data, alias, rest = node
                funcs, variables = {}, {}
                if is_data:
                    data = self._load_data(path, search_dirs)
                    variables[alias] = data
                    variables[alias + "::" + alias] = data
                else:
                    dep = self._load_module(path, search_dirs)
                    for (name, arity), fv in dep.exports:
                        key = (alias + "::" + name, arity)
                        funcs[key] = fv
                        exposed.add(key[0])
                ops.append(("funcs", funcs))
                ops.append(("vars", variables))
                node = rest

        if node[0] != "empty":
            raise JqParseError(
                "module %s should contain only function definitions"
                % fname)

        # The module's functions compile in a name context covering its own
        # definitions (and filter parameters) plus everything its directives
        # expose under bare or qualified names.
        from .prelude import prelude_env
        ctx = frozenset(collect_defined_names(ast)) | exposed

        funcs = {}
        variables = {}
        own_funcvals = []
        own_latest = {}
        for op in ops:
            if op[0] == "own":
                _, key, params, body = op
                fv = FuncVal(params, body, ctx)
                own_funcvals.append(fv)
                own_latest[key] = fv
                funcs[key] = fv
            elif op[0] == "funcs":
                funcs.update(op[1])
            else:
                variables.update(op[1])

        root = Env(parent=prelude_env(), funcs=funcs or None,
                   vars=variables or None)
        for fv in own_funcvals:
            fv.env = root

        # Only this module's own definitions are exported: definitions it
        # pulled in for itself must not leak through its namespace.
        exports = [(key, fv) for key, fv in own_latest.items()
                   if funcs.get(key) is fv]
        return _Unit(fname, exports)

    # ------------------------------------------------------------------
    # JSON data imports
    # ------------------------------------------------------------------
    def _load_data(self, path, search_dirs):
        # Data files are only looked up as "<path>.json" (jq has no
        # <path>/<path>.json fallback for data imports).
        fname = None
        for base in search_dirs:
            candidate = os.path.join(base, path + ".json")
            if os.path.isfile(candidate):
                fname = os.path.realpath(candidate)
                break
        if fname is None:
            raise JqParseError("module not found: %s" % path)
        try:
            with open(fname, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            raise JqParseError("module not found: %s" % path) from None
        values = []
        decoder = json.JSONDecoder()
        i, n = 0, len(text)
        try:
            while i < n:
                while i < n and text[i] in " \t\r\n":
                    i += 1
                if i >= n:
                    break
                value, i = decoder.raw_decode(text, i)
                values.append(value)
        except ValueError:
            raise JqParseError(
                "data module %s does not contain valid JSON" % path) from None
        # An empty data file binds to an empty array, as in jq.
        return values

    # ------------------------------------------------------------------
    # Path resolution: "<dir>/<path><ext>", then "<dir>/<path>/<last><ext>"
    # ------------------------------------------------------------------
    def _resolve(self, path, search_dirs, ext):
        leaf = path.rstrip("/").split("/")[-1]
        for base in search_dirs:
            candidate = os.path.join(base, path + ext)
            if os.path.isfile(candidate):
                return os.path.realpath(candidate)
        for base in search_dirs:
            candidate = os.path.join(base, path, leaf + ext)
            if os.path.isfile(candidate):
                return os.path.realpath(candidate)
        raise JqParseError("module not found: %s" % path)
