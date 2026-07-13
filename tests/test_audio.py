import pytest

from dalistener.audio import CaptureManager
from dalistener.models import CaptureMode, CaptureSelection


class PartiallyFailingDevices:
    def resolve_microphone(self, _device_id, _follow_default):
        return object()

    def resolve_output_loopback(self, _device_id, _follow_default):
        raise RuntimeError("loopback unavailable")


def test_partial_device_resolution_leaves_capture_manager_clean():
    manager = CaptureManager(devices=PartiallyFailingDevices())
    selection = CaptureSelection(mode=CaptureMode.BOTH)
    with pytest.raises(RuntimeError, match="loopback unavailable"):
        manager.start(selection, lambda _frame: None, lambda _source, _text: None)
    assert manager.workers == {}
    manager.stop()
