"""Explicit interpolation namespaces work across Python versions."""

from collections import ChainMap, UserDict
import sys
from types import MappingProxyType

import pytest

from brish.brishmod import Brish, get_locals
from tests.conftest import check


@pytest.fixture
def formatter():
    # Quoting and interpolation do not need a shell worker.
    return Brish(delayed_init=True, server_count=1)


@pytest.mark.parametrize("method", ["zstring", "zstring_old"])
@pytest.mark.parametrize("mapping", [MappingProxyType, UserDict, ChainMap])
def test_explicit_mappings_support_nested_expression_scope(formatter, method, mapping):
    source = {"values": [2, 3], "scale": 4}
    namespace = mapping(source)
    cmd = getattr(formatter, method)(
        "printf %s {sum(value * scale for value in values)}",
        locals_=namespace,
    )
    assert cmd.strip() == "printf %s 20"
    assert source == {"values": [2, 3], "scale": 4}
    assert "__builtins__" not in namespace


@pytest.mark.parametrize("method", ["zstring", "zstring_old"])
def test_explicit_frame_locals(formatter, method):
    def caller():
        value = "a b's $value"
        return getattr(formatter, method)(
            "printf %s {value}", locals_=sys._getframe().f_locals
        )

    assert caller().strip() == "printf %s " + formatter.zsh_quote("a b's $value")


@pytest.mark.parametrize("method", ["zstring", "zstring_old"])
def test_implicit_caller_locals_still_work(formatter, method):
    value = "caller"
    assert getattr(formatter, method)("{value}").strip() == "caller"


@pytest.mark.parametrize("method", ["zstring", "zstring_old"])
def test_explicit_empty_mapping_does_not_fall_back_to_caller(formatter, method):
    value = "must remain outside the explicit namespace"
    with pytest.raises(NameError):
        getattr(formatter, method)("{value}", locals_=MappingProxyType({}))


def test_explicit_dict_identity_and_eval_side_effects_are_preserved(formatter):
    namespace = {}
    assert get_locals(locals_=namespace) is namespace
    assert (
        formatter.zstring("{(value := 3)} {value}", locals_=namespace).strip() == "3 3"
    )
    assert namespace["value"] == 3


def test_mapping_is_a_shallow_snapshot():
    values = [1]
    source = {"values": values}
    namespace = get_locals(locals_=MappingProxyType(source))
    source["new"] = 2
    assert type(namespace) is dict
    assert namespace == {"values": [1]}
    assert namespace["values"] is values


def test_mapping_names_are_available_in_dynamic_format_spec(formatter):
    namespace = MappingProxyType({"value": 7, "width": 3})
    assert formatter.zstring("{value:0{width}d}", locals_=namespace).strip() == "007"


def test_getframe_selects_outer_caller(formatter):
    def wrapper():
        return formatter.zstring("{value}", getframe=2)

    value = "outer"
    assert wrapper().strip() == value


def test_shell_and_background_entry_points_accept_frame_locals():
    check(r"""
        from types import MappingProxyType
        b = Brish(server_count=1)
        def caller():
            value = "a b's $value"
            namespace = sys._getframe().f_locals
            assert b.z("printf %s {value}", locals_=namespace).out == value
            with b.zpopen("printf %s {value}", locals_=namespace) as p:
                assert b"".join(chunk for stream, chunk in p if stream == "out").decode() == value
            for background in (False, True):
                result = brish.z_background(
                    "printf %s {value}", locals_=namespace, background_p=background
                )
                if background:
                    result = result.result(timeout=10)
                assert result.out == value
            assert b.z("printf %s {value}", locals_=MappingProxyType({"value": value})).out == value
        caller()
        b.cleanup()
        """)
