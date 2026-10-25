"""Reproducible isolated file-executor scaling and warm no-op measurement."""
import argparse
import json
from pathlib import Path
import platform
import statistics
import tempfile
import time

from claude_config.core.transaction import FileChange, FilePlan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--counts', default='1000,5000,10000')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    samples = []
    for count in map(int, args.counts.split(',')):
        for repetition in range(args.repeats):
            with tempfile.TemporaryDirectory(prefix='claude-config-benchmark-') as directory:
                root = Path(directory)
                changes = [FileChange(root / 'files' / str(n), b'benchmark content\n', 'fixture', lambda _: None) for n in range(count)]
                started = time.monotonic()
                plan = FilePlan(changes)
                planned = time.monotonic()
                report = plan.apply(root / 'state')
                applied = time.monotonic()
                repeat = FilePlan(changes)
                replanned = time.monotonic()
                repeated = repeat.apply(root / 'state')
                finished = time.monotonic()
                journal = Path(report['recovery'])
                noop = Path(repeated['recovery'])
                samples.append({'count': count, 'repetition': repetition + 1,
                                'plan_seconds': planned - started, 'apply_seconds': applied - planned,
                                'warm_plan_seconds': replanned - applied, 'noop_seconds': finished - replanned,
                                'event_bytes': journal.with_name('events.jsonl').stat().st_size,
                                'checkpoint_bytes': journal.stat().st_size,
                                'noop_event_lines': len(noop.with_name('events.jsonl').read_bytes().splitlines())})
                print(json.dumps(samples[-1]), flush=True)
    medians = {count: {key: statistics.median(x[key] for x in samples if x['count'] == count)
                       for key in ('plan_seconds', 'apply_seconds', 'warm_plan_seconds', 'noop_seconds', 'event_bytes')}
               for count in sorted({x['count'] for x in samples})}
    result = {'platform': platform.platform(), 'python': platform.python_version(),
              'cache_method': 'new paths then immediate repeat; operating-system caches are not forcibly flushed',
              'samples': samples, 'medians': medians}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'medians': medians}))


if __name__ == '__main__':
    main()
