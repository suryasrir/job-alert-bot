"""Job Alert Bot entry point.

    python jobs.py run          # discover, score, store and notify (default)
    python jobs.py list         # show stored matches
    python jobs.py --help       # all commands
"""

import sys

from job_agent.cli import main

if __name__ == "__main__":
    sys.exit(main())
