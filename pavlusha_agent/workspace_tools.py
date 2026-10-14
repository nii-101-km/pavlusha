#!/usr/bin/python3
"""Small shell helpers, executed inside the existing Worker sandbox."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import uuid

WORK = Path('/work')


def work_path(value):
    path = Path(value)
    path = (path if path.is_absolute() else WORK / path).resolve()
    if not path.is_relative_to(WORK.resolve()):
        raise ValueError('path must stay inside /work')
    return path


def text(path):
    return path.read_bytes().decode('utf-8')


def run_check(command, timeout=60, tail_chars=4000):
    if not command or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('provide a command and a finite positive timeout')
    directory = work_path('.scratch/checks')
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / (uuid.uuid4().hex + '.log')
    started = time.monotonic()
    timed_out = False
    with log.open('wb') as output:
        try:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            output.write(str(exc).encode('utf-8'))
            code = 127
        else:
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                # Stop descendants too; keep the report bounded even if they ignore TERM.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                code = 124
    with log.open('rb') as output:
        output.seek(max(0, log.stat().st_size - tail_chars))
        excerpt = output.read(tail_chars).decode('utf-8', errors='replace')
    report = {'status': 'timed_out' if timed_out else ('passed' if code == 0 else 'failed'),
              'exit_code': code, 'duration_seconds': round(time.monotonic() - started, 3),
              'log': str(log), 'output_bytes': log.stat().st_size, 'tail': excerpt}
    return report, code


def replace_text(path, old_file, new_file, count=1):
    if count < 1:
        raise ValueError('count must be positive')
    path, old_file, new_file = map(work_path, (path, old_file, new_file))
    source, old, new = text(path), text(old_file), text(new_file)
    if not old or source.count(old) != count:
        raise ValueError('old text must occur exactly the requested number of times; file unchanged')
    result = source.replace(old, new)
    # Publish a complete file, preserving its mode and exact UTF-8/newline bytes.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(result.encode('utf-8'))
            os.fchmod(output.fileno(), path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {'path': str(path), 'replacements': count}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    check = commands.add_parser('run-check', help='run an argv command; persist full output and return its exit code')
    check.add_argument('--timeout', type=float, default=60)
    check.add_argument('command', nargs=argparse.REMAINDER)
    read = commands.add_parser('read', help='read up to 200 numbered lines by count or inclusive end')
    read.add_argument('path')
    read.add_argument('--start', type=int, default=1)
    read_range = read.add_mutually_exclusive_group()
    read_range.add_argument('--count', type=int, help='number of lines to read (1..200; default: 200)')
    read_range.add_argument('--end', type=int, help='inclusive last line; alternative to --count')
    find = commands.add_parser('find', help='search a UTF-8 file using a regular expression')
    find.add_argument('path')
    find.add_argument('pattern')
    find.add_argument('--limit', type=int, default=50)
    replace = commands.add_parser('replace', help='replace exact text only when the occurrence count matches')
    replace.add_argument('path')
    replace.add_argument('--old-file', required=True)
    replace.add_argument('--new-file', required=True)
    replace.add_argument('--count', type=int, default=1)
    args = parser.parse_args(argv)
    try:
        if args.action == 'run-check':
            command = args.command[1:] if args.command[:1] == ['--'] else args.command
            report, code = run_check(command, args.timeout)
            print(json.dumps(report, ensure_ascii=False))
            return code if code >= 0 else 128 - code
        if args.action == 'replace':
            print(json.dumps(replace_text(args.path, args.old_file, args.new_file, args.count)))
        elif args.action == 'read':
            if args.start < 1:
                raise ValueError('read --start must be positive')
            count = args.count if args.count is not None else (
                args.end - args.start + 1 if args.end is not None else 200)
            if count < 1:
                raise ValueError('read requires a positive --count or --end >= --start')
            if count > 200:
                raise ValueError(f'read requested {count} lines; maximum is 200. '
                                 f'Use --start {args.start} --count 200 '
                                 f'or --start {args.start} --end {args.start + 199}')
            end = args.start + count - 1
            for i, line in enumerate(text(work_path(args.path)).splitlines(), 1):
                if args.start <= i <= end:
                    print(f'{i}: {line}')
        else:
            if not 1 <= args.limit <= 200:
                raise ValueError('limit must be 1..200')
            pattern = re.compile(args.pattern)
            matches = 0
            for i, line in enumerate(text(work_path(args.path)).splitlines(), 1):
                if pattern.search(line):
                    print(f'{i}: {line}')
                    matches += 1
                    if matches == args.limit:
                        break
        return 0
    except (OSError, ValueError, UnicodeError, re.error) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
