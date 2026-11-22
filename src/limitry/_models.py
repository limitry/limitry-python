"""Response models: pydantic v2, read leniently.

Every response is a model with snake_case attributes and the API's own
camelCase names as aliases (``member.joined_at`` from ``joinedAt``), as in
OpenAI's and Anthropic's Python SDKs (owner, 2026-11-22). A response is
CONSTRUCTED, not validated: a field or an enum value the API adds later
never fails a caller. A new field is kept (``extra="allow"``) and read by
its API name (``model.someNewField``); a value of a type the model doesn't
expect is kept as it came. ``to_dict()`` and ``to_json()`` give it back in
the API's names.
"""

from __future__ import annotations

import datetime as _dt
import inspect
from types import UnionType
from typing import Any, Dict, List, Literal, Mapping, Optional, Type, TypeVar, Union, cast, get_args, get_origin

import pydantic

_M = TypeVar("_M", bound="BaseModel")


class BaseModel(pydantic.BaseModel):
    """A response from the API. Attributes in Python's names; ``to_dict()``
    and ``to_json()`` in the API's."""

    model_config = pydantic.ConfigDict(extra="allow", protected_namespaces=(), arbitrary_types_allowed=True)

    def to_dict(
        self,
        *,
        mode: Literal["json", "python"] = "python",
        use_api_names: bool = True,
        exclude_unset: bool = True,
        exclude_none: bool = False,
    ) -> Dict[str, Any]:
        """The model as a dict, keyed by the API's field names (``use_api_names``).

        Only the fields the API sent (``exclude_unset``). ``mode="json"``
        turns datetimes into strings, as the API sent them.
        """
        return self.model_dump(
            mode=mode,
            by_alias=use_api_names,
            exclude_unset=exclude_unset,
            exclude_none=exclude_none,
            warnings=False,
        )

    def to_json(
        self,
        *,
        indent: Optional[int] = 2,
        use_api_names: bool = True,
        exclude_unset: bool = True,
        exclude_none: bool = False,
    ) -> str:
        """The model as JSON, in the API's field names (see ``to_dict``)."""
        return self.model_dump_json(
            indent=indent,
            by_alias=use_api_names,
            exclude_unset=exclude_unset,
            exclude_none=exclude_none,
            warnings=False,
        )


def _construct_model(cls: Type[_M], data: Mapping[str, Any]) -> _M:
    """A model from the API's data without validating it, fields built
    recursively (OpenAI's ``construct``)."""
    fields: Dict[str, Any] = {}
    given = set()
    consumed = set()
    for name, field in cls.model_fields.items():
        key = field.alias or name
        if key in data:
            fields[name] = construct_type(data[key], field.annotation)
            given.add(name)
            consumed.add(key)
        elif not field.is_required():
            fields[name] = field.get_default(call_default_factory=True)
    extra = {key: value for key, value in data.items() if key not in consumed}
    model = cls.__new__(cls)
    object.__setattr__(model, "__dict__", fields)
    object.__setattr__(model, "__pydantic_fields_set__", given | set(extra))
    object.__setattr__(model, "__pydantic_extra__", extra)
    object.__setattr__(model, "__pydantic_private__", None)
    return model


def _is_model(type_: Any) -> bool:
    return inspect.isclass(type_) and issubclass(type_, pydantic.BaseModel)


def _datetime(value: Any, type_: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        if type_ is _dt.date:
            return _dt.date.fromisoformat(value)
        # Python 3.10's fromisoformat doesn't read a trailing "Z".
        return _dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return value


def construct_type(value: Any, type_: Any) -> Any:
    """``value`` as ``type_``, as far as it fits: models built, lists and
    dicts walked, datetimes parsed; anything else kept as it came."""
    if value is None or type_ is None or type_ is Any:
        return value
    origin = get_origin(type_)
    if origin is Union or origin is UnionType:
        variants = [t for t in get_args(type_) if t is not type(None)]
        if len(variants) == 1:
            return construct_type(value, variants[0])
        # Several shapes: the first that validates, else the first model.
        for variant in variants:
            if _is_model(variant) and isinstance(value, Mapping):
                try:
                    return variant.model_validate(value)
                except pydantic.ValidationError:
                    continue
        for variant in variants:
            if _is_model(variant) and isinstance(value, Mapping):
                return _construct_model(variant, value)
        return value
    if origin is Literal:
        return value
    if origin in (list, List):
        (item,) = get_args(type_) or (Any,)
        return [construct_type(v, item) for v in value] if isinstance(value, list) else value
    if origin in (dict, Dict):
        args = get_args(type_)
        item = args[1] if len(args) == 2 else Any
        return {k: construct_type(v, item) for k, v in value.items()} if isinstance(value, Mapping) else value
    if _is_model(type_):
        return _construct_model(type_, value) if isinstance(value, Mapping) else value
    if type_ in (_dt.datetime, _dt.date):
        return _datetime(value, type_)
    return value


def construct(type_: Type[_M], value: Any) -> _M:
    """A typed ``construct_type`` for one model."""
    return cast(_M, construct_type(value, type_))


__all__ = ["BaseModel", "construct", "construct_type"]
