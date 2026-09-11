"""Authenticated REST requests and validated pagination for SenseCore services."""
import base64
import email.utils
import hashlib
import hmac
import json
import urllib.request
import urllib.error
from scripts import cli


class RestError(cli.ConfigError):
    def __init__(self, status, body=None):
        self.status, self.body = status, body or {}
        super().__init__(f'接口请求失败（HTTP {status}），请检查当前账号的资源权限。')


def request_json(config, url, *, method="GET", body=None, timeout=30):
    settings = config['sco']
    ak = cli.string_value(settings, 'access_key_id', required=True)
    sk = cli.string_value(settings, 'access_key_secret', required=True)
    date = email.utils.formatdate(usegmt=True)
    signature = base64.b64encode(hmac.new(sk.encode(), ('x-date: ' + date).encode(), hashlib.sha256).digest()).decode()
    auth = f'hmac accesskey="{ak}", algorithm="hmac-sha256", headers="x-date", signature="{signature}"'
    request = urllib.request.Request(url, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={'X-Date': date, 'Authorization': auth, 'Content-Type': 'application/json'})
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


def get_json(config, url, *, timeout=120):
    return request_json(config, url, timeout=timeout)


def pages(fetch, field):
    result, seen, tokens, token, count = [], set(), set(), '1', 0
    for _ in range(1000):
        if token in tokens:
            raise cli.ConfigError('接口分页标识重复，请刷新重试。')
        tokens.add(token)
        data = fetch(token)
        rows = data.get(field) if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise cli.ConfigError('接口列表格式无效。')
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
        total = data.get('total_size', data.get('totalSize'))
        following = data.get('next_page_token', data.get('nextPageToken'))
        if following not in (None, '', '0'):
            token = str(following)
        elif isinstance(total, int) and count < total:
            raise cli.ConfigError('接口未返回完整列表或下一页标识，请刷新重试。')
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
