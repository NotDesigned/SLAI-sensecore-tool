import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import download_cache


class DownloadCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / 'cache with spaces'
        self.url = 'https://example.test/package.tar.gz'

    def download(self, command, **kwargs):
        Path(command[-1]).write_bytes(b'complete archive')
        return subprocess.CompletedProcess(command, 0)

    def test_cache_hit_never_starts_network_process(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with patch.object(subprocess, 'run', side_effect=self.download) as run:
                path = download_cache.fetch(self.url, self.cache, '/curl', {})
            self.assertEqual(run.call_count, 1)
            with patch.object(subprocess, 'run', side_effect=AssertionError('network forbidden')):
                cached = download_cache.fetch(self.url, self.cache, '/curl', {})
        self.assertEqual(cached, path)
        self.assertEqual(cached.read_bytes(), b'complete archive')

    def test_corrupt_cache_is_downloaded_again(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with patch.object(subprocess, 'run', side_effect=self.download):
                path = download_cache.fetch(self.url, self.cache, '/curl', {})
            path.write_bytes(b'corrupt')
            with patch.object(subprocess, 'run', side_effect=self.download) as run:
                path = download_cache.fetch(self.url, self.cache, '/curl', {})
        self.assertEqual(run.call_count, 1)
        self.assertEqual(path.read_bytes(), b'complete archive')

    def test_failed_download_does_not_leave_partial_cache(self):
        def fail(command, **kwargs):
            Path(command[-1]).write_bytes(b'partial')
            return subprocess.CompletedProcess(command, 18)
        with patch.object(subprocess, 'run', side_effect=fail), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                download_cache.fetch(self.url, self.cache, '/curl', {})
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_cache_separates_versions_and_platforms(self):
        with patch.object(subprocess, 'run', side_effect=self.download), contextlib.redirect_stdout(io.StringIO()):
            first = download_cache.fetch(self.url, self.cache, '/curl', {})
            second = download_cache.fetch(self.url + '?version=2', self.cache, '/curl', {})
        self.assertNotEqual(first, second)
