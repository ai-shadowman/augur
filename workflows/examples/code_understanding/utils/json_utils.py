import json
import logging
import os
import re
import yaml

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())


def merge_json(base, override, treat_lists_as_singletons: bool = False):
    """Recursively merges override into base.

    - Both dicts: keys combined; override wins on conflict.
    - Both lists: base elements kept; override elements appended if not already present.
      When ``treat_lists_as_singletons=False`` (default) each override element is treated
      as an independent value: appended only if an equal element is not already present
      (identity semantics — multiple elements of the same type may coexist).
      When ``treat_lists_as_singletons=True`` at most one element of each type is kept —
      nested dicts/lists are merged recursively into the first element of the same type
      (type-based singleton semantics).
    - Otherwise: override takes precedence over base.
    """
    if not override:
        return base
    if not base:
        return override
    if isinstance(base, dict) and isinstance(override, dict):
        merged = {}
        for key in set(base) | set(override):
            merged[key] = merge_json(base.get(key), override.get(key), treat_lists_as_singletons)
        return merged
    if isinstance(base, list) and isinstance(override, list):
        result = list(base)
        for item in override:
            if isinstance(item, (dict, list)):
                if treat_lists_as_singletons:
                    for i, existing in enumerate(result):
                        if type(existing) is type(item):
                            result[i] = merge_json(existing, item, treat_lists_as_singletons)
                            break
                    else:
                        result.append(item)
                else:
                    if item not in result:
                        result.append(item)
            elif item not in result:
                result.append(item)
        return result
    return override if override is not None else base


def _apply_defaults_to_json_keys(target: dict, schema: dict, handle_iterables: bool) -> None:
    """Applies missing schema defaults to target.
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


yaml.add_representer(str, _str_representer)


def extract_json_from_string(text: str) -> dict | list | None:
    """Extracts json from given string."""
    try:
        match = re.search(r'\{.*\}|\[.*\]', text, re.DOTALL)
        return json.loads(match.group())
    except Exception:
        return None


def get_as_json(obj, prefix: str = "", sep: str = "_"):
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
    """Converts code metadata to a YAML string with literal block style for multiline fields."""
    if schema:
        _apply_defaults_to_json_keys(obj, schema, handle_iterables=True)

    json_obj = get_as_json(obj)

    if schema:
        _apply_defaults_to_json_keys(json_obj, schema, handle_iterables=False)

    return yaml.dump(json_obj, default_flow_style=False, allow_unicode=True)
