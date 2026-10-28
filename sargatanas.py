#!/usr/bin/env python3
"""Sargatanas launcher.

Thin entry point so the tool also runs from a clone without installing:

    ./sargatanas list
"""

import sys

from sargatanas.cli import main

if __name__ == "__main__":
    sys.exit(main())
