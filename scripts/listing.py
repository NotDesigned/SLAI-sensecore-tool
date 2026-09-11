"""Small list pages; local snapshots and remote pages share one UI contract."""
from dataclasses import dataclass


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
