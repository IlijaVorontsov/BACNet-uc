"""``python3 -m uc_controller`` is uc-ctl."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
