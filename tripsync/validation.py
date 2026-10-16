"""Small hand-written request validation.

Each endpoint describes its fields as a dict of Field objects; load() reads the
JSON body and returns only the fields that were sent, already converted to
Python types. Anything invalid raises ValidationError, which create_app() turns
into a 400 response with a readable message.
"""
import datetime as dt
import uuid

from flask import request


class ValidationError(Exception):
    pass


class Field:
    def __init__(self, required=False, nullable=True):
        self.required = required
        # A required field is never allowed to be null.
        self.nullable = nullable and not required

    def parse(self, key, value):
        if value is None:
            if not self.nullable:
                raise ValidationError(f"{key} is required")
            return None
        return self.convert(key, value)

    def convert(self, key, value):
        raise NotImplementedError


class Text(Field):
    def __init__(self, max_len, **kwargs):
        super().__init__(**kwargs)
        self.max_len = max_len

    def convert(self, key, value):
        if not isinstance(value, str):
            raise ValidationError(f"{key} must be a string")
        value = value.strip()
        if len(value) > self.max_len:
            raise ValidationError(f"{key} must be at most {self.max_len} characters")
        if not value:
            if not self.nullable:
                raise ValidationError(f"{key} must not be empty")
            return None  # an empty optional field is stored as null
        return value


class Date(Field):
    def convert(self, key, value):
        try:
            return dt.date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{key} must be a date like 2026-10-16") from None


class Time(Field):
    def convert(self, key, value):
        try:
            return dt.datetime.strptime(value, "%H:%M").time()
        except (TypeError, ValueError):
            raise ValidationError(f"{key} must be a time like 14:30") from None


class Number(Field):
    def __init__(self, minimum, maximum, **kwargs):
        super().__init__(**kwargs)
        self.minimum = minimum
        self.maximum = maximum

    def convert(self, key, value):
        # bool is a subclass of int in Python, so True would pass as 1 otherwise.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{key} must be a number")
        if not self.minimum <= value <= self.maximum:
            raise ValidationError(f"{key} must be between {self.minimum} and {self.maximum}")
        return float(value)


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("request body must be a JSON object")
    return data


def load(fields, partial=False):
    """Validate the request body against `fields`.

    partial=False (create): required fields must be present.
    partial=True (PATCH): only the fields that were sent are checked and returned.
    Unknown keys are ignored.
    """
    data = json_body()
    result = {}
    for key, field in fields.items():
        if key in data:
            result[key] = field.parse(key, data[key])
        elif field.required and not partial:
            raise ValidationError(f"{key} is required")
    return result


def parse_uuid(key, value):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ValidationError(f"{key} must be a UUID") from None


def optional_id():
    """The client-chosen ID for a new row, if it sent one (see models.py)."""
    data = json_body()
    if data.get("id") is None:
        return None
    return parse_uuid("id", data["id"])
