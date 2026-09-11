"""Shared SOCKS5 configuration and local proxy tools."""
from pathlib import Path
from urllib.parse import quote
from scripts import cli


def validate_destination(host, port):
    import ipaddress
    try:
        ipaddress.ip_address(host)
    except (TypeError, ValueError):
        raise cli.ConfigError('SSH 目标必须是有效 IP 地址。') from None
    if not str(port).isascii() or not str(port).isdecimal() or not 1 <= int(port) <= 65535:
        raise cli.ConfigError('SSH 端口无效。')


def socks5(config):
    import ipaddress
    proxy = config.get('network', {}).get('socks5', {})
    if not isinstance(proxy, dict):
        raise cli.ConfigError('network.socks5 必须是配置表。')
    server = cli.string_value(proxy, 'server').strip()
    if not server:
        return None
    try:
        ipaddress.ip_address(server)
    except ValueError:
        raise cli.ConfigError('SOCKS5 代理 server 必须是有效 IP 地址。') from None
    proxy_port = proxy.get('port', 1080)
    if isinstance(proxy_port, bool) or not isinstance(proxy_port, int) or not 1 <= proxy_port <= 65535:
        raise cli.ConfigError('SOCKS5 代理端口无效。')
    username = cli.string_value(proxy, 'username')
    password = cli.string_value(proxy, 'password')
    if (':' in username or any(c in username + password for c in '\r\n\x00')
            or bool(username) != bool(password)
            or len(username.encode()) > 255 or len(password.encode()) > 255):
        raise cli.ConfigError('SOCKS5 认证信息格式无效；账号密码需同时提供且各不超过 255 字节。')
    return server, proxy_port, username, password


def ncat_args(config, host, port):
    validate_destination(host, port)
    proxy = socks5(config)
    if proxy is None:
        return None
    server, proxy_port, username, password = proxy
    address = f'[{server}]:{proxy_port}' if ':' in server else f'{server}:{proxy_port}'
    args = ['ncat', '--proxy', address, '--proxy-type', 'socks5']
    if username:
        args += ['--proxy-auth', username + ':' + password]
    return [*args, host, str(port)]


def acp_environment(config, env):
    result = env.copy()
    enabled = config.get('network', {}).get('acp_proxy', False)
    if not isinstance(enabled, bool):
        raise cli.ConfigError('network.acp_proxy 必须为布尔值。')
    if enabled:
        proxy = socks5(config)
        if proxy is None:
            raise cli.ConfigError('已启用 ACP 代理，请填写 network.socks5.server。')
        host, port, username, password = proxy
        if ':' in host:
            host = '[' + host + ']'
        auth = quote(username, safe='') + ':' + quote(password, safe='') + '@' if username else ''
        url = f'socks5://{auth}{host}:{port}'
        result.update(HTTP_PROXY=url, HTTPS_PROXY=url, http_proxy=url, https_proxy=url)
    return result


def ncat_install_hint():
    import platform
    import sys
    if sys.platform == 'win32':
        return 'winget install --id Insecure.Nmap -e （或从 https://nmap.org/download.html 安装 Nmap/Ncat）'
    if sys.platform == 'darwin':
        return 'brew install nmap'
    if sys.platform == 'linux':
        try:
            release = platform.freedesktop_os_release()
        except OSError:
            release = {}
        families = {release.get('ID', ''), *release.get('ID_LIKE', '').split()}
        if families & {'debian', 'ubuntu'}:
            return 'sudo apt update && sudo apt install -y ncat'
        if families & {'fedora', 'rhel', 'centos', 'rocky', 'almalinux'}:
            return 'sudo dnf install -y nmap-ncat'
        if families & {'arch', 'manjaro'}:
            return 'sudo pacman -S nmap'
    return '请用当前系统的软件包管理器安装 Ncat（命令名 ncat）。'


def find_ncat():
    import os
    import shutil
    import sys
    executable = shutil.which('ncat')
    if executable or sys.platform != 'win32':
        return executable
    for variable in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        root = os.environ.get(variable)
        if root:
            candidate = Path(root) / 'Nmap' / 'ncat.exe'
            if candidate.is_file():
                return str(candidate)
    return None
