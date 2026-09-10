"""Persistent URL cache used by the official POSIX installer's curl calls."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def fetch(url, cache, curl, env):
    key = hashlib.sha256(url.encode()).hexdigest()
    target = cache / key
    record = cache / (key + '.json')
    cache.mkdir(parents=True, exist_ok=True)
    if target.is_file() and record.is_file():
        try:
            metadata = json.loads(record.read_text())
            if metadata['url'] == url and metadata['sha256'] == digest(target):
                print(f'[本地缓存] {url.rsplit("/", 1)[-1]}', flush=True)
                return target
        except (ValueError, KeyError):
            pass
    print(f'[下载并缓存] {url.rsplit("/", 1)[-1]}', flush=True)
    with tempfile.TemporaryDirectory(prefix='download-', dir=cache) as tmp:
        partial = Path(tmp) / 'payload'
        result = subprocess.run([curl, '-fL' if sys.stderr.isatty() else '-sSfL', '--connect-timeout', '15', '--retry', '2',
                                 '--speed-limit', '1024', '--speed-time', '60',
                                 url, '-o', str(partial)], env=env)
        if result.returncode:
            raise RuntimeError(f'下载失败（退出码 {result.returncode}），未保存不完整缓存。')
        metadata = {'url': url, 'sha256': digest(partial), 'bytes': partial.stat().st_size}
        partial.replace(target)
        temporary_record = Path(tmp) / 'metadata.json'
        temporary_record.write_text(json.dumps(metadata, indent=2) + '\n')
        temporary_record.replace(record)
    return target


def curl_entry():
    """Handle only the documented curl shape used by the official installer."""
    args = sys.argv[1:]
    url = next((arg for arg in args if arg.startswith(('https://', 'http://'))), None)
    if not url or '-o' not in args:
        print('缓存下载器不支持该 curl 调用。', file=sys.stderr)
        return 2
    try:
        output = Path(args[args.index('-o') + 1])
        source = fetch(url, Path(os.environ['SLAI_DOWNLOAD_CACHE']),
                       os.environ['SLAI_REAL_CURL'], os.environ.copy())
        shutil.copyfile(source, output)
    except (OSError, RuntimeError, IndexError, KeyError) as error:
        print(f'缓存下载失败：{error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(curl_entry())
