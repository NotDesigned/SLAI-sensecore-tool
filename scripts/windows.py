"""Windows installation and per-user environment setup (no administrator required)."""
import os
import platform
import shutil
from pathlib import Path


def install(config):
    from scripts import cli
    from scripts.download_cache import fetch
    arch = os.environ.get('PROCESSOR_ARCHITEW6432') or os.environ.get('PROCESSOR_ARCHITECTURE') or platform.machine()
    if arch.lower() not in ('amd64', 'x86_64'):
        raise cli.ConfigError('官方 SCO Windows 安装器目前仅提供 AMD64；请选择 Windows x64 或 WSL。')
    powershell = shutil.which('powershell.exe') or shutil.which('pwsh.exe')
    curl = shutil.which('curl.exe')
    if not powershell or not curl or not shutil.which('tar.exe'):
        raise cli.ConfigError('Windows 安装需要 PowerShell、curl.exe 和 tar.exe（较新的 Windows 10/11 自带）。')
    env, executable = cli.runtime(config)
    cache = Path(cli.string_value(config.get('install', {}), 'cache_dir') or 'vendor/sco').expanduser()
    if not cache.is_absolute():
        cache = cli.ROOT / cache
    try:
        installer = fetch('https://sco.sensecore.cn/registry/install.ps1', cache, curl, env)
    except RuntimeError as error:
        raise cli.ConfigError(str(error)) from None
    env['SLAI_WINDOWS_INSTALLER'] = str(installer.resolve())
    # Same scriptblock mechanism as the official PowerShell installation command.
    # Pass paths via environment, not source interpolation; do not change execution policy.
    command = "$ErrorActionPreference='Stop'; & ([scriptblock]::Create([IO.File]::ReadAllText($env:SLAI_WINDOWS_INSTALLER))); exit 0"
    cli.run([powershell, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command], env)
    cli.run([str(executable), 'version'], env)
    try:
        configure_environment(env)
    except (OSError, ValueError):
        raise cli.ConfigError('SCO 已安装，但 Windows 用户环境保存失败，请检查用户注册表权限。') from None
    print('已配置 Windows 用户环境：SCO_HOME、SCO_DATA_HOME、SCO_CONFIG 和 PATH。')
    print('请重新打开终端；如果从 VS Code 启动终端，请重启 VS Code。当前项目菜单可直接继续配置。')


def updated_path(existing, bin_dir):
    import ntpath
    items = existing.split(';') if existing else []
    normalized = ntpath.normcase(ntpath.normpath(bin_dir))
    if not any(ntpath.normcase(ntpath.normpath(item.strip('"'))) == normalized for item in items if item):
        items.append(bin_dir)
    return ';'.join(items)


def configure_environment(env):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
        for name in ('SCO_HOME', 'SCO_DATA_HOME', 'SCO_CONFIG'):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, env[name])
        try:
            current, kind = winreg.QueryValueEx(key, 'Path')
        except FileNotFoundError:
            current, kind = '', winreg.REG_EXPAND_SZ
        if not isinstance(current, str):
            raise ValueError('Windows 用户 PATH 格式无效。')
        value = updated_path(current, str(Path(env['SCO_HOME']) / 'bin'))
        winreg.SetValueEx(key, 'Path', 0, kind, value)
    # Inform Explorer so subsequently opened terminals inherit the new environment.
    import ctypes
    from ctypes import wintypes
    result = ctypes.c_size_t()
    send = ctypes.windll.user32.SendMessageTimeoutW
    send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    send.restype = wintypes.LPARAM
    send(0xffff, 0x001A, 0, 'Environment', 0x0002, 5000, ctypes.byref(result))
