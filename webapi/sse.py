import json
import queue
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class SSEFrame:
    event: str
    data: dict[str, Any]

    def encode(self) -> bytes:
        payload = json.dumps(self.data, ensure_ascii=False)
        return f"event: {self.event}\ndata: {payload}\n\n".encode("utf-8")


class SSEEmitter:
    def __init__(self, session_id: str, run_id: str | None = None):
        self.session_id = session_id
        self.run_id = run_id
        self._seq = 0

    def event(self, name: str, **payload: Any) -> SSEFrame:
        self._seq += 1
        envelope = {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "seq": self._seq,
            "ts": utc_now_iso(),
        }
        envelope.update(payload)
        return SSEFrame(event=name, data=envelope)


class SSEStream:
    def __init__(self) -> None:
        self._queue: queue.Queue[bytes | None] = queue.Queue()

    def put(self, frame: SSEFrame) -> None:
        self._queue.put(frame.encode())

    def close(self) -> None:
        self._queue.put(None)

    def __iter__(self) -> Iterator[bytes]:
        while True:
            item = self._queue.get()
            if item is None:
                break
            yield item
