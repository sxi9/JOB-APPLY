"""Prompt text that asks a model without native structured outputs for JSON matching a schema."""

from __future__ import annotations

import json

from pydantic import BaseModel


def json_instructions(schema: type[BaseModel]) -> str:
    return ("Respond with a single JSON object and nothing else (no prose, no code fences) that matches this "
            "JSON schema:\n" + json.dumps(schema.model_json_schema(), separators=(",", ":")))
