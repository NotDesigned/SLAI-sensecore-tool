"""Copy plain text through the desktop clipboard, without a shell."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

from scripts.cli import ConfigError


def copy_text(value):
    if sys.platform == 'darwin':
        command = ['/usr/bin/pbcopy']
    elif sys.platform == 'win32':
        powershell = shutil.which('pwsh') or shutil.which('powershell.exe')
        if not powershell:
            raise ConfigError('找不到 PowerShell，无法访问 Windows 剪贴板。')
        command = [powershell, '-NoProfile', '-NonInteractive', '-Command',
                   '[Console]::InputEncoding = [System.Text.UTF8Encoding]::new(); '
                   'Set-Clipboard -Value ([Console]::In.ReadToEnd())']
    elif os.environ.get('WAYLAND_DISPLAY') and shutil.which('wl-copy'):
        command = [shutil.which('wl-copy')]
    elif os.environ.get('DISPLAY') and shutil.which('xclip'):
        command = [shutil.which('xclip'), '-selection', 'clipboard']
    elif os.environ.get('DISPLAY') and shutil.which('xsel'):
        command = [shutil.which('xsel'), '--clipboard', '--input']
    else:
        raise ConfigError('当前会话没有可用的系统剪贴板。可保存文本文件后复制；Linux 桌面可安装 wl-clipboard 或 xclip。')
    try:
        result = subprocess.run(command, input=value.encode('utf-8'), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        raise ConfigError('系统剪贴板未响应，复制未完成；可保存文本文件后复制。') from None
    if result.returncode:
        raise ConfigError('无法写入系统剪贴板，复制未完成；可保存文本文件后复制。')


def save_text(value):
    import tempfile
    from scripts import cli
    directory = cli.ROOT / '.cache' / 'text'
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.txt', prefix='slai-',
                                     dir=directory, delete=False) as stream:
        os.chmod(stream.name, 0o600)
        stream.write(value)
        return Path(stream.name)
