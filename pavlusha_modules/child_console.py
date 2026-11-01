"""Run a child in its terminal and record a confirmed process exit."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    marker = Path(sys.argv[1])
    result = subprocess.run(sys.argv[2:], check=False)
    temporary = marker.with_suffix('.tmp')
    temporary.write_text(json.dumps({'exit_code': result.returncode}))
    os.replace(temporary, marker)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
