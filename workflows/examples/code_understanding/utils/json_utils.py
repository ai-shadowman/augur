
import json

import re
import yaml
import logging
logging.basicConfig(level=logging.INFO)


def _apply_defaults_to_json_keys(target: dict, schema: dict, handle_iterables: bool) -> None:
    """
    Applies missing schema defaults to target.
    If handle_iterables=True, applies list/dict defaults.
    If handle_iterables=False, applies scalar defaults.
    """
    if schema is None:
        logging.info("Schema cannot be None or empty.")
        return

    if target is None:
        logging.info("Target cannot be None or empty.")
        return

    for key, default in schema.items():

        is_iterable = isinstance(default, (list, dict))

        if is_iterable == handle_iterables and key not in target:

            target[key] = default

def _str_representer(dumper, data):
    if '\n' in data:

        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')

    return dumper.represent_scalar('tag:yaml.org,2002:str', data)


def extract_json_from_string(text: str) -> dict | list | None:
    """Extracts json from given string."""
    try:
        match = re.search(r'\{.*\}|\[.*\]', text, re.DOTALL)

        return json.loads(match.group())

    except Exception:
        return None

def get_as_json(obj, prefix: str ="", sep: str ="_"):
    """Flattens code metadata file."""

    items = {}

    if isinstance(obj, dict):

        for k, v in obj.items():

            items.update(get_as_json(v, f"{prefix}{sep}{k}" if prefix else k, sep))

    elif isinstance(obj, list):

        for i, v in enumerate(obj):

            items.update(get_as_json(v, f"{prefix}{sep}{i}" if prefix else str(i), sep))
    else:

        items[prefix] = obj

    return items


def flatten_code_metadata(obj, schema: dict = None):

    """
    Converts code metadata to a YAML string with literal block style for multiline fields.
    """

    yaml.add_representer(str, _str_representer)

    if schema: _apply_defaults_to_json_keys(obj, schema, handle_iterables=True)

    json_obj = get_as_json(obj)

    if schema: _apply_defaults_to_json_keys(json_obj, schema, handle_iterables=False)

    return yaml.dump(json_obj, default_flow_style=False, allow_unicode=True)

