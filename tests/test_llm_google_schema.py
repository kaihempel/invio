"""Tests of the pure schema converter ``invio.llm.google.gemini_schema``."""

import copy
import enum
from typing import Any, Literal

import jsonschema
import pytest
from pydantic import BaseModel, Field

from invio.graph.nodes.relevance import RelevanceResult
from invio.graph.nodes.summarize_item import ItemSummary
from invio.llm.base import LLMConfigError
from invio.llm.google import gemini_schema
from tests.google_helpers import Nested


def _convert(model: type[BaseModel]) -> dict[str, Any]:
    return gemini_schema(model.model_json_schema())


def test_relevance_result_loses_min_length_but_keeps_bounds() -> None:
    schema = _convert(RelevanceResult)

    assert "minLength" not in schema["properties"]["reason"]
    assert schema["properties"]["score"]["minimum"] == 0
    assert schema["properties"]["score"]["maximum"] == 1
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["score", "reason", "key_points"]
    assert schema["properties"]["key_points"]["items"] == {"type": "string"}


def test_item_summary_keeps_item_counts_and_loses_min_length() -> None:
    schema = _convert(ItemSummary)
    bullets = schema["properties"]["bullets"]

    assert bullets["minItems"] == 3
    assert bullets["maxItems"] == 6
    assert "minLength" not in bullets["items"]
    assert "minLength" not in schema["properties"]["headline"]


def test_refs_are_inlined_and_defs_removed() -> None:
    schema = _convert(Nested)

    assert "$defs" not in schema
    assert "$ref" not in repr(schema)
    assert schema["properties"]["main"]["type"] == "object"
    assert schema["properties"]["others"]["items"]["properties"]["name"] == {
        "title": "Name",
        "type": "string",
    }
    sample = {
        "main": {"name": "a", "weight": 1},
        "others": [{"name": "b", "weight": 2}],
        "backup": {"name": "c", "weight": 3},
    }
    jsonschema.validate(sample, schema)
    jsonschema.validate(sample, Nested.model_json_schema())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**sample, "backup": {"name": "c", "weight": -1}}, schema)


class _Color(BaseModel):
    label: Literal["red", "green"]
    note: str | None = Field(default=None, description="free note")


def test_optional_literal_and_description_survive() -> None:
    schema = _convert(_Color)

    assert schema["properties"]["label"]["enum"] == ["red", "green"]
    assert schema["properties"]["note"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert schema["properties"]["note"]["description"] == "free note"
    assert "default" not in schema["properties"]["note"]


def test_unsupported_keywords_are_dropped() -> None:
    schema = gemini_schema(
        {
            "type": "object",
            "properties": {
                "a": {
                    "type": "string",
                    "pattern": "^x$",
                    "default": "x",
                    "minLength": 1,
                    "maxLength": 2,
                },
                "b": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "exclusiveMaximum": 1,
                    "minimum": 0,
                },
            },
        }
    )

    assert schema["properties"]["a"] == {"type": "string"}
    assert schema["properties"]["b"] == {"type": "number", "minimum": 0}


def test_property_names_are_never_filtered() -> None:
    schema = gemini_schema(
        {
            "type": "object",
            "properties": {"default": {"type": "string"}, "pattern": {"type": "string"}},
            "required": ["default"],
        }
    )

    assert set(schema["properties"]) == {"default", "pattern"}


class _Node(BaseModel):
    child: "_Node | None" = None


def test_recursive_schema_raises_config_error_naming_it() -> None:
    with pytest.raises(LLMConfigError, match=r"_Node.*recursive"):
        _convert(_Node)


def test_non_local_ref_raises_config_error() -> None:
    with pytest.raises(LLMConfigError, match="reference"):
        gemini_schema({"type": "object", "properties": {"a": {"$ref": "http://x/y.json"}}})


def test_missing_definition_raises_config_error() -> None:
    with pytest.raises(LLMConfigError, match="reference"):
        gemini_schema({"type": "object", "properties": {"a": {"$ref": "#/$defs/Nope"}}})


def test_input_is_not_mutated() -> None:
    original = Nested.model_json_schema()
    snapshot = copy.deepcopy(original)

    gemini_schema(original)

    assert original == snapshot


def test_prefix_items_and_lists_are_converted() -> None:
    schema = gemini_schema(
        {
            "type": "array",
            "prefixItems": [{"type": "string", "minLength": 1}, {"type": "integer"}],
            "minItems": 2,
        }
    )

    assert schema["prefixItems"] == [{"type": "string"}, {"type": "integer"}]
    assert schema["minItems"] == 2


class _Mood(enum.Enum):
    HAPPY = "happy"
    SAD = "sad"


class _Inner(BaseModel):
    tag: Nested
    mood: _Mood


class _Outer(BaseModel):
    """Optional and list-of references, an enum definition and a reference inside a reference."""

    maybe: _Inner | None = None
    many: list[_Inner] | None = None
    mood: _Mood


def test_references_inside_any_of_and_nested_definitions_are_inlined() -> None:
    original = _Outer.model_json_schema()
    schema = gemini_schema(original)

    assert "$defs" not in schema
    assert "$ref" not in repr(schema)
    maybe = schema["properties"]["maybe"]["anyOf"]
    assert maybe[1] == {"type": "null"}
    assert maybe[0]["properties"]["tag"]["properties"]["main"]["properties"]["weight"] == {
        "minimum": 0,
        "title": "Weight",
        "type": "integer",
    }
    many = schema["properties"]["many"]["anyOf"][0]
    assert many["type"] == "array"
    assert many["items"]["properties"]["mood"]["enum"] == ["happy", "sad"]
    assert schema["properties"]["mood"]["enum"] == ["happy", "sad"]
    sample = {
        "maybe": {
            "tag": {
                "main": {"name": "a", "weight": 1},
                "others": [],
                "backup": {"name": "b", "weight": 2},
            },
            "mood": "sad",
        },
        "many": None,
        "mood": "happy",
    }
    jsonschema.validate(sample, schema)
    jsonschema.validate(sample, original)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**sample, "mood": "angry"}, schema)


class _Loop(BaseModel):
    items: list["_Loop"] | None = None


def test_recursion_through_any_of_and_items_is_a_config_error() -> None:
    with pytest.raises(LLMConfigError, match="recursive"):
        _convert(_Loop)


def test_one_of_becomes_any_of_and_is_converted_recursively() -> None:
    schema = gemini_schema(
        {
            "type": "object",
            "properties": {
                "pet": {
                    "oneOf": [
                        {"type": "string", "minLength": 1},
                        {"type": "object", "properties": {"n": {"type": "integer"}}},
                    ]
                }
            },
        }
    )

    assert schema["properties"]["pet"] == {
        "anyOf": [
            {"type": "string"},
            {"type": "object", "properties": {"n": {"type": "integer"}}},
        ]
    }


def test_const_becomes_a_single_value_enum() -> None:
    schema = gemini_schema(
        {"type": "object", "properties": {"k": {"const": "a", "type": "string"}}}
    )

    assert schema["properties"]["k"] == {"type": "string", "enum": ["a"]}


def test_const_does_not_override_an_existing_enum() -> None:
    schema = gemini_schema({"const": "a", "enum": ["a", "b"]})

    assert schema == {"enum": ["a", "b"]}


def test_one_of_next_to_any_of_keeps_both_alternatives() -> None:
    schema = gemini_schema({"anyOf": [{"type": "string"}], "oneOf": [{"type": "integer"}]})

    assert schema == {"anyOf": [{"type": "string"}]}
