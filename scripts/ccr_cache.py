"""Private five-minute repository snapshots; credentials never enter cache files."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from scripts import cli

TTL = 300
_locks = {}
_guard = threading.Lock()


def account_key(config):
    account = config.get('account', {})
    values = [account.get(k, '') for k in ('access_key_id','access_key_secret')]
    if not all(isinstance(v,str) and v.strip() for v in values):
        return None
    return hashlib.sha256('\0'.join(values).encode()).hexdigest()


def directory(config):
    key = account_key(config)
    return cli.ROOT / '.cache' / 'ccr' / key if key else None


def load(config, namespace, fetch, *, refresh=False):
    folder = directory(config)
    if folder is None:
        return fetch(), time.time(), False
    scope = {k:namespace.get(k) for k in ('region','subscription_name','resource_group_name','zone','name','uid','id')}
    key = hashlib.sha256(json.dumps(scope,sort_keys=True).encode()).hexdigest()
    path = folder / (key + '.json')
    with _guard:
        lock = _locks.setdefault(str(path), threading.Lock())
    with lock:
        if not refresh:
            try:
                entry = json.loads(path.read_text(encoding='utf-8'))
                if (entry['version'] == 1 and entry['scope'] == scope
                        and 0 <= time.time() - entry['updated_at'] < TTL
                        and isinstance(entry['rows'],list)
                        and all(isinstance(row,dict) and isinstance(row.get('name'),str) for row in entry['rows'])):
                    return entry['rows'], entry['updated_at'], True
            except (OSError, ValueError, KeyError, TypeError):
                pass
        rows = fetch()  # Failures never replace a previously complete snapshot.
        updated_at = time.time()
        temporary = None
        try:
            folder.mkdir(parents=True,exist_ok=True,mode=0o700)
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=folder,delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary,0o600)
                json.dump(dict(version=1,scope=scope,updated_at=updated_at,rows=rows),stream,ensure_ascii=False)
            os.replace(temporary,path)
        except OSError:
            pass  # A read-only checkout must still be able to list cloud images.
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return rows, updated_at, False


def invalidate(config, registry, namespace):
    folder = directory(config)
    if folder is None:
        return
    with _guard:
        paths = set(folder.glob('*.json')) | {Path(key) for key in _locks if Path(key).parent == folder}
    for path in paths:
        with _guard:
            lock = _locks.setdefault(str(path),threading.Lock())
        with lock:
            try:
                scope = json.loads(path.read_text(encoding='utf-8'))['scope']
                if scope['name'] == namespace and registry == 'registry.' + scope['region'] + '.sensecore.cn':
                    path.unlink()
            except (OSError,ValueError,KeyError,TypeError):
                continue
