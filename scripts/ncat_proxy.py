"""SSH ProxyCommand entry point: read local config and hand the stream to Ncat."""
import os
from pathlib import Path
import subprocess
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import cli, cci_ssh


def proxy_args(config, host, port):
    args = cci_ssh.ncat_args(config.get('cci', {}), host, port)
    if args is None:
        raise cli.ConfigError('未配置 cci.ssh_proxy.server。')
    return args


def main():
    try:
        if len(sys.argv) != 3:
            raise cli.ConfigError('代理需要目标 IP 和端口。')
        executable = cci_ssh.find_ncat()
        if not executable:
            raise cli.ConfigError('未找到 ncat：' + cci_ssh.ncat_install_hint())
        args = proxy_args(cli.load_config(), sys.argv[1], sys.argv[2])
        if sys.platform == 'win32':
            return subprocess.run([executable, *args[1:]], check=False).returncode
        os.execv(executable, [executable, *args[1:]])
    except cli.ConfigError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print('Ncat 代理启动失败，请检查本机 ncat 安装及项目 config.toml 中的代理配置。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
