"""Helper to run a block on the AppKit main thread from any thread."""

import Quartz
from Foundation import NSObject

_pending = set()  # prevent GC of trampolines


class _Trampoline(NSObject):
    _blocks = {}

    def run_(self, _sender):
        block = self._blocks.pop(id(self), None)
        _pending.discard(self)
        if block:
            block()


def run_on_main(block):
    """Schedule block() on the AppKit main thread (async). Safe from any thread."""
    t = _Trampoline.alloc().init()
    _Trampoline._blocks[id(t)] = block
    _pending.add(t)
    t.performSelectorOnMainThread_withObject_waitUntilDone_("run:", None, False)
    Quartz.CFRunLoopWakeUp(Quartz.CFRunLoopGetMain())
