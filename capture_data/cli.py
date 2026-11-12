"""在边界情况清单上演示采集与擦除恢复，输出可核对的来源与缺口。"""
import argparse
import json
from pathlib import Path

from .io import load
from .retriever import retrieve
from .demo import make_adapter


def _report(name, path):
    manifest, _ = load(path)
    try:
        adapter = make_adapter(manifest, Path(path).parent, path)
        report = retrieve(manifest, Path(path).parent, adapter)
    except ValueError as exc:
        report = {'status': 'rejected', 'reason': str(exc)}
    progress = report.get('progress', {})
    out = {
        'case': name,
        'status': report['status'],
        'file_present': report.get('file') is not None,
        'original_length': len(report['file']) if report.get('file') else None,
        'original_sha256': __import__('hashlib').sha256(report['file']).hexdigest()
        if report.get('file') else None,
        'bytes_downloaded': progress.get('bytes_downloaded'),
        'budget_remaining': progress.get('budget_remaining'),
        'sources': report.get('sources'),
        'failures': report.get('failures'),
        'ignored_rows': report.get('ignored_rows'),
        'rank_failures': report.get('rank_failures'),
        'missing_stripes': report.get('missing_stripes'),
        'reason': report.get('reason'),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['inspect', 'collect'])
    parser.add_argument('file')
    parser.add_argument('--case', default=None)
    args = parser.parse_args()
    if args.command == 'inspect':
        from .domain import summarize
        manifest, digest = load(args.file)
        print(json.dumps({'manifest_sha256': digest,
                          'summary': summarize(manifest, Path(args.file).parent)},
                         ensure_ascii=False, indent=2))
    else:
        _report(args.case or Path(args.file).stem, args.file)


if __name__ == '__main__':
    main()

