"""İptal edilebilir bekleme yardımcıları."""
import threading
import time

def stoppable_wait(stop_event: threading.Event | None, seconds: float, tick: float = 0.5) -> bool:
    """Stop edilebilir bekleme. Stop gelirse True, normal biterse False döner."""
    if seconds <= 0:
        return False
    if stop_event is None:
        time.sleep(seconds)
        return False
    end = time.monotonic() + seconds
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return False
        if stop_event.wait(min(tick, remaining)):
            return True
