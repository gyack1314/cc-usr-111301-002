"""GF(256) 与冻结约定的系统 Reed-Solomon 生成矩阵。

约定（见 docs/data-contract.md）：primitive polynomial 0x11d；V 的第 i 行第 j 列为
(i+1)^j（GF 内幂，i 从 0 到 k+m-1，j 从 0 到 k-1）；G = V * (V 前 k 行)^-1，
行号不重排。因此 G 的前 k 行为单位阵：行 r<k 的分片就是原文数据块。
"""
FIELDS = {'0x11d': 0x11d}


def field_check(polynomial):
    if polynomial not in FIELDS:
        raise ValueError('不支持的GF多项式')
    return FIELDS[polynomial]


def _element(value):
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError('GF元素越界')
    return value


def mul(a, b, polynomial='0x11d'):
    poly = field_check(polynomial)
    a, b = _element(a), _element(b)
    r = 0
    while b:
        if b & 1:
            r ^= a
        a <<= 1
        if a & 0x100:
            a ^= poly
        b >>= 1
    return r & 0xff


def power(a, exponent, polynomial='0x11d'):
    a = _element(a)
    if type(exponent) is not int or exponent < 0:
        raise ValueError('非法幂次')
    r, base = 1, a
    while exponent:
        if exponent & 1:
            r = mul(r, base, polynomial)
        base = mul(base, base, polynomial)
        exponent >>= 1
    return r


def inverse(a, polynomial='0x11d'):
    if _element(a) == 0:
        raise ValueError('零元不可逆')
    return power(a, 254, polynomial)


def _identity(n):
    return [[1 if i == j else 0 for j in range(n)] for i in range(n)]


def invert(matrix, polynomial='0x11d'):
    """完全消元（列主元）求逆；奇异返回 None，由调用方决定拒绝方式。"""
    n = len(matrix)
    if any(len(row) != n for row in matrix):
        raise ValueError('矩阵必须为方阵')
    aug = [list(row) + _identity(n)[i] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = next((i for i in range(col, n) if aug[i][col]), None)
        if pivot is None:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        lead = inverse(aug[col][col], polynomial)
        aug[col] = [mul(lead, x, polynomial) for x in aug[col]]
        for i in range(n):
            if i != col and aug[i][col]:
                factor = aug[i][col]
                aug[i] = [x ^ mul(factor, y, polynomial)
                          for x, y in zip(aug[i], aug[col])]
    return [row[n:] for row in aug]


def _matmul(a, b, polynomial='0x11d'):
    inner = len(b)
    return [[_xor_reduce(mul(a[i][t], b[t][j], polynomial) for t in range(inner))
             for j in range(len(b[0]))] for i in range(len(a))]


def _xor_reduce(values):
    acc = 0
    for value in values:
        acc ^= value
    return acc


def generator(k, m, polynomial='0x11d'):
    """冻结生成矩阵；参数非法或前 k 行奇异时 ValueError。"""
    field_check(polynomial)
    if type(k) is not int or type(m) is not int or k < 1 or m < 1 or k + m > 255:
        raise ValueError('非法纠删参数')
    v = [[power(i + 1, j, polynomial) for j in range(k)] for i in range(k + m)]
    head_inv = invert(v[:k], polynomial)
    if head_inv is None:
        raise ValueError('V前k行不可逆')
    g = _matmul(v, head_inv, polynomial)
    if g[:k] != _identity(k):
        raise ValueError('生成矩阵非系统码')
    return g


def select_points(k, m, points, polynomial='0x11d'):
    """取 G 的指定行作为方程组并实际求逆。

    返回 (行子矩阵, 逆矩阵)。点集不足、重复、越界或子矩阵奇异时 ValueError；
    绝不把相同 row_index 的重复分片当成独立方程。
    """
    field_check(polynomial)
    if type(k) is not int or type(m) is not int or k < 1 or m < 1 or k + m > 255:
        raise ValueError('非法纠删参数')
    if not isinstance(points, (list, tuple)) or isinstance(points, str) or \
            len(points) != k:
        raise ValueError('必须有k个方程')
    if len(set(points)) != k:
        raise ValueError('方程行不独立')
    for point in points:
        if type(point) is not int or not 0 <= point < k + m:
            raise ValueError('行号越界')
    g = generator(k, m, polynomial)
    a = [list(g[point]) for point in points]
    ainv = invert(a, polynomial)
    if ainv is None:
        raise ValueError('所选子矩阵不可逆')
    return a, ainv


def encode(g, blocks, block_bytes, polynomial='0x11d'):
    """按 G 编码一个条带的 k 个数据块，返回全部 k+m 个分片块。"""
    if len(blocks) != len(g[0]):
        raise ValueError('数据块数量与k不符')
    out = []
    for row in g:
        shard = bytearray(block_bytes)
        for position in range(block_bytes):
            acc = 0
            for j, block in enumerate(blocks):
                if row[j]:
                    acc ^= mul(row[j], block[position], polynomial)
            shard[position] = acc
        out.append(bytes(shard))
    return out
