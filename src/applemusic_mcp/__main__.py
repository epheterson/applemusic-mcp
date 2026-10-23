"""Entry point for running as a module: python -m applemusic_mcp [serve --level …]

With no arguments it serves, as it always has; with arguments it is the CLI,
so `serve --level` is honored here too."""

import sys

from .cli import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("serve")
    main()
