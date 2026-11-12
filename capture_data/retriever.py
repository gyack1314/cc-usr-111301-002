"""按冻结清单采集、核验分片，必要时做擦除恢复。

约定（见 docs/data-contract.md）：GF(256)/0x11d；系统码 G 的前 k 行为单位阵；
shard[row] = G[row] · data_block。只在已通过 sha256 的分片之间做擦除恢复，
不把校验失败的分片当成可纠正的噪声。下载适配器由调用者注入；本模块不执行
清单中的任何命令，也不自行访问网络。
"""
import hashlib
from pathlib import Path

from . import gf

_HEX = set('0123456789abcdef')


class ManifestError(ValueError):
    """清单声明本身不可信，或恢复缓存越出本次采集范围。"""


class Interrupted(Exception):
    """调用方主动中止采集；已核验缓存可由新采集器续用。"""


def _sha_ok(value):
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX for c in value)



def _bytes_ok(value):
    return type(value) is int and value >= 0


def _resolve_row(row, parent_generation, k, m, block, polynomial):
    """校验行声明。显式他代返回 None（不下载、不入方程）；未知/非法行 ValueError。"""
    if not isinstance(row, dict):
        raise ValueError('行记录非法')
    row_index = row.get('row_index')
    if type(row_index) is not int or not 0 <= row_index < k + m:
        raise ValueError('未知或非法 row_index')
    generation = row.get('generation', parent_generation)
    if not isinstance(generation, str) or not generation:
        raise ValueError('行 generation 非法')
    ref = row.get('asset')
    if not isinstance(ref, dict) or not isinstance(ref.get('path'), str):
        raise ValueError('行资产非法')
    if not _bytes_ok(ref.get('bytes')) or not _sha_ok(ref.get('sha256')):
        raise ValueError('行资产身份声明非法')
    if ref['bytes'] != block:
        raise ValueError('分片长度与 block_bytes 不符')
    mirrors = row.get('mirrors')
    if not isinstance(mirrors, list) or not mirrors or \
            not all(isinstance(u, str) and u for u in mirrors):
        raise ValueError('镜像列表非法')
    if len(set(mirrors)) != len(mirrors):
        raise ValueError('镜像重复')
    available = row.get('available')
    if available is not None and type(available) is not bool:
        raise ValueError('available 非法')
    for name, expected in (('k', k), ('m', m), ('block_bytes', block),
                           ('polynomial', polynomial)):
        if name in row and row[name] != expected:
            raise ValueError('行内参数与清单不符')
    if generation != parent_generation:
        return None  # 他代分片即使同名同摘要也不进入本代方程
    return {'row_index': row_index, 'asset': ref, 'mirrors': tuple(mirrors),
            'available': True if available is None else available}


def _structural(manifest):
    """核对编码参数、条带容量与原文长度；不读取任何分片内容。"""
    if not isinstance(manifest, dict):
        raise ValueError('清单必须为对象')
    if manifest.get('kind') != 'erasure-shards':
        raise ValueError('资料类别不匹配')
    generation = manifest.get('generation')
    if not isinstance(generation, str) or not generation:
        raise ValueError('generation 非法')
    k, m = manifest.get('k'), manifest.get('m')
    polynomial = manifest.get('polynomial')
    gf.field_check(polynomial)
    gf.generator(k, m, polynomial)
    if manifest.get('matrix_points') != list(range(1, k + m + 1)):
        raise ValueError('matrix_points 与冻结行号不符')
    block = manifest.get('block_bytes')
    if not _bytes_ok(block) or block == 0:
        raise ValueError('block_bytes 非法')
    length = manifest.get('original_length')
    if not _bytes_ok(length) or length == 0:
        raise ValueError('original_length 非法')
    if not _sha_ok(manifest.get('original_sha256')):
        raise ValueError('original_sha256 非法')
    stripes = manifest.get('stripes')
    if not isinstance(stripes, list) or not stripes:
        raise ValueError('条带为空')
    for position, stripe in enumerate(stripes):
        if not isinstance(stripe, dict) or stripe.get('index') != position:
            raise ValueError('条带编号不连续或重复')
        rows = stripe.get('rows')
        if not isinstance(rows, list) or not rows:
            raise ValueError('条带行记录非法')
        for row in rows:
            _resolve_row(row, generation, k, m, block, polynomial)
    # 每个条带含 k 个数据块；最后条带允许零填充，但不得缺少原文所需条带。
    if len(stripes) * k * block < length or (len(stripes) - 1) * k * block >= length:
        raise ValueError('条带数量与原文长度不符')
    if not (type(manifest.get('max_buffered_stripes')) is int and
            manifest['max_buffered_stripes'] >= 1):
        raise ValueError('max_buffered_stripes 非法')
    if not _bytes_ok(manifest.get('download_budget')):
        raise ValueError('download_budget 非法')
    return generation, block, length, stripes


def _decode(ainv, values, block, polynomial):
    """x = A^-1 · y；A 为选中行的生成矩阵子矩阵，逐字节求值。"""
    rows = len(values)
    out = bytearray(rows * block)
    for position in range(block):
        ys = [values[i][position] for i in range(rows)]
        for i in range(rows):
            acc = 0
            for t in range(rows):
                acc ^= gf.mul(ainv[i][t], ys[t], polynomial)
            out[i * block + position] = acc
    return bytes(out)


def _local_backend(root, ref):
    """默认适配器：返回本地资产原始字节；读不到即来源失败。"""
    base = Path(root).resolve()
    path = (base / ref['path']).resolve()
    if not path.is_relative_to(base):
        raise ManifestError('资产路径越界')
    try:
        return path.read_bytes()
    except OSError:
        return None


class Collector:
    """一次采集。预算与已核验缓存跨条带共享，来源失败逐镜像记录。"""

    def __init__(self, manifest, root=None, adapter=None):
        self.manifest = manifest
        self.generation, self.block, self.length, self.stripes = _structural(manifest)
        self.k, self.m = manifest['k'], manifest['m']
        self.polynomial = manifest['polynomial']
        if adapter is None:
            if root is None:
                raise ValueError('缺少来源适配器')
            adapter = lambda url, ref, remaining: _local_backend(root, ref)
        self.adapter = adapter
        # 清单全局资产表：row 内的 inline 资产声明必须与之一致，不另立身份。
        self.assets = {}
        for entry in manifest.get('assets', []):
            if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
                raise ManifestError('assets 记录非法')
            key = entry['path']
            if key in self.assets and self.assets[key] != entry:
                raise ManifestError('同一路径有多份资产声明')
            self.assets[key] = entry
        self.budget = manifest['download_budget']
        self.downloaded = 0
        self.halted = False
        self.sources = []
        self.failures = []
        self.ignored = []
        self.rank_failures = []
        self.peer_reuse = []
        self.rejected_rows = []
        self.fetched_keys = set()   # 本次 run 已尝试过下载的 (gen,path,sha)
        # (generation, path, sha256) -> {'bytes','url','origin'}；只存已核验内容。
        self.cache = {}

    # ---- 缓存 -------------------------------------------------------
    def _check_declared_asset(self, stripe_index, row):
        """行内资产必须与清单全局 assets 一致；冲突时拒绝该行并记录原因。"""
        if not self.assets:
            return  # 精简清单只带行内身份，仍会在下载后逐份核对摘要
        expected = self.assets.get(row['asset']['path'])
        if expected is None:
            row['available'] = False
            self.rejected_rows.append({'stripe': stripe_index,
                                        'row_index': row['row_index'],
                                        'path': row['asset']['path'],
                                        'reason': 'not-in-manifest-assets'})
            return
        if expected.get('sha256') != row['asset']['sha256'] or \
                expected.get('bytes') != row['asset']['bytes']:
            row['available'] = False
            self.rejected_rows.append({'stripe': stripe_index,
                                        'row_index': row['row_index'],
                                        'path': row['asset']['path'],
                                        'reason': 'asset-identity-conflict'})

    def resume(self, supplied):
        """续采用：录用上一轮缓存，逐项重算摘要；未封存/不符的临时块丢弃。"""
        if supplied is None:
            return self
        for key, entry in supplied.items():
            if not (isinstance(key, tuple) and len(key) == 3):
                raise ManifestError('恢复缓存键格式非法')
            generation, path, digest = key
            if generation != self.generation or not isinstance(path, str):
                raise ManifestError('恢复缓存跨代或路径非法')
            if isinstance(entry, dict):
                body = entry.get('bytes')
                digest = entry.get('sha256', digest)
                url = entry.get('url', 'resume')
            else:
                body, url = entry, 'resume'
            if not isinstance(body, bytes):
                raise ManifestError('恢复缓存内容非法')
            if hashlib.sha256(body).hexdigest() != digest:
                continue  # 复核不过：不是可信分片，不进方程
            self.cache[(generation, path, digest)] = {
                'bytes': body, 'url': url, 'origin': 'resume'}
        return self

    # ---- 下载 -------------------------------------------------------
    def _remaining(self):
        return self.budget - self.downloaded

    def _fetch(self, stripe_index, row, key):
        """逐镜像尝试；坏副本按实际字节计入预算并记来源失败，核验通过才入缓存。"""
        self.fetched_keys.add(key)
        for url in row['mirrors']:
            if self._remaining() < row['asset']['bytes']:
                self.failures.append({'stripe': stripe_index, 'row': row['row_index'],
                                      'path': row['asset']['path'], 'url': url,
                                      'error': 'budget-exhausted'})
                self.halted = True
                return None
            try:
                body = self.adapter(url, row['asset'], self._remaining())
            except (ManifestError, Interrupted):
                raise
            except Exception as exc:  # 来源不可用不等同于分片校验错
                self.failures.append({'stripe': stripe_index, 'row': row['row_index'],
                                      'path': row['asset']['path'], 'url': url,
                                      'error': 'source-failed',
                                      'detail': type(exc).__name__})
                continue
            if body is None:
                self.failures.append({'stripe': stripe_index, 'row': row['row_index'],
                                      'path': row['asset']['path'], 'url': url,
                                      'error': 'source-failed'})
                continue
            if not isinstance(body, bytes):
                raise ValueError('下载适配器必须返回 bytes 或 None')
            self.downloaded += len(body)
            actual = hashlib.sha256(body).hexdigest()
            identity_ok = len(body) == row['asset']['bytes'] and actual == row['asset']['sha256']
            if self.downloaded > self.budget:
                self.failures.append({'stripe': stripe_index, 'row': row['row_index'],
                                      'path': row['asset']['path'], 'url': url,
                                      'bytes': len(body), 'actual_sha256': actual,
                                      'error': 'identity-mismatch+budget-exhausted'
                                      if not identity_ok else 'budget-exhausted'})
                self.halted = True
                return None
            if not identity_ok:
                self.failures.append({'stripe': stripe_index, 'row': row['row_index'],
                                      'path': row['asset']['path'], 'url': url,
                                      'bytes': len(body), 'actual_sha256': actual,
                                      'error': 'identity-mismatch'})
                continue
            self.cache[key] = {'bytes': body, 'url': url, 'origin': 'download'}
            self.sources.append({'stripe': stripe_index, 'row': row['row_index'],
                                 'path': row['asset']['path'], 'sha256': key[2],
                                 'url': url, 'bytes': len(body), 'origin': 'download'})
            return body
        return None

    # ---- 选择与恢复 -------------------------------------------------
    def _select(self, stripe_index, rows):
        """确定规则：已核验内容优先，再按 row_index、资产路径排序；同 row_index 只取一次。

        某个 row_index 的来源全部失败时不再占用方程名额，继续尝试其他行；
        重复 row_index 不构成独立方程。只选数据行时仍靠填充与原文件摘要核对。
        """
        limit = 3 * (self.m + 1)  # 坏副本重试上限，防同一条带无限换源

        def order(row):
            cached = (self.generation, row['asset']['path'], row['asset']['sha256']) in self.cache
            return (0 if cached else 1, row['row_index'], row['asset']['path'],
                    row['asset']['sha256'])

        chosen, points, seen, attempts = [], [], set(), 0
        duplicates = []
        for row in sorted(rows, key=order):
            if len(points) == self.k:
                break
            if row['row_index'] in seen:
                duplicates.append({'row_index': row['row_index'],
                                   'path': row['asset']['path'],
                                   'sha256': row['asset']['sha256']})
                continue
            key = (self.generation, row['asset']['path'], row['asset']['sha256'])
            entry = self.cache.get(key)
            if not row['available']:
                # 本轮来源不可用：唯有已核验缓存仍可作方程，绝不发起下载。
                if entry is None:
                    continue
            elif entry is None:
                if self.halted or attempts >= limit or key in self.fetched_keys:
                    continue  # 本次 run 已验证该身份不可得，不重复下载
                attempts += 1
                self._fetch(stripe_index, row, key)
                entry = self.cache.get(key)
            if entry is None:
                seen.add(row['row_index'])  # 该行所有副本都不可用，换别的行
                continue
            chosen.append({'row_index': row['row_index'], 'path': row['asset']['path'],
                           'sha256': key[2], 'url': entry['url'],
                           'origin': entry['origin'], 'bytes': len(entry['bytes'])})
            if entry['origin'] == 'resume':
                self.peer_reuse.append({'stripe': stripe_index, 'row': row['row_index'],
                                        'path': key[1], 'sha256': key[2]})
            points.append(row['row_index'])
            seen.add(row['row_index'])
        if len(points) < self.k:
            if duplicates:
                self.rank_failures.append({'stripe': stripe_index,
                                           'reason': 'duplicate-row-not-equation',
                                           'duplicates': duplicates})
            return None, chosen, points, 'insufficient-rows'
        try:
            _, ainv = gf.select_points(self.k, self.m, points, self.polynomial)
        except ValueError as exc:
            self.rank_failures.append({'stripe': stripe_index, 'points': list(points),
                                       'error': str(exc)})
            return None, chosen, points, 'rank-deficient'
        values = [self.cache[(self.generation, c['path'], c['sha256'])]['bytes']
                  for c in chosen]
        return (ainv, points, values), chosen, points, None

    def run(self):
        stripe_records, decoded, pending = [], [], []
        active = peak = 0
        for stripe in self.stripes:
            index = stripe['index']
            rows = []
            for row in stripe['rows']:
                resolved = _resolve_row(row, self.generation, self.k, self.m,
                                        self.block, self.polynomial)
                if resolved is None:
                    self.ignored.append({'stripe': index, 'row_index': row.get('row_index'),
                                         'declared_generation': row.get('generation')})
                    continue
                self._check_declared_asset(index, resolved)
                rows.append(resolved)
            if not rows:
                raise ValueError('条带无本代合法行')
            if active >= self.manifest['max_buffered_stripes']:
                raise ValueError('同时暂存条带超限')
            active += 1
            peak = max(peak, active)
            plan, chosen, points, reason = self._select(index, rows)
            if plan is None:
                pending.append(index)
                status = 'budget-exhausted' if self.halted else reason
                stripe_records.append({'stripe': index, 'status': status,
                                       'selected_rows': points, 'selected': chosen,
                                       'recovered': False})
            else:
                ainv, points, values = plan
                decoded.append((index, _decode(ainv, values, self.block, self.polynomial)))
                stripe_records.append({'stripe': index, 'status': 'recovered',
                                       'selected_rows': points, 'selected': chosen,
                                       'recovered': True})
            active -= 1  # 该条带方程与工作集就地释放，再进入下一条带

        progress = {'stripes_recovered': len(decoded), 'stripes_total': len(self.stripes),
                    'stripes_pending': pending, 'bytes_downloaded': self.downloaded,
                    'budget_remaining': self._remaining(), 'max_active_stripes': peak}
        base = {'manifest_generation': self.generation, 'progress': progress,
                'sources': self.sources, 'failures': self.failures,
                'ignored_rows': self.ignored, 'rejected_rows': self.rejected_rows,
                'peer_reuse': self.peer_reuse,
                'rank_failures': self.rank_failures, 'stripes': stripe_records,
                'cache': {key: {'url': item['url'], 'origin': item['origin'],
                                'sha256': key[2], 'bytes': item['bytes']}
                          for key, item in self.cache.items()},
                'file': None}
        if pending:
            base.update(status='incomplete', missing_stripes=pending)
            return base
        joined = b''.join(data for _, data in decoded)
        tail = joined[self.length:]
        if any(tail):
            base.update(status='invalid-padding')
            return base
        file_bytes = joined[:self.length]
        digest = hashlib.sha256(file_bytes).hexdigest()
        if digest != self.manifest['original_sha256']:
            base.update(status='digest-mismatch', original_sha256=digest)
            return base
        base.update(status='ok', file=file_bytes)
        return base


def retrieve(manifest, root=None, adapter=None):
    """执行一次冻结清单采集，返回进度、来源与仅在完整校验后出现的原文件。"""
    return Collector(manifest, root, adapter).run()
