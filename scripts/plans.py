"""Save reviewable creation plans with private permissions."""
import json
import os
import tempfile
from pathlib import Path
import yaml
from scripts import cli


def save(service, name, document, *, yaml_format=False):
    directory = cli.ROOT / '.cache' / service
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix=name + '-',
                                     suffix='.yaml' if yaml_format else '.json', dir=directory, delete=False) as stream:
        os.chmod(stream.name, 0o600)
        if yaml_format:
            yaml.safe_dump(document, stream, allow_unicode=True, sort_keys=False)
        else:
            json.dump(document, stream, ensure_ascii=False, indent=2)
        return Path(stream.name)
