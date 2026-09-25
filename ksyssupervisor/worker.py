"""Background sensor reading."""

import traceback

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot


class SensorWorker(QObject):
    """Reads every provider on a worker thread and emits the result.

    Reading spawns `sensors` and `nvidia-smi`. Doing that on the GUI thread made
    the whole window stall whenever a call was slow, so it happens here instead
    and only the finished Sample crosses back.
    """

    sample_ready = pyqtSignal(object)   # providers.Sample
    failed = pyqtSignal(str)            # formatted traceback

    def __init__(self, registry, interval_ms=1000):
        super().__init__()
        self.registry = registry
        self._interval_ms = interval_ms
        self._timer = None

    @pyqtSlot()
    def begin(self):
        """Start ticking. Called once the worker is on its own thread."""
        # Created here, not in __init__, so the timer belongs to this thread.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(self._interval_ms)
        self._tick()

    @pyqtSlot(int)
    def set_interval(self, interval_ms):
        self._interval_ms = interval_ms
        if self._timer is not None:
            self._timer.setInterval(interval_ms)

    def _tick(self):
        try:
            self.sample_ready.emit(self.registry.read_all())
        except Exception:
            self.failed.emit(traceback.format_exc())
