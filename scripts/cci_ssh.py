"""Public-key-only SSH bootstrap for root Ubuntu CCI containers."""
from scripts.ui import output as print
from pathlib import Path
import shlex
import subprocess

from scripts import cli, network


def enabled(defaults):
    value = defaults.get('ssh_enabled', True)
    if not isinstance(value, bool):
        raise cli.ConfigError('cci.ssh_enabled 必须为布尔值。')
    return value


def public_key(defaults):
    from scripts.ui import ask, choose
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
        raw = path.read_text(encoding='utf-8').strip()
    except UnicodeError:
        raise cli.ConfigError('SSH 公钥文件不是有效文本。') from None
    parts = raw.split()
    if (len(raw.splitlines()) != 1 or len(parts) < 2
            or not parts[0].startswith(('ssh-', 'ecdsa-', 'sk-'))):
        raise cli.ConfigError('请提供单个 OpenSSH 公钥，不能使用私钥或 authorized_keys 选项。')
    try:
        result = subprocess.run(['ssh-keygen', '-l', '-f', str(path)], capture_output=True, text=True, timeout=10, encoding='utf-8')
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
    if not command.strip():
        script += 'exec /usr/sbin/sshd -D -e -f /run/slai-ssh/sshd_config\n'
    else:
        script += '/usr/sbin/sshd -f /run/slai-ssh/sshd_config\nexec /bin/sh -c ' + shlex.quote(command) + '\n'
    return script


def connection_command(config, host, port):
    import sys
    proxy_args = network.ncat_args(config, host, port)
    args = ['ssh', '-p', str(port)]
    if proxy_args:
        helper = Path(__file__).with_name('ncat_proxy.py').resolve()
        # OpenSSH expands percent tokens even inside shell-quoted strings.
        if sys.platform == 'win32':
            if any(c in sys.executable + str(helper) for c in '%\r\n\"'):
                raise cli.ConfigError('Windows SSH 代理路径不能含百分号、换行或双引号，请移动项目后重试。')
            proxy = f'"{sys.executable}" "{helper}" %h %p'
        else:
            proxy = shlex.join([sys.executable.replace('%', '%%'), str(helper).replace('%', '%%'), '%h', '%p'])
        args += ['-o', 'ProxyCommand=' + proxy]
    from scripts.commands import format_command
    return format_command([*args, 'root@' + host])


def show_connection(config, host, port, name):
    import sys
    command = connection_command(config, host, port)
    from scripts.ssh_probe import report
    report(config, host, port)
    from scripts import ui
    if ui.active():
        ui.show_text('SSH 连接命令', command, hint='终端可直接执行。VS Code：先选择 Remote-SSH: Add New SSH Host… 添加此命令，再用 Connect to Host… 选择保存的主机。')
        return
    if sys.platform == 'win32':
        print('\nSSH 连接命令（PowerShell 7.3+ 复制执行）：\n' + command)
        print('Windows PowerShell 5.1 的嵌套引号行为不同，请在 PowerShell 7.3+ 执行。')
    else:
        print('\nSSH 连接命令（复制执行）：\n' + command)
    print('VS Code：在 Remote-SSH: Add New SSH Host… 中粘贴此命令，保存后再通过 Connect to Host… 选择主机。')
    print('Connect to Host… 的主机输入框只接受主机名或 user@host，不能直接粘贴整条命令。')
    print('非默认私钥请在命令中加 -i，或在 SSH 配置中加 IdentityFile。')
    print('连接信息已生成；实际可用性仍需 SSH 登录验证。')
