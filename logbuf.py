# -*- coding: utf-8 -*-
"""内存里的日志缓冲，给控制台看最近发生了什么。

控制台跑在另一个线程，所以这里所有操作都用锁保护。
"""
import collections
import logging
import queue
import threading
import time
from typing import *

MAX_ENTRIES = 500


class LogEntry:
    __slots__ = ('timestamp', 'level', 'name', 'message')

    def __init__(self, timestamp: float, level: str, name: str, message: str):
        self.timestamp = timestamp
        self.level = level
        self.name = name
        self.message = message

    def to_dict(self) -> Dict[str, Any]:
        return {
            'time': time.strftime('%H:%M:%S', time.localtime(self.timestamp)),
            'timestamp': self.timestamp,
            'level': self.level,
            'name': self.name,
            'message': self.message,
        }


class RingLogBuffer:
    def __init__(self, max_entries: int = MAX_ENTRIES):
        self._entries: Deque[LogEntry] = collections.deque(maxlen=max_entries)
        self._lock = threading.Lock()
        self._next_seq = 0

    def append(self, entry: LogEntry):
        with self._lock:
            self._entries.append(entry)
            self._next_seq += 1

    def snapshot(self, *, limit: int = 200) -> Dict[str, Any]:
        """取最近 limit 条日志，附带一个自增序号，控制台可用它判断有没有新内容"""
        with self._lock:
            entries = list(self._entries)
            seq = self._next_seq

        if limit > 0:
            entries = entries[-limit:]
        return {
            'seq': seq,
            'entries': [e.to_dict() for e in entries],
        }

    def clear(self):
        with self._lock:
            self._entries.clear()


LOG_BUFFER = RingLogBuffer()


class QueueLogHandler(logging.Handler):
    """把日志塞进一个有界队列，交给后台协程转发给 blivechat。

    用队列是为了不让日志把网络协程拖住；队列满了就直接丢弃，绝不能让"写日志"
    这件事把插件本身卡死。
    """

    def __init__(self, max_size: int = 500, level=logging.INFO):
        super().__init__(level)
        self._queue: "queue.Queue[Tuple[int, str]]" = queue.Queue(maxsize=max_size)
        self.dropped = 0

    def emit(self, record: logging.LogRecord):
        import logging as _logging

        try:
            # blcsdk 自己的日志不要回灌给 blcsdk，否则会自激
            if record.name.startswith('blcsdk'):
                return
            level = record.levelno if record.levelno >= _logging.WARNING else _logging.INFO
            self._queue.put_nowait((level, self.format(record)))
        except queue.Full:
            self.dropped += 1
        except Exception:  # noqa: BLE001 日志处理器自身绝不能抛异常
            pass

    def drain(self, limit: int = 50) -> List[Tuple[int, str]]:
        items: List[Tuple[int, str]] = []
        while len(items) < limit:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return items

    @property
    def pending(self) -> int:
        return self._queue.qsize()


class BufferLogHandler(logging.Handler):
    """把日志同时塞进内存缓冲"""

    def emit(self, record: logging.LogRecord):
        try:
            LOG_BUFFER.append(LogEntry(
                timestamp=record.created,
                level=record.levelname,
                name=record.name,
                message=self.format(record),
            ))
        except Exception:  # noqa: BLE001 日志处理器自身绝不能抛异常
            pass
