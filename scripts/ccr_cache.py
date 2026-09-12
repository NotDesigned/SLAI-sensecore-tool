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
STALE_TTL = 86400
_generations = {}
_scopes = {}
_jobs = {}
_errors = {}
_slots = threading.BoundedSemaphore(2)
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
        with _guard:
            generation = _generations.get(str(path), 0)
            _scopes[str(path)] = scope
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
            with _guard:
                if generation != _generations.get(str(path), 0):
                    raise cli.ConfigError('镜像缓存已失效，旧查询结果未发布。')
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
    # Never wait for a slow fetch. Its generation prevents a late write-back.
    with _guard:
        paths = set(folder.glob('*.json')) | {Path(key) for key in _scopes if Path(key).parent == folder}
        for path in paths:
            try:
                scope = _scopes.get(str(path)) or json.loads(path.read_text(encoding='utf-8'))['scope']
                if scope['name'] == namespace and registry == 'registry.' + scope['region'] + '.sensecore.cn':
                    _generations[str(path)] = _generations.get(str(path), 0) + 1
                    _errors.pop(str(path), None)
                    path.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError, TypeError):
                continue


_event_guard = threading.Lock()


def invalidate_snapshot(config, registry, namespace, identity):
    """Remember completed snapshot identities across sessions, not every read."""
    folder = directory(config)
    if folder is None:
        return
    key = hashlib.sha256(json.dumps([registry, namespace, identity], sort_keys=True).encode()).hexdigest()
    marker = folder / 'snapshot-events' / key
    with _event_guard:
        if marker.exists():
            return
        invalidate(config, registry, namespace)
        try:
            marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            marker.touch(mode=0o600)
        except OSError:
            pass


# Stat-checked parsed snapshots avoid parsing a large JSON file on every UI poll.
_memory = {}


def location(config, namespace):
    folder = directory(config)
    if folder is None:
        return None, None
    scope = {k:namespace.get(k) for k in ('region','subscription_name','resource_group_name','zone','name','uid','id')}
    key = hashlib.sha256(json.dumps(scope,sort_keys=True).encode()).hexdigest()
    return folder / (key + '.json'), scope


def peek(config, namespace):
    """Read an atomic snapshot without acquiring the network-fetch lock."""
    path, scope = location(config, namespace)
    if path is None:
        return None
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        with _guard:
            cached = _memory.get(str(path))
        if cached is not None and cached[0] == stamp:
            entry = cached[1]
        else:
            entry = json.loads(path.read_text(encoding='utf-8'))
            if (entry['version'] != 1 or entry['scope'] != scope or not isinstance(entry['rows'],list)
                    or any(not isinstance(row,dict) or not isinstance(row.get('name'),str) for row in entry['rows'])):
                return None
            with _guard:
                if len(_memory) >= 16:
                    _memory.pop(next(iter(_memory)))
                _memory[str(path)] = (stamp, entry)
        age = time.time() - entry['updated_at']
        if not 0 <= age < STALE_TTL:
            return None
        return entry['rows'], entry['updated_at'], age < TTL, stamp
    except (OSError, ValueError, KeyError, TypeError):
        return None


def background_state(config, namespace):
    path, _ = location(config, namespace)
    with _guard:
        job = _jobs.get(str(path))
        error = _errors.get(str(path))
        return bool(job and job.is_alive()), error[1] if error else ''


def refresh_background(config, namespace, fetch, *, force=False):
    """At most two daemon fetches, one per scope; exit never waits for them."""
    path, scope = location(config, namespace)
    if path is None:
        return False
    key = str(path)
    with _guard:
        job = _jobs.get(key)
        if job and job.is_alive():
            return True
        error = _errors.get(key)
        if not force and error and time.monotonic() < error[0]:
            return False
        if not _slots.acquire(blocking=False):
            return False
        _errors.pop(key, None)
        _scopes[key] = scope
        generation = _generations.get(key, 0)
        def run():
            try:
                load(config, namespace, fetch, refresh=True)
            except Exception as exc:
                # Provider exception strings may include temporary URLs.
                with _guard:
                    if generation == _generations.get(key, 0):
                        if len(_errors) >= 64:
                            _errors.pop(next(iter(_errors)))
                        message = str(exc) if isinstance(exc, cli.ConfigError) else '请求失败'
                        _errors[key] = (time.monotonic()+30, '后台更新失败：' + message + '；保留缓存，可点“更新云端”重试')
            finally:
                with _guard:
                    _jobs.pop(key, None)
                _slots.release()
        thread = threading.Thread(target=run, name='ccr-refresh', daemon=True)
        _jobs[key] = thread
        thread.start()
    return True
