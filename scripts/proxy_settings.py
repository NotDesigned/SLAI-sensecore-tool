"""Root-config SOCKS5 editing and bounded protocol/authentication checks."""
import copy
import socket
import time
from scripts import cli, network, ui


def check(config, timeout=8):
    try:
        proxy = network.socks5(config)
    except cli.ConfigError:
        return '配置无效，请重新配置'
    if proxy is None:
        return '未配置（SSH 直连）'
    host, port, username, password = proxy
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((host,port),timeout=timeout) as connection:
            def receive(size):
                data = b''
                while len(data) < size:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    connection.settimeout(remaining)
                    chunk = connection.recv(size-len(data))
                    if not chunk:
                        raise ConnectionError
                    data += chunk
                return data
            method = 2 if username else 0
            connection.settimeout(max(.01,deadline-time.monotonic()))
            connection.sendall(bytes([5,1,method]))
            reply = receive(2)
            if reply[0] != 5:
                return '不可用（不是 SOCKS5 响应）'
            if reply[1] != method:
                return '不可用（认证方式不匹配）'
            if username:
                user, secret = username.encode(), password.encode()
                connection.sendall(bytes([1,len(user)])+user+bytes([len(secret)])+secret)
                if receive(2) != b'\x01\x00':
                    return '不可用（用户名或密码认证失败）'
                return '可用（握手、认证通过）'
            return '可用（握手通过，无需认证）'
    except TimeoutError:
        return '不可用（连接超时）'
    except OSError:
        return '不可用（连接失败或中断）'


def status():
    try:
        result = check(cli.load_config(for_setup=True))
    except (cli.ConfigError,OSError):
        result = '配置无法读取'
    return 'SOCKS5：' + result + ' · ' + time.strftime('%H:%M:%S')


def configure():
    config = cli.load_config(for_setup=True)
    original = config.get('network', {})
    existing = original.get('socks5', {})
    if not isinstance(existing,dict):
        raise cli.ConfigError('network.socks5 必须是配置表，请检查 config.toml。')
    actions = ['配置代理', '关闭代理'] if existing.get('server') else ['配置代理']
    action = ui.choose('SOCKS5 代理设置',actions,default='配置代理')
    if action == '关闭代理':
        updates = dict(socks5={'server':'','port':1080,'username':'','password':''})
        if original.get('acp_proxy'):
            updates['acp_proxy'] = False
        cli.save_config_updates('network',updates,original)
        ui.output('已关闭 SOCKS5；SSH 和 ACP 恢复使用系统网络。')
        return
    server = ui.ask('SOCKS5 代理 IP 地址',cli.string_value(existing,'server'))
    port = ui.number('SOCKS5 端口',existing.get('port',1080))
    mode = ui.choose('认证方式',['用户名和密码','无需认证'],
                     default='用户名和密码' if existing.get('username') else '无需认证')
    username = password = ''
    if mode == '用户名和密码':
        username = ui.ask('代理用户名',cli.string_value(existing,'username'))
        same_endpoint = server == existing.get('server') and port == existing.get('port',1080) and username == existing.get('username')
        keep = same_endpoint and existing.get('password') and ui.choose('代理密码',
            ['保留已保存的密码','输入新密码'],default='保留已保存的密码') == '保留已保存的密码'
        if keep:
            password = existing['password']
        else:
            import getpass
            import warnings
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error',getpass.GetPassWarning)
                    password = ui.secret('代理密码（隐藏输入）')
            except getpass.GetPassWarning:
                raise cli.ConfigError('当前终端不能隐藏密码，请在交互终端配置代理。') from None
    values = dict(server=server,port=port,username=username,password=password)
    candidate = copy.deepcopy(config)
    candidate.setdefault('network',{})['socks5'] = values
    network.socks5(candidate)
    cli.save_config_updates('network',{'socks5':values},original)
    ui.output('SOCKS5 已保存。SSH 会使用该代理；ACP 保持原有代理开关。')
