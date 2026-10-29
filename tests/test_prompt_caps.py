"""Every list cap a role's schema enforces is stated in that role's prompt, so a model never has to find it in the
pasted schema. A wording drift check: it proves each cap value is stated, not which field the number constrains."""

import json
import re
import unittest
from types import MappingProxyType

from abx.roles import ROLES

from . import REPO

NUMBER_WORDS = MappingProxyType({
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "eighty": 80})
SLOT = re.compile(r"\{\{[a-z_]+\}\}")
STATED = re.compile(r"\b(?:at\s+most|up\s+to|to|exactly)\s+(\d+|[a-z]+)\b", re.I)


def caps(node, path: str = "") -> list[tuple[str, int]]:
    """(schema path, maxItems) for every capped array in a schema."""
    if isinstance(node, list):
        return [c for v in node for c in caps(v, path)]
    if not isinstance(node, dict):
        return []
    found = [(path or ".", node["maxItems"])] if "maxItems" in node else []
    for key, value in node.items():
        found += caps(value, path if key == "properties" else f"{path}[]" if key == "items" else f"{path}.{key}")
    return found


def stated(template: str) -> set[int]:
    """Numbers the prose before the schema states after a qualifier, with every slot removed."""
    prose = SLOT.sub("", template.split("{{schema}}")[0])
    values = set()
    for token in STATED.findall(prose):
        value = int(token) if token.isdigit() else NUMBER_WORDS.get(token.lower())
        if value is not None:
            values.add(value)
    return values


def unstated() -> list[str]:
    missing = []
    for name, role in sorted(ROLES.items()):
        schema = json.loads((REPO / "schemas" / f"{role.schema}.json").read_text())
        said = stated((REPO / "prompts" / f"{name}.md").read_text())
        by_value: dict[int, list[str]] = {}
        for path, value in caps(schema):
            by_value.setdefault(value, []).append(path)
        missing += [f"{name}: {value} ({', '.join(paths)})" for value, paths in sorted(by_value.items())
                    if value not in said]
    return missing


class PromptCapsTest(unittest.TestCase):
    def test_every_schema_cap_is_stated_in_its_prompt(self):
        self.assertEqual(unstated(), [])

    def test_the_check_reads_qualified_numbers_in_prose_only(self):
        self.assertEqual(stated("Give at most 12 ids, up to three items, 1 to 6 each.\n{{schema}}\nat most 99"),
                         {12, 3, 6})
        self.assertEqual(stated("Score all 30 facets. {{facets}} at most {{n}}\n{{schema}}"), set())

    def test_caps_are_found_at_every_depth(self):
        schema = {"properties": {"a": {"type": "array", "maxItems": 3, "items": {"properties": {
            "b": {"type": "array", "maxItems": 12}}}}}}
        self.assertEqual(caps(schema), [(".a", 3), (".a[].b", 12)])
