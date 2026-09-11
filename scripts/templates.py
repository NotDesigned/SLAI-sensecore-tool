"""Project API responses onto the published writable request schema."""
import json
from functools import lru_cache
from pathlib import Path
from scripts import cli


@lru_cache
def schema(kind):
    return json.loads((Path(__file__).resolve().parent.parent / 'schemas' / (kind + '.json')).read_text())


def writable(kind, document):
    def project(value, spec, path):
        if spec.get('readOnly') or value is None:
            return None
        if spec.get('allOf'):
            spec = {**spec, **spec['allOf'][0]}
        if isinstance(value, list):
            return [project(v, spec.get('items', {}), path + '[]') for v in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                field = spec.get('properties', {}).get(key, spec.get('additionalProperties'))
                if field is None:
                    # Returned by the ACP server but absent from both the
                    # published request schema and official CLI request model.
                    if path == 'acp.mount[]' and key == 'access_mode' and item == 'VOLUME_ACCESS_MODE_UNSPECIFIED':
                        continue
                    if item not in (None, '', [], {}):
                        raise cli.ConfigError(f'模板包含 API 未声明字段 {path}.{key}，无法保证完整复制。')
                    continue
                field = field if isinstance(field, dict) else {}
                projected = project(item, field, path + '.' + key)
                if projected is not None:
                    result[key] = projected
            return result
        return value
    return project(document, schema(kind), kind)
