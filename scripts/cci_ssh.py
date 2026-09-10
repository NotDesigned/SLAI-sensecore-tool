"""Public-key-only SSH bootstrap for root Ubuntu CCI containers."""
from pathlib import Path
import shlex
import subprocess

from scripts import cli


def enabled(defaults):
    value = defaults.get('ssh_enabled', True)
    if not isinstance(value, bool):
        raise cli.ConfigError('cci.ssh_enabled 必须为布尔值。')
    return value


def public_key(defaults):
    from scripts.cci import ask, choose
    configured = cli.string_value(defaults, 'ssh_public_key').strip()
    if configured:
        path = Path(configured).expanduser()
    else:
        keys = sorted((Path.home() / '.ssh').glob('*.pub'))
        if keys:
            preferred = next((x for x in keys if x.name == 'id_ed25519.pub'), keys[0])
            path = keys[0] if len(keys) == 1 else choose('SSH 公钥', keys, str, default=preferred)
        else:
            path = Path(ask('SSH 公钥文件路径（.pub）')).expanduser()
    if not path.is_absolute():
        path = cli.ROOT / path
    try:
        raw = path.read_text().strip()
    except UnicodeError:
        raise cli.ConfigError('SSH 公钥文件不是有效文本。') from None
    parts = raw.split()
    if (len(raw.splitlines()) != 1 or len(parts) < 2
            or not parts[0].startswith(('ssh-', 'ecdsa-', 'sk-'))):
        raise cli.ConfigError('请提供单个 OpenSSH 公钥，不能使用私钥或 authorized_keys 选项。')
    try:
        result = subprocess.run(['ssh-keygen', '-l', '-f', str(path)], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise cli.ConfigError('SSH 公钥校验超时，请检查 ssh-keygen。') from None
    if result.returncode:
        raise cli.ConfigError('SSH 公钥格式校验失败。')
    print(f'SSH 公钥：{path}；登录用户 root，容器端口 22。')
    # Comments are unnecessary; never upload a private key.
    return ' '.join(parts[:2])


def startup(key, command=''):
    config = '''Port 22
HostKey /run/slai-ssh/host_ed25519
PidFile /run/slai-ssh/sshd.pid
AuthorizedKeysFile /run/slai-ssh/authorized_keys
PermitRootLogin prohibit-password
PubkeyAuthentication yes
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
UsePAM yes
AllowUsers root
Subsystem sftp internal-sftp
'''
    script = '''set -eu
[ "$(id -u)" = 0 ] || { echo 'SSH bootstrap requires root' >&2; exit 1; }
if [ ! -x /usr/sbin/sshd ]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server
fi
umask 077
mkdir -p /run/sshd /run/slai-ssh
chmod 700 /run/slai-ssh
if [ ! -f /run/slai-ssh/host_ed25519 ]; then
    ssh-keygen -q -t ed25519 -N '' -f /run/slai-ssh/host_ed25519
fi
'''
    script += "printf '%s\\n' " + shlex.quote(key) + ' > /run/slai-ssh/authorized_keys\n'
    script += "printf '%s\\n' " + shlex.quote(config) + ' > /run/slai-ssh/sshd_config\n'
    script += '/usr/sbin/sshd -t -f /run/slai-ssh/sshd_config\n'
    if command.strip() in ('', 'sleep infinity', 'sleep inf'):
        script += 'exec /usr/sbin/sshd -D -e -f /run/slai-ssh/sshd_config\n'
    else:
        script += '/usr/sbin/sshd -f /run/slai-ssh/sshd_config\nexec /bin/sh -c ' + shlex.quote(command) + '\n'
    return script


def validate_destination(host, port):
    import ipaddress
    try:
        ipaddress.ip_address(host)
    except (TypeError, ValueError):
        raise cli.ConfigError('SSH 目标必须是有效 IP 地址。') from None
    if not str(port).isascii() or not str(port).isdecimal() or not 1 <= int(port) <= 65535:
        raise cli.ConfigError('SSH 端口无效。')


def ncat_args(defaults, host, port):
    import ipaddress
    validate_destination(host, port)
    if not isinstance(defaults, dict):
        raise cli.ConfigError('[cci] 必须是配置表。')
    proxy = defaults.get('ssh_proxy', {})
    if not isinstance(proxy, dict):
        raise cli.ConfigError('cci.ssh_proxy 必须是配置表。')
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
    address = f'[{server}]:{proxy_port}' if ':' in server else f'{server}:{proxy_port}'
    args = ['ncat', '--proxy', address, '--proxy-type', 'socks5']
    if username:
        args += ['--proxy-auth', username + ':' + password]
    return [*args, host, str(port)]


def connection_command(defaults, host, port):
    import sys
    proxy_args = ncat_args(defaults, host, port)
    args = ['ssh', '-p', str(port)]
    if proxy_args:
        helper = Path(__file__).with_name('ncat_proxy.py').resolve()
        # OpenSSH expands percent tokens even inside shell-quoted strings.
        proxy = shlex.join([sys.executable.replace('%', '%%'), str(helper).replace('%', '%%'), '%h', '%p'])
        args += ['-o', 'ProxyCommand=' + proxy]
    return shlex.join([*args, 'root@' + host])


def ncat_install_hint():
    import platform
    import sys
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


def show_connection(defaults, host, port, name):
    import shutil
    command = connection_command(defaults, host, port)
    args = shlex.split(command)
    proxy = args[args.index('-o') + 1].removeprefix('ProxyCommand=') if '-o' in args else None
    if proxy and not shutil.which('ncat'):
        print('本机未找到 ncat，请先安装：' + ncat_install_hint())
    print('\nSSH 连接命令（复制执行）：\n' + command)
    print('同一命令也可粘贴到 VS Code 的 Remote-SSH: Add New SSH Host…。')
    print('非默认私钥请在命令中加 -i，或在 SSH 配置中加 IdentityFile。')
    print('连接信息已生成；实际可用性仍需 SSH 登录验证。')
