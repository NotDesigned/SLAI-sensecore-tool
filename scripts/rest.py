"""Authenticated read-only REST requests for SenseCore services."""
import base64
import email.utils
import hashlib
import hmac
import json
import urllib.request
import urllib.error
from scripts import cli


class RestError(cli.ConfigError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'REST 查询失败（HTTP {status}），请检查当前账号的资源权限。')


def get_json(config, url):
    settings = config['sco']
    ak = cli.string_value(settings, 'access_key_id', required=True)
    sk = cli.string_value(settings, 'access_key_secret', required=True)
    date = email.utils.formatdate(usegmt=True)
    signature = base64.b64encode(hmac.new(sk.encode(), ('x-date: ' + date).encode(), hashlib.sha256).digest()).decode()
    auth = f'hmac accesskey="{ak}", algorithm="hmac-sha256", headers="x-date", signature="{signature}"'
    request = urllib.request.Request(url, headers={'X-Date': date, 'Authorization': auth})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RestError(error.code) from None
    except (urllib.error.URLError, TimeoutError):
        raise cli.ConfigError('REST 查询超时或网络异常，未能确认查询结果。') from None
    except ValueError:
        raise cli.ConfigError('REST 返回的数据格式无效。') from None
