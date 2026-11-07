"""Tests for the jq module system: include/import and JSON data imports.

Module search directories come from JQ_LIBRARY_PATH (pathsep-separated),
read when the Program is created.
"""
import pytest

import purejq
from purejq import JqError, JqParseError


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("JQ_LIBRARY_PATH", str(tmp_path))
    return tmp_path


def write(root, name, content):
    """Write a module/data file; name may contain '/' for subdirectories."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def run(program, value=None):
    return purejq.Program(program).all(value)


# --------------------------------------------------------------------------
# include
# --------------------------------------------------------------------------

def test_include_merges_definitions(lib):
    write(lib, "a.jq", 'def a: "a";\ndef b($x): $x + 1;\n')
    assert run('include "a"; [a, b(41)]') == [["a", 42]]


def test_multiple_includes_later_wins(lib):
    write(lib, "s1.jq", "def e: 1;\n")
    write(lib, "s2.jq", "def e: 2;\n")
    write(lib, "s3.jq", "def e: 3;\n")
    assert run('include "s1"; include "s2"; include "s3"; e') == [3]


def test_later_definition_within_one_module_wins(lib):
    # mirrors jq's own shadow1.jq: def e: 1; def e: 2;
    write(lib, "shadow.jq", "def e: 1;\ndef e: 2;\n")
    assert run('include "shadow"; e') == [2]


def test_included_definitions_call_each_other_and_recurse(lib):
    write(lib, "math.jq", """
def even($n): if $n == 0 then true else odd($n - 1) end;
def odd($n): if $n == 0 then false else even($n - 1) end;
def countdown($n): if $n == 0 then [] else [$n] + countdown($n - 1) end;
""")
    assert run('include "math"; [even(10), odd(7), countdown(3)]') == [
        [True, True, [3, 2, 1]]]


def test_module_include_is_private_to_the_module(lib):
    write(lib, "helper.jq", 'def helper: "secret";\n')
    write(lib, "a.jq", 'include "helper";\ndef a: helper;\n')
    # the module's own function may use the privately included definition ...
    assert run('include "a"; a') == ["secret"]
    # ... but it does not leak into the including program, nor into the
    # namespace of an importing program.
    with pytest.raises(JqError):
        run('include "a"; helper')
    with pytest.raises(JqError):
        run('import "a" as m; m::helper')


def test_module_functions_see_prelude(lib):
    write(lib, "m.jq", "def doubled: [1,2,3] | map(. * 2);\n")
    assert run('import "m" as m; m::doubled') == [[2, 4, 6]]


# --------------------------------------------------------------------------
# import ... as <name>
# --------------------------------------------------------------------------

def test_import_namespace(lib):
    write(lib, "a.jq", 'def a: "a";\ndef f($x): $x + 1;\n')
    assert run('import "a" as x; [x::a, x::f(41)]') == [["a", 42]]


def test_import_same_alias_later_wins(lib):
    write(lib, "s1.jq", "def e: 2;\n")
    write(lib, "s2.jq", "def e: 3;\n")
    assert run('import "s1" as f; import "s2" as f; f::e') == [3]


def test_namespaced_call_missing_at_runtime_is_jqerror(lib):
    write(lib, "a.jq", "def a: 1;\n")
    # an unknown qualified name compiles (jq also binds names dynamically)
    # but raises at run time.
    prog = purejq.Program('import "a" as x; x::nope')
    with pytest.raises(JqError):
        prog.all(None)


def test_imported_namespace_does_not_leak_through_importer(lib):
    write(lib, "a.jq", 'def a: "a";\n')
    write(lib, "chain.jq", 'import "a" as x;\ndef ch: x::a;\n')
    assert run('import "chain" as c; c::ch') == ["a"]
    # x belongs to chain's internals only; the program never imported it.
    with pytest.raises(JqError):
        run('import "chain" as c; x::a')


def test_diamond_imports_load_shared_module_once(lib):
    write(lib, "shared.jq", 'def v: "shared";\n')
    write(lib, "d1.jq", 'import "shared" as s;\ndef p: s::v;\n')
    write(lib, "d2.jq", 'import "shared" as s;\ndef q: s::v;\n')
    assert run('import "d1" as one; import "d2" as two; [one::p, two::q]') == [
        ["shared", "shared"]]


# --------------------------------------------------------------------------
# relative paths and the d.jq / d/d.jq fallback
# --------------------------------------------------------------------------

def test_module_resolves_sibling_imports_from_its_own_directory(lib):
    write(lib, "pkg/base.jq", 'def base: "sibling";\n')
    write(lib, "pkg/inner.jq",
          'include "base";\ndef inner: base;\n')
    assert run('import "pkg/inner" as m; m::inner') == ["sibling"]


def test_path_fallback_file_then_directory(lib):
    write(lib, "d/d.jq", 'def f: "subdir";\n')
    write(lib, "pkg/d/d.jq", 'def f: "pkgdir";\n')
    assert run('import "d" as z; z::f') == ["subdir"]
    assert run('import "pkg/d" as z; z::f') == ["pkgdir"]


def test_plain_file_wins_over_directory(lib):
    write(lib, "d.jq", 'def f: "file";\n')
    write(lib, "d/d.jq", 'def f: "subdir";\n')
    assert run('import "d" as z; z::f') == ["file"]


def test_multiple_library_directories(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    write(second, "b.jq", 'def b: "second";\n')
    monkeypatch.setenv("JQ_LIBRARY_PATH",
                       "%s%s%s" % (first, __import__("os").pathsep, second))
    assert run('import "b" as b; b::b') == ["second"]


# --------------------------------------------------------------------------
# import ... as $name (JSON data)
# --------------------------------------------------------------------------

def test_data_import_single_value_is_wrapped(lib):
    write(lib, "data.json", '{"this": "is a test"}\n')
    assert run('import "data" as $d; $d') == [[{"this": "is a test"}]]


def test_data_import_binds_qualified_name(lib):
    write(lib, "data.json", '{"this": "is a test"}\n')
    assert run('import "data" as $d; [$d, $d::d]') == [
        [[{"this": "is a test"}], [{"this": "is a test"}]]]


def test_data_import_multiple_values(lib):
    write(lib, "multi.json", "1\n2\n3\n")
    assert run('import "multi" as $m; $m') == [[1, 2, 3]]


def test_empty_data_file_binds_empty_array(lib):
    write(lib, "blank.json", "")
    assert run('import "blank" as $b; $b') == [[]]


def test_invalid_json_data_file_is_parse_error(lib):
    write(lib, "broken.json", "{not json")
    with pytest.raises(JqParseError):
        purejq.Program('import "broken" as $b; 1')


def test_data_import_has_no_directory_fallback(lib):
    write(lib, "dd/dd.json", "1\n")
    with pytest.raises(JqParseError):
        purejq.Program('import "dd" as $d; 1')


def test_qualified_data_name_uses_the_alias(lib):
    write(lib, "data.json", "1\n")
    assert run('import "data" as $z; $z::z') == [[1]]


def test_data_import_inside_module(lib):
    write(lib, "data.json", '{"this": "is a test","that": "is too"}\n')
    write(lib, "usemod.jq", """
import "data" as $d;
def get: $d[0].this;
def get2: $d::d[0].that;
""")
    assert run('import "usemod" as u; [u::get, u::get2]') == [
        ["is a test", "is too"]]


def test_data_variable_is_private_to_module(lib):
    write(lib, "data.json", "1\n")
    write(lib, "usemod.jq", 'import "data" as $d;\ndef get: $d;\n')
    assert run('import "usemod" as u; u::get') == [[1]]
    with pytest.raises(JqError):
        run('import "usemod" as u; $d')


# --------------------------------------------------------------------------
# errors at Program creation
# --------------------------------------------------------------------------

def test_missing_module_is_parse_error(lib):
    with pytest.raises(JqParseError):
        purejq.Program('import "nope" as x; 1')
    with pytest.raises(JqParseError):
        purejq.Program('include "nope"; 1')


def test_missing_data_file_is_parse_error(lib):
    with pytest.raises(JqParseError):
        purejq.Program('import "nodata" as $d; 1')


def test_cyclic_imports_are_parse_errors(lib):
    write(lib, "c1.jq", 'import "c2" as y;\ndef a: 1;\n')
    write(lib, "c2.jq", 'import "c1" as x;\ndef b: 2;\n')
    with pytest.raises(JqParseError):
        purejq.Program('import "c1" as z; 1')


def test_self_import_is_parse_error(lib):
    write(lib, "self.jq", 'include "self";\ndef s: 1;\n')
    with pytest.raises(JqParseError):
        purejq.Program('include "self"; 1')


def test_nonconstant_import_path_is_parse_error(lib):
    with pytest.raises(JqParseError):
        purejq.Program('import "\\(x)" as z; 1')
    with pytest.raises(JqParseError):
        purejq.Program('include "\\(x)"; 1')


def test_syntax_error_in_module_is_parse_error(lib):
    write(lib, "half.jq", "def f:\n")
    with pytest.raises(JqParseError):
        purejq.Program('import "half" as z; 1')
    write(lib, "stray.jq", "wat;\n")
    with pytest.raises(JqParseError):
        purejq.Program('import "stray" as z; 1')


def test_module_with_trailing_expression_is_parse_error(lib):
    write(lib, "withbody.jq", "def f: 1;\n42\n")
    with pytest.raises(JqParseError):
        purejq.Program('import "withbody" as z; 1')


# --------------------------------------------------------------------------
# environment handling
# --------------------------------------------------------------------------

def test_library_path_is_read_at_program_creation(lib, monkeypatch):
    write(lib, "d.jq", 'def f: "ok";\n')
    prog = purejq.Program('import "d" as d; d::f')
    monkeypatch.delenv("JQ_LIBRARY_PATH")
    assert prog.all(None) == ["ok"]


def test_program_with_modules_is_reusable(lib):
    write(lib, "data.json", '{"v": 1}\n')
    prog = purejq.Program('import "data" as $d; $d[0].v + .')
    assert prog.all(1) == [2]
    assert prog.all(41) == [42]


def test_user_vars_still_layer_above(lib):
    write(lib, "a.jq", "def a: 1;\n")
    prog = purejq.Program('include "a"; [$x, a]')
    assert prog.all(None, vars={"x": "v"}) == [["v", 1]]
