"""纠删码约定、采集选择、恢复与来源核对的离线检查。"""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from capture_data import gf
from capture_data.demo import make_adapter
from capture_data.io import load
from capture_data.retriever import Collector, Interrupted, ManifestError, retrieve

ROOT = Path('data')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def baseline(prefix, k, length):
    """独立基线：直接用前 k 个数据行拼原文，不经过任何解码。"""
    out = b''
    index = 0
    while len(out) < length:
        out += b''.join((ROOT / f'{prefix}s{index}-r{row}.bin').read_bytes()
                        for row in range(k))
        index += 1
    return out[:length]


class FieldTests(unittest.TestCase):
    def test_systematic_generator_matches_frozen_convention(self):
        g21 = gf.generator(2, 1)
        self.assertEqual(g21, [[1, 0], [0, 1], [244, 245]])
        self.assertEqual(gf.generator(3, 2), [[1, 0, 0], [0, 1, 0], [0, 0, 1],
                                             [7, 9, 15], [7, 8, 14]])

    def test_field_arithmetic(self):
        for value in range(1, 256):
            self.assertEqual(gf.mul(value, gf.inverse(value)), 1)
        self.assertEqual(gf.mul(0x53, 0xca), gf.mul(0xca, 0x53))
        with self.assertRaises(ValueError):
            gf.mul(256, 1)
        with self.assertRaises(ValueError):
            gf.inverse(0)
        for bad in (0, 1 << 8):
            with self.assertRaises(ValueError):
                gf.field_check(bad)

    def test_select_points_rejects_duplicates_and_non_k(self):
        for points in ([0, 0], [0], [0, 1, 2], (0, 3)):
            with self.assertRaises(ValueError):
                gf.select_points(2, 1, list(points))
        with self.assertRaises(ValueError):
            gf.select_points(2, 1, [0, -1])
        with self.assertRaises(ValueError):
            gf.select_points(2, 1, [0, True])  # bool 不是行号

    def test_submatrix_inverse_verified(self):
        a, ainv = gf.select_points(3, 2, [0, 3, 4])
        product = [[gf._xor_reduce(gf.mul(a[i][t], ainv[t][j])
                                  for t in range(3)) for j in range(3)]
                   for i in range(3)]
        self.assertEqual(product, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])

    def test_each_k_subset_recovers_stripe(self):
        g = gf.generator(3, 2)
        blocks = [bytes([1, 2, 3, 4]), bytes([5, 6, 7, 8]), bytes([9, 10, 11, 12])]
        shards = gf.encode(g, blocks, 4)
        for points in ([0, 1, 2], [0, 1, 4], [0, 3, 4], [2, 3, 4], [1, 2, 3]):
            _, ainv = gf.select_points(3, 2, list(points))
            selected = [shards[p] for p in points]
            from capture_data.retriever import _decode
            self.assertEqual(_decode(ainv, selected, 4, '0x11d'), b''.join(blocks))


class CollectorTests(unittest.TestCase):
    def test_normal_recovers_missing_row_and_matches_independent_bytes(self):
        manifest, _ = load(ROOT / 'normal.json')
        report = retrieve(manifest, ROOT)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['file'], b'ORBIT-DATA!')
        self.assertEqual(report['file'], baseline('', 2, 11))
        self.assertEqual(sha(report['file']), manifest['original_sha256'])
        self.assertEqual(report['progress']['stripes_recovered'], 2)
        self.assertEqual(report['progress']['bytes_downloaded'], 16)
        self.assertEqual(report['progress']['budget_remaining'], 84)
        used = {(s['stripe'], s['row']) for s in report['sources']}
        self.assertEqual(used, {(0, 0), (0, 2), (1, 0), (1, 2)})
        self.assertFalse(any(s['row'] == 1 for s in report['sources']))
        self.assertEqual(report['failures'], [])

    def test_duplicate_row_is_not_an_independent_equation(self):
        manifest, _ = load(ROOT / 'duplicate-row-rank.json')
        report = retrieve(manifest, ROOT)
        self.assertEqual(report['status'], 'incomplete')
        self.assertIsNone(report['file'])
        self.assertEqual(report['missing_stripes'], [0, 1])
        for stripe in report['stripes']:
            self.assertEqual(len(stripe['selected_rows']), 1)
        self.assertEqual(report['progress']['bytes_downloaded'], 8)
        self.assertEqual(len(report['rank_failures']), 2)
        for entry in report['rank_failures']:
            self.assertEqual(entry['reason'], 'duplicate-row-not-equation')
            self.assertEqual(len(entry['duplicates']), 1)
        with self.assertRaises(ValueError):
            gf.select_points(2, 1, [0, 0])

    def test_bad_mirror_replaced_and_counted(self):
        manifest, _ = load(ROOT / 'mirror-replacement.json')
        adapter = make_adapter(manifest, ROOT, ROOT / 'mirrors/mirror-replacement.json')
        report = retrieve(manifest, ROOT, adapter)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['file'], b'ORBIT-DATA!')
        self.assertEqual(report['file'],
                         baseline('mirror-replacement-', 2, 11))
        self.assertEqual(report['progress']['bytes_downloaded'], 20)  # 含坏副本
        self.assertEqual(len(report['failures']), 1)
        failure = report['failures'][0]
        self.assertEqual(failure['error'], 'identity-mismatch')
        self.assertEqual(failure['url'], 'https://mirror.test/g1/s0/r0-bad')
        self.assertNotEqual(failure['actual_sha256'], failure['path'] and
                            manifest['stripes'][0]['rows'][0]['asset']['sha256'])
        selected = report['stripes'][0]['selected'][0]
        self.assertEqual(selected['url'], 'https://mirror.test/g1/s0/r0')
        self.assertEqual(selected['sha256'],
                         manifest['stripes'][0]['rows'][0]['asset']['sha256'])
        self.assertEqual(selected['origin'], 'download')
        self.assertNotIn('mirror-replacement-s0-r0.bad.bin',
                         [v['url'] for v in report['cache'].values()])

    def test_source_failure_is_not_identity_mismatch(self):
        manifest, _ = load(ROOT / 'mirror-replacement.json')
        adapter = make_adapter(
            manifest, ROOT, ROOT / 'mirrors/mirror-replacement.json')
        real = adapter
        calls = []

        def flaky(url, ref, remaining):
            calls.append(url)
            if url.endswith('r0-bad'):
                return None
            return real(url, ref, remaining)

        report = retrieve(manifest, ROOT, flaky)
        self.assertEqual(report['status'], 'ok')
        errors = [f['error'] for f in report['failures']]
        self.assertEqual(errors, ['source-failed'])
        self.assertEqual(report['progress']['bytes_downloaded'], 16)

    def test_mixed_generation_is_ignored_not_downloaded(self):
        manifest, _ = load(ROOT / 'mixed-generation.json')
        report = retrieve(manifest, ROOT,
                          make_adapter(manifest, ROOT,
                                       ROOT / 'mixed-generation.json'))
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['missing_stripes'], [0])
        self.assertEqual(report['ignored_rows'],
                         [{'stripe': 0, 'row_index': 2, 'declared_generation': 'g0'}])
        self.assertFalse(any(s['path'] == 's0-r2.bin' for s in report['sources']))
        self.assertEqual(report['progress']['bytes_downloaded'], 12)
        self.assertIsNone(report['file'])

    def test_short_last_stripe_accepts_zero_padding(self):
        manifest, _ = load(ROOT / 'short-last-stripe.json')
        report = retrieve(manifest, ROOT,
                          make_adapter(manifest, ROOT,
                                       ROOT / 'short-last-stripe.json'))
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['file'], b'ORBIT-DATA!')
        self.assertEqual(report['file'], baseline('short-last-stripe-', 2, 11))
        self.assertEqual(len(report['file']), 11)
        self.assertEqual(report['progress']['bytes_downloaded'], 16)

    def test_nonzero_padding_never_yields_file(self):
        manifest, _ = load(ROOT / 'invalid-padding.json')
        report = retrieve(manifest, ROOT,
                          make_adapter(manifest, ROOT,
                                       ROOT / 'invalid-padding.json'))
        self.assertEqual(report['status'], 'invalid-padding')
        self.assertIsNone(report['file'])
        self.assertEqual(report['progress']['stripes_recovered'], 2)

    def test_digest_mismatch_never_yields_file(self):
        manifest, _ = load(ROOT / 'short-last-stripe.json')
        manifest['original_sha256'] = '0' * 64
        report = retrieve(manifest, ROOT,
                          make_adapter(manifest, ROOT,
                                       ROOT / 'short-last-stripe.json'))
        self.assertEqual(report['status'], 'digest-mismatch')
        self.assertIsNone(report['file'])

    def test_budget_counts_actual_bytes_and_stops(self):
        manifest, _ = load(ROOT / 'mirror-replacement.json')
        manifest['download_budget'] = 12
        adapter = make_adapter(manifest, ROOT, ROOT / 'mirrors/mirror-replacement.json')
        report = retrieve(manifest, ROOT, adapter)
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['progress']['bytes_downloaded'], 12)
        self.assertEqual(report['progress']['budget_remaining'], 0)
        self.assertEqual(report['progress']['stripes_recovered'], 1)
        self.assertEqual(report['missing_stripes'], [1])
        self.assertIsNone(report['file'])
        self.assertIn('identity-mismatch',
                      [f['error'] for f in report['failures']])
        self.assertIn('budget-exhausted',
                      [f['error'] for f in report['failures']])

    def test_buffered_stripes_bound(self):
        manifest, _ = load(ROOT / 'normal.json')
        manifest['max_buffered_stripes'] = 1
        report = retrieve(manifest, ROOT)
        self.assertLessEqual(report['progress']['max_active_stripes'], 1)
        for value in (0, -1, True, '1'):
            manifest['max_buffered_stripes'] = value
            with self.assertRaises(ValueError):
                retrieve(manifest, ROOT)

    def test_unknown_row_and_row_parameters_rejected(self):
        manifest, _ = load(ROOT / 'normal.json')
        manifest['stripes'][0]['rows'][0]['row_index'] = 3
        with self.assertRaises(ValueError):
            retrieve(manifest, ROOT)
        manifest, _ = load(ROOT / 'normal.json')
        manifest['stripes'][0]['rows'][0]['k'] = 3
        with self.assertRaises(ValueError):
            retrieve(manifest, ROOT)

    def test_manifest_parameters_rejected(self):
        manifest, _ = load(ROOT / 'normal.json')
        for key, value in (('polynomial', '0x11b'), ('k', 1), ('m', 0),
                           ('matrix_points', [2, 1, 3]),
                           ('block_bytes', 3), ('original_length', 20),
                           ('original_length', 7), ('generation', ''),
                           ('matrix_points', [1, 2])):
            changed = copy.deepcopy(manifest)
            changed[key] = value
            with self.assertRaises(ValueError):
                retrieve(changed, ROOT)

    def test_bool_is_not_an_integer_parameter(self):
        manifest, _ = load(ROOT / 'normal.json')
        for key in ('k', 'm', 'block_bytes', 'original_length', 'download_budget'):
            changed = copy.deepcopy(manifest)
            changed[key] = True
            with self.assertRaises(ValueError):
                retrieve(changed, ROOT)

    def test_resume_reuses_verified_cache_without_redownload(self):
        manifest, _ = load(ROOT / 'short-last-stripe.json')
        base = make_adapter(manifest, ROOT, ROOT / 'short-last-stripe.json')
        interrupted = []

        def stop_second(url, ref, remaining):
            if url.split('/')[-2] == '1':
                interrupted.append(url)
                raise Interrupted()
            return base(url, ref, remaining)

        collector = Collector(manifest, ROOT, stop_second)
        with self.assertRaises(Interrupted):
            collector.run()
        self.assertEqual(interrupted, ['https://mirror.test/g1/1/0'])
        saved = dict(collector.cache)
        self.assertEqual(sorted(k[1] for k in saved),
                         ['short-last-stripe-s0-r0.bin',
                          'short-last-stripe-s0-r1.bin'])

        resumed = Collector(manifest, ROOT, base).resume(saved)
        report = resumed.run()
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['file'], b'ORBIT-DATA!')
        self.assertEqual(report['progress']['bytes_downloaded'], 8)
        self.assertEqual({(p['stripe'], p['row']) for p in report['peer_reuse']},
                         {(0, 0), (0, 1)})
        self.assertEqual({(s['stripe'], s['row']) for s in report['sources']},
                         {(1, 0), (1, 1)})

    def test_resume_drops_unsealed_or_tampered_cache(self):
        manifest, _ = load(ROOT / 'short-last-stripe.json')
        good_key = ('g1', 'short-last-stripe-s0-r0.bin',
                    manifest['stripes'][0]['rows'][0]['asset']['sha256'])
        bad = {good_key: {'bytes': b'\xff\xff\xff\xff', 'url': 'forged',
                          'sha256': good_key[2]}}
        report = Collector(manifest, ROOT,
                           make_adapter(manifest, ROOT,
                                        ROOT / 'short-last-stripe.json')) \
            .resume(bad).run()
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['progress']['bytes_downloaded'], 16)
        self.assertEqual(report['peer_reuse'], [])
        self.assertNotIn('forged', [v['url'] for v in report['cache'].values()])
        self.assertEqual(report['file'], b'ORBIT-DATA!')

    def test_resume_cache_cross_generation_rejected(self):
        manifest, _ = load(ROOT / 'short-last-stripe.json')
        key = ('g0', 'short-last-stripe-s0-r0.bin', 'a' * 64)
        with self.assertRaises(ManifestError):
            Collector(manifest, ROOT,
                      make_adapter(manifest, ROOT,
                                   ROOT / 'short-last-stripe.json')) \
                .resume({key: {'bytes': b'abcd'}})

    def test_manifest_assets_table_conflict_rejects_row(self):
        manifest, _ = load(ROOT / 'normal.json')
        manifest['stripes'][0]['rows'][0]['asset']['sha256'] = 'a' * 64
        report = retrieve(manifest, ROOT,
                          make_adapter(manifest, ROOT,
                                       ROOT / 'normal.json'))
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['missing_stripes'], [0])
        self.assertEqual(report['rejected_rows'],
                         [{'stripe': 0, 'row_index': 0, 'path': 's0-r0.bin',
                           'reason': 'asset-identity-conflict'}])
        manifest, _ = load(ROOT / 'normal.json')
        manifest['stripes'][0]['rows'][0]['asset']['path'] = 'unknown.bin'
        report = retrieve(manifest, ROOT,
                          make_adapter(manifest, ROOT,
                                       ROOT / 'normal.json'))
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['rejected_rows'][0]['reason'],
                         'not-in-manifest-assets')

    def test_partial_result_has_progress_and_gaps_only(self):
        manifest, _ = load(ROOT / 'duplicate-row-rank.json')
        report = retrieve(manifest, ROOT)
        self.assertIsNone(report['file'])
        self.assertEqual(report['progress']['stripes_recovered'], 0)
        self.assertEqual(report['progress']['stripes_pending'], [0, 1])
        self.assertEqual(report['missing_stripes'], [0, 1])
        for stripe in report['stripes']:
            self.assertFalse(stripe['recovered'])
            self.assertEqual(stripe['status'], 'insufficient-rows')

    def test_duplicate_identity_not_refetched_across_stripes(self):
        manifest, _ = load(ROOT / 'normal.json')
        manifest['stripes'][1]['rows'] = [manifest['stripes'][0]['rows'][0]]
        manifest['stripes'][1]['rows'][0]['row_index'] = 0
        report = retrieve(manifest, ROOT)
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['missing_stripes'], [1])
        # s0-r0 已在 stripe 0 下载并核验；stripe 1 直接复用，不重复下载。
        self.assertEqual(report['progress']['bytes_downloaded'], 8)

    def test_budget_across_stripes_returns_gap_not_file(self):
        manifest, _ = load(ROOT / 'short-last-stripe.json')
        manifest['download_budget'] = 8
        adapter = make_adapter(manifest, ROOT,
                               ROOT / 'short-last-stripe.json')
        report = retrieve(manifest, ROOT, adapter)
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['progress']['stripes_recovered'], 1)
        self.assertEqual(report['progress']['bytes_downloaded'], 8)
        self.assertEqual(report['progress']['budget_remaining'], 0)
        self.assertEqual(report['missing_stripes'], [1])
        self.assertIsNone(report['file'])


if __name__ == '__main__':
    unittest.main()
