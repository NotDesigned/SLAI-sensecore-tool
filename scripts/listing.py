"""Small list pages; local snapshots and remote pages share one UI contract."""
from dataclasses import dataclass
import hashlib
import json
import threading
import time


@dataclass
class Page:
    rows: list
    more: bool
    total: int | None = None


class LocalSource:
    states = ()
    search_hint = '搜索（名称、标签或状态）'

    def __init__(self, fetch, describe=str, *, columns=('资源',), cells=None):
        self.fetch, self.describe = fetch, describe
        self.columns = columns
        self.cells = cells or (lambda row: (describe(row),))
        self.snapshot = None

    def page(self, index, size, query='', state='', refresh=False):
        if refresh or self.snapshot is None:
            rows = self.fetch()
            # Publish only a complete successful snapshot.
            self.snapshot = rows
        query = query.casefold()
        rows = [row for row in self.snapshot if query in self.describe(row).casefold()]
        start = index * size
        return Page(rows[start:start + size], start + size < len(rows), len(rows))


# Resource state stays in memory; CCR image catalogs have a separate disk cache.

_cache = {}
_cache_guard = threading.Lock()
_cache_slots = threading.BoundedSemaphore(2)


def resource_key(kind, config, *scope):
    from scripts.ccr_cache import account_key
    return (kind, account_key(config), json.dumps(scope, sort_keys=True, default=str))


def invalidate():
    with _cache_guard:
        for entry in _cache.values():
            entry['generation'] += 1
            entry['updated'] = 0
            entry['retry'] = 0
            entry['revision'] += 1


class CachedSource:
    """Nonblocking session snapshots; mutations never use these for validation."""
    background_capable = True
    def __init__(self, source, key, ttl=15):
        self.source, self.key, self.ttl = source, key, ttl
        self.local = isinstance(source, LocalSource)
        self.params = (0, 20, '', '')
        self.status_hint = ''
        self.background_token = None
        self.pending = False
        self.background = False
        self._source_lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self.source, name)

    def enable_background(self):
        self.background = True

    def _entry(self):
        index, size, query, state = self.params
        key = (self.key, () if self.local else self.params)
        with _cache_guard:
            if key not in _cache:
                if len(_cache) >= 64:
                    victim = next((k for k,v in _cache.items() if not v['running']), None)
                    if victim is not None:
                        del _cache[victim]
                _cache[key] = dict(data=None,updated=0,received=0,generation=0,revision=0,
                                   running=False,error='',retry=0)
            return _cache[key]

    def _ensure(self, refresh=False):
        entry = self._entry()
        with _cache_guard:
            if refresh:
                entry.update(updated=0,retry=0)
                entry['generation'] += 1
            if entry['running'] or time.monotonic() < entry['retry']:
                return entry
            if entry['data'] is not None and entry['updated'] and time.monotonic()-entry['updated'] < self.ttl:
                return entry
            if not _cache_slots.acquire(blocking=False):
                return entry
            entry['running'] = True
            entry['error'] = ''
            entry['revision'] += 1
            generation = entry['generation']
        params = self.params
        def run():
            try:
                with self._source_lock:
                    data = self.source.fetch() if self.local else self.source.page(*params,refresh=True)
                with _cache_guard:
                    if generation == entry['generation']:
                        entry.update(data=data,updated=time.monotonic(),received=time.monotonic(),
                                     error='',retry=0)
            except Exception as exc:
                from scripts.cli import ConfigError
                message = str(exc) if isinstance(exc, ConfigError) else '请求失败'
                with _cache_guard:
                    if generation == entry['generation']:
                        entry.update(error='后台更新失败：' + message + '；保留缓存，按 r 重试',retry=time.monotonic()+30)
            finally:
                with _cache_guard:
                    entry['running'] = False
                    entry['revision'] += 1
                _cache_slots.release()
        threading.Thread(target=run,name='resource-list',daemon=True).start()
        return entry

    def poll_background(self):
        if not self.background:
            return self.background_token
        entry = self._ensure()
        with _cache_guard:
            return (self.key,self.params,entry['revision'])

    def page(self,index,size,query='',state='',refresh=False):
        if not self.background:
            return self.source.page(index,size,query,state,refresh)
        self.params = (index,size,query,state)
        entry = self._ensure(refresh)
        with _cache_guard:
            data, received = entry['data'], entry['received']
            if data is not None and time.monotonic()-received > 300:
                data = None
            self.background_error = bool(entry['error'])
            self.pending = entry['running'] or (entry['updated']==0 and not entry['error'])
            self.status_hint = entry['error'] or ('后台更新中' if entry['running'] else
                '等待后台查询' if data is None else '会话缓存')
            if data is not None:
                self.status_hint += f' · {max(0,int(time.monotonic()-received))} 秒前'
            self.background_token = (self.key,self.params,entry['revision'])
        if data is None:
            return Page([],False,None)
        if not self.local:
            return data
        filtered = [row for row in data if query.casefold() in self.source.describe(row).casefold()]
        start=index*size
        return Page(filtered[start:start+size],start+size<len(filtered),len(filtered))
