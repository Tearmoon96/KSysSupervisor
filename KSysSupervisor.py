#!/usr/bin/env python3
"""KSysSupervisor - a HWMonitor-style hardware monitor for Linux.

Thin entry point. The implementation lives in the ksyssupervisor package next to
this file, so that running this script, the installed launcher and a PyInstaller
build all go through the same code.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ksyssupervisor.app import main

if __name__ == "__main__":
    sys.exit(main())
