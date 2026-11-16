"""Write openapi.json and the TemplateConfig JSON Schema for frontend code generation.

Usage: python scripts/export_schemas.py <output_dir>
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("TC_DATA_DIR", tempfile.mkdtemp(prefix="tc-schema-"))
os.environ.setdefault("TC_DATABASE_URL", "sqlite://")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402
from app.schemas.config import TemplateConfig  # noqa: E402


def inline_refs(schema: dict) -> dict:
    """Replace ``$ref`` pointers with their ``$defs`` bodies (json-schema-to-zod does not resolve refs)."""
    defs = schema.get("$defs", {})

    def walk(node, stack=()):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.rsplit("/", 1)[-1]
                if name in stack:
                    raise ValueError(f"Recursive schema at {name}")
                merged = {**defs[name], **{k: v for k, v in node.items() if k != "$ref"}}
                return walk(merged, (*stack, name))
            return {k: walk(v, stack) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v, stack) for v in node]
        return node

    return walk(schema)


def default_nested_objects(node):
    """Give optional object/array properties ``default: {}`` / ``[]`` so the generated validator fills them in.

    Pydantic omits defaults created by ``default_factory`` (e.g. ``font: FontConfig = Field(default_factory=...)``).
    """
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            required = set(node.get("required", []))
            for name, prop in props.items():
                if name in required or "default" in prop:
                    continue
                if prop.get("type") == "object":
                    prop["default"] = {}
                elif prop.get("type") == "array":
                    prop["default"] = []
        for v in node.values():
            default_nested_objects(v)
    elif isinstance(node, list):
        for v in node:
            default_nested_objects(v)
    return node


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    (out / "openapi.json").write_text(json.dumps(create_app().openapi(), indent=2), "utf-8")
    (out / "template-config.schema.json").write_text(json.dumps(default_nested_objects(inline_refs(TemplateConfig.model_json_schema())), indent=2), "utf-8")
    print(f"Wrote schemas to {out}")


if __name__ == "__main__":
    main()
