"""读取冻结清单及验证资产字节身份。"""
import hashlib
import json
from pathlib import Path


def load(path):
    """解析清单并返回 (对象, 清单原始字节的 sha256)。

    扩展拒绝的内容已在 JSON 解码阶段失败：重复键与 NaN/Infinity 常量。
    清单里的任何字符串都只当数据，绝不执行。
    """
    raw = Path(path).read_bytes()
    try:
        value = json.loads(raw.decode('utf8'), parse_constant=_reject_constant,
                           object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('清单不是合法UTF-8 JSON') from exc
    if not isinstance(value, dict):
        raise ValueError('清单必须为对象')
    return value, hashlib.sha256(raw).hexdigest()


def _reject_constant(name):
    raise ValueError('非有限JSON常量')


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'重复JSON键：{key}')
        result[key] = value
    return result


def _within(base, raw_path):
    return raw_path.is_relative_to(base)


def asset(root, ref):
    """按清单声明读取本地资产；路径越界、长度或摘要不符都 ValueError。"""
    base = Path(root).resolve()
    path = (base / ref['path']).resolve()
    if not _within(base, path):
        raise ValueError('资产路径越界')
    raw = path.read_bytes()
    if type(ref['bytes']) is not int or ref['bytes'] < 0:
        raise ValueError('资产长度非法')
    if len(raw) != ref['bytes'] or hashlib.sha256(raw).hexdigest() != ref['sha256']:
        raise ValueError('资产身份不符')
    return raw


class Source:
    """固定版本字节范围读取器，记录实际请求。"""
    def __init__(self, root, ref):
        self.raw = asset(root, ref)
        self.sha256 = ref['sha256']
        self.calls = []

    def read_at(self, start, length):
        if type(start) is not int or type(length) is not int or start < 0 or length < 0 or start + length > len(self.raw):
            raise ValueError('范围越界')
        self.calls.append({'start': start, 'length': length})
        return self.raw[start:start + length]


class Replay:
    """按序返回冻结HTTP记录，不完成采集策略。"""
    def __init__(self, records):
        self.records = records
        self.position = 0
        self.requests = []

    def next(self, method, url, headers=None):
        if self.position >= len(self.records):
            raise EOFError('没有后续响应')
        row = self.records[self.position]
        expected = row['request']
        if (method, url) != (expected['method'], expected['url']):
            raise ValueError('请求不匹配')
        self.requests.append({'method': method, 'url': url, 'headers': dict(headers or {})})
        self.position += 1
        return row['response']
