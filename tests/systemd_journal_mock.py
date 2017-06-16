# mocks for systemd.journal

import sys
from unittest.mock import NonCallableMock

class Reader:
    def __init__(self, path): pass
    def __enter__(self): return _ReaderCm()
    def __exit__(self, *args): pass

class _ReaderCm:
    def seek_cursor(self, cursor): pass
    def this_boot(self): pass
    def seek_head(self): pass
    def __iter__(self): yield from ()

systemd = NonCallableMock(
    journal=NonCallableMock(
        Reader=Reader,
    )
)
sys.modules['systemd'] = systemd
sys.modules['systemd.journal'] = systemd.journal
