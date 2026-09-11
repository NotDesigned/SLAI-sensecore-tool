"""Bounded, unauthenticated SSH banner check over the configured route."""
from scripts.ui import output as print
import queue
import socket
import subprocess
import threading
import time


PROBE_ID = b'SSH-2.0-SLAI_ConnectivityCheck\r\n'


def ssh_banner(stream):
    # RFC 4253 allows informational lines before the protocol identification.
    for _ in range(32):
        line = stream.readline(256)
        if not line:
            return False
        if line.startswith((b'SSH-2.0-', b'SSH-1.99-')) and line.endswith(b'\n'):
            return True
    return False


def check(config, host, port, timeout=8):
    from scripts import network
    args = network.ncat_args(config, host, port)
    if args is None:
        deadline = time.monotonic() + timeout
        try:
            with socket.create_connection((host, int(port)), timeout=timeout) as sock:
                sock.settimeout(max(0.1, deadline - time.monotonic()))
                sock.sendall(PROBE_ID)
                data = b''
                while len(data) < 8192:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False, 'timeout'
                    sock.settimeout(remaining)
                    chunk = sock.recv(min(1024, 8192 - len(data)))
                    if not chunk:
                        break
                    data += chunk
                    if any(line.startswith((b'SSH-2.0-', b'SSH-1.99-')) and len(line) <= 255
                           for line in data.split(b'\n')[:-1]):
                        return True, 'ssh'
                return False, 'no_ssh_banner'
        except (OSError, TimeoutError):
            return False, 'unreachable'
    executable = network.find_ncat()
    if not executable:
        return False, 'ncat_missing'
    result = queue.Queue(maxsize=1)
    try:
        process = subprocess.Popen([executable, *args[1:]], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return False, 'proxy_failed'

    def reader():
        try:
            result.put(ssh_banner(process.stdout))
        except (OSError, ValueError):
            result.put(False)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        process.stdin.write(PROBE_ID)
        process.stdin.flush()
        return (True, 'ssh') if result.get(timeout=timeout) else (False, 'proxy_failed')
    except (queue.Empty, OSError):
        return False, 'timeout'
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        thread.join(timeout=1)
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except OSError:
                pass


def report(config, host, port):
    from scripts import network
    if network.ncat_args(config, host, port):
        network.ensure_ncat()
    route = '经配置的 SOCKS5 代理' if network.ncat_args(config, host, port) else '直连'
    print(f'正在检查 SSH 入口（{route}，最多约 8 秒）……', flush=True)
    success, reason = check(config, host, port)
    if success:
        print('SSH 入口可达：已收到 SSH 协议响应；尚未验证公钥登录。')
    elif reason == 'ncat_missing':
        print('未能检查：本机缺少 ncat。' + network.ncat_install_hint())
    else:
        print('SSH 入口暂不可达或未收到 SSH 响应。可能未处于 SLAI 内网，请参照 README 的“SLAI 内网代理”配置 config.toml。')
        print('若已配置代理，请检查代理可用性；也请确认 CCI 已运行、sshd 已启动及 DNAT 绑定正确。')
    return success
