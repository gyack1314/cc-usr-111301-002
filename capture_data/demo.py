"""演示用来源适配器：把冻结的 mirror URL 映射到本地只读文件。

清单只声明 URL 与分片身份；本适配器按 fixture 的 ``*.sources.json`` 覆盖
个别 URL（可返回坏副本字节或模拟来源失败），其余 URL 按行声明的资产路径读取。
它不执行清单中的任何内容，也不访问网络。
"""
import json
from pathlib import Path


def load_overrides(path, root):
    """可选的 URL 覆盖表；优先清单旁的 ``*.sources.json``，其次 ``mirrors/`` 下的同名表。"""
    candidates = []
    if path is not None:
        from_path = Path(path)
        candidates.append(from_path.with_suffix('.sources.json'))
        candidates.append(Path(root) / 'mirrors' / f'{from_path.stem}.sources.json')
    for candidate in candidates:
        if candidate.exists():
            table = json.loads(candidate.read_text(encoding='utf8'))
            if not isinstance(table, dict):
                raise ValueError('来源覆盖表必须为对象')
            return table
    return {}


def make_adapter(manifest, root, path=None):
    root = Path(root).resolve()

    def read(relative):
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise ValueError('来源路径越界')
        try:
            return target.read_bytes()
        except OSError:
            return None

    url_to_asset = {}
    for stripe in manifest['stripes']:
        for row in stripe['rows']:
            for url in row['mirrors']:
                url_to_asset[url] = row['asset']['path']
    overrides = load_overrides(path, root)

    def adapter(url, ref, remaining):
        spec = overrides.get(url)
        if spec is None:
            relative = url_to_asset.get(url, ref['path'])
        elif isinstance(spec, str):
            relative = spec
        elif isinstance(spec, dict):
            if spec.get('status') == 'fail':
                return None
            relative = spec.get('path')
        else:
            raise ValueError('来源覆盖项非法')
        return read(relative)

    return adapter
