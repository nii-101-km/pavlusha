#!/usr/bin/env python3
"""Launch the Pavlusha command-line runtime."""

from pavlusha_agent.cli import main
from pavlusha_agent.process_supervision import termination_signals


if __name__ == "__main__":
    with termination_signals():
        raise SystemExit(main())
