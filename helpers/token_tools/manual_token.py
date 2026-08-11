"""Compatibility entry point for the manual OAuth flow.

The previous implementation embedded a one-time authorization code in source.
Keep this filename for operators who already use it, but delegate to the safe
interactive flow instead.
"""

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from helpers.token_tools.get_token_manual import main

if __name__ == "__main__":
    main()
