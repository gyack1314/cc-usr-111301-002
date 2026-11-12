"""清点下传资料的纠删分片重建与来源选择原始资产，不解释内容格式。"""
from .io import asset


def summarize(v, root):
    if v['kind'] != 'erasure-shards':
        raise ValueError('资料类别不匹配')
    rows = v['assets']
    if not isinstance(rows, list) or not rows:
        raise ValueError('资产清单为空')
    paths = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('path'), str):
            raise ValueError('资产记录非法')
        paths.append(row['path'])
    if len(paths) != len(set(paths)):
        raise ValueError('重复资产路径')
    total = sum(len(asset(root, r)) for r in rows)
    return {'kind': v['kind'], 'asset_count': len(rows), 'raw_bytes': total,
            'http_records': len(v.get('calls', []))}
