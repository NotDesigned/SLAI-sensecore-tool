"""Authenticated REST requests and validated pagination for SenseCore services."""
import base64
import email.utils
import hashlib
import hmac
import json
import urllib.request
import urllib.error
import urllib.parse
import ssl
from scripts import cli


class RestError(cli.ConfigError):
    def __init__(self, status, body=None):
        self.status, self.body = status, body or {}
        import re
        reason = next((d.get('reason') for d in (self.body.get('details') or [])
                       if isinstance(d, dict) and isinstance(d.get('reason'), str)
                       and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', d['reason'])), '') if isinstance(self.body, dict) else ''
        super().__init__(f'接口请求失败（HTTP {status}' + (f'，{reason}' if reason else '') + '），请检查参数、权限或资源状态。')


class IncompletePage(cli.ConfigError):
    pass


def request_json(config, url, *, method="GET", body=None, timeout=30, proxy=None):
    settings = config['account']
    ak = cli.string_value(settings, 'access_key_id', required=True)
    sk = cli.string_value(settings, 'access_key_secret', required=True)
    date = email.utils.formatdate(usegmt=True)
    signature = base64.b64encode(hmac.new(sk.encode(), ('x-date: ' + date).encode(), hashlib.sha256).digest()).decode()
    auth = f'hmac accesskey="{ak}", algorithm="hmac-sha256", headers="x-date", signature="{signature}"'
    request = urllib.request.Request(url, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={'X-Date': date, 'Authorization': auth, 'Content-Type': 'application/json'})
    if proxy:
        return socks_json(request, proxy, timeout)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        try:
            detail = json.load(error)
        except ValueError:
            detail = {}
        raise RestError(error.code, detail) from None
    except (urllib.error.URLError, TimeoutError):
        message = '查询失败，请检查网络后重试。' if method == 'GET' else '写入结果未知，请刷新列表确认，勿重复提交。'
        raise cli.ConfigError(message) from None
    except ValueError:
        message = '接口响应格式无效。' if method == 'GET' else '接口响应格式无效，写入结果未知；请刷新列表确认。'
        raise cli.ConfigError(message) from None


def socks_json(request, proxy, timeout):
    """Explicit SOCKS route, verified TLS, no retries or direct fallback."""
    from urllib3.contrib.socks import SOCKSProxyManager
    from urllib3.exceptions import HTTPError
    from urllib3.util import Timeout
    try:
        with SOCKSProxyManager(proxy, ssl_context=ssl.create_default_context()) as manager:
            response = manager.request(request.method, request.full_url, body=request.data,
                headers=dict(request.header_items()), timeout=Timeout(total=timeout), retries=False, redirect=False)
            raw = response.data
            if not 200 <= response.status < 300:
                try:
                    detail = json.loads(raw)
                except ValueError:
                    detail = {}
                raise RestError(response.status, detail)
            return json.loads(raw) if raw else {}
    except (HTTPError, OSError):
        message = ('SOCKS5 接口查询失败，请检查代理和网络。' if request.method == 'GET' else
                   'SOCKS5 接口写入结果未知，请刷新列表确认，勿重复提交。')
        raise cli.ConfigError(message) from None
    except ValueError:
        message = ('接口响应格式无效。' if request.method == 'GET' else
                   '接口响应格式无效，写入结果未知；请刷新列表确认。')
        raise cli.ConfigError(message) from None


def get_json(config, url, *, timeout=120, proxy=None):
    if proxy:
        return request_json(config, url, timeout=timeout, proxy=proxy)
    return request_json(config, url, timeout=timeout)


def query_url(base, values):
    return base + '?' + urllib.parse.urlencode(values)


def list_page(data, field):
    if not isinstance(data, dict) or not isinstance(data.get(field), list):
        raise cli.ConfigError('接口列表格式无效。')
    rows = data[field]
    if any(not isinstance(row, dict) or not isinstance(row.get('name'), str) or not row['name'] for row in rows):
        raise cli.ConfigError('接口返回缺少名称的记录。')
    total = data.get('total_size', data.get('totalSize'))
    if total is not None and (not isinstance(total, int) or isinstance(total, bool) or total < len(rows)):
        raise cli.ConfigError('接口列表总数无效。')
    token = data.get('next_page_token', data.get('nextPageToken'))
    if isinstance(token, bool) or token is not None and not isinstance(token, (str, int)):
        raise cli.ConfigError('接口分页标识无效。')
    return rows, total, str(token) if token not in (None, '', '0', 0) else ''


def pages(fetch, field, *, numbered=False):
    result, seen, tokens, token, count = [], set(), set(), '1', 0
    for _ in range(1000):
        if token in tokens:
            raise cli.ConfigError('接口分页标识重复，请刷新重试。')
        tokens.add(token)
        data = fetch(token)
        rows, total, following = list_page(data, field)
        fresh = 0
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('name'), str) or not row['name']:
                raise cli.ConfigError('接口返回缺少名称的记录。')
            key = row.get('uid') or row.get('rid') or row.get('id') or row['name']
            if key not in seen:
                seen.add(key)
                result.append(row)
                fresh += 1
        if rows and not fresh:
            raise cli.ConfigError('接口重复返回整页，请刷新重试。')
        count += len(rows)
        if following not in (None, '', '0'):
            token = str(following)
        elif isinstance(total, int) and count < total:
            if numbered and rows and str(token).isdecimal():
                token = str(int(token) + 1)
            else:
                raise IncompletePage('接口未返回完整列表或下一页标识，请刷新重试。')
        else:
            return result
    raise cli.ConfigError('列表超过分页上限，请缩小查询范围。')


def identity_id(data):
    import uuid
    value = data.get('id') if isinstance(data, dict) else None
    try:
        if not uuid.UUID(value).int:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise cli.ConfigError('无法确认当前用户身份，未执行资源操作。') from None
    return value
