# -*- coding: utf-8 -*-
"""房间列表。

blivechat 用 ``RoomKey`` 标识房间（房间ID 或 身份码），插件收到的每条消息
都带 ``extra.room_key`` 和 ``extra.room_id``。这里只做一件事：记住当前有哪些
房间、哪个是主房间，给控制台和“自动回复发到哪个房间”用。
"""
import logging
import threading
import time
from typing import *

logger = logging.getLogger('send-danmu.' + __name__)


class RoomInfo:
    def __init__(self, room_key, room_id: Optional[int] = None):
        self.room_key = room_key
        self.room_id = room_id
        self.init_success = False
        self.first_seen = time.time()
        self.last_seen = time.time()

    @property
    def display_name(self) -> str:
        if self.room_id is not None:
            return str(self.room_id)
        return str(self.room_key)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'roomId': self.room_id,
            'roomKey': str(self.room_key),
            'roomKeyType': int(self.room_key.type) if self.room_key is not None else None,
            'initSuccess': self.init_success,
            'displayName': self.display_name,
        }


class RoomRegistry:
    """线程安全的房间表（事件循环写、控制台线程读）"""

    def __init__(self):
        self._lock = threading.Lock()
        self._rooms: Dict[str, RoomInfo] = {}
        self._primary_id: Optional[int] = None

    def add(self, room_key, room_id: Optional[int] = None, *, init_success: bool = False) -> RoomInfo:
        if room_key is None:
            return None
        key = str(room_key)
        with self._lock:
            info = self._rooms.get(key)
            if info is None:
                info = RoomInfo(room_key, room_id)
                self._rooms[key] = info
            if room_id is not None:
                info.room_id = room_id
            info.init_success = init_success or info.init_success
            info.last_seen = time.time()

            # 第一个成功初始化的房间作为主房间
            if self._primary_id is None and info.room_id is not None and info.init_success:
                self._primary_id = info.room_id
        return info

    def remove(self, room_key) -> None:
        if room_key is None:
            return
        key = str(room_key)
        with self._lock:
            info = self._rooms.pop(key, None)
            if info is not None and info.room_id == self._primary_id:
                self._primary_id = None
                # 退而求其次，随便挑一个还有效的房间
                for other in self._rooms.values():
                    if other.room_id is not None:
                        self._primary_id = other.room_id
                        break

    def set_primary(self, room_id: int):
        with self._lock:
            self._primary_id = room_id

    @property
    def primary_room_id(self) -> Optional[int]:
        with self._lock:
            return self._primary_id

    def primary_room_key(self):
        """主房间的 RoomKey，发插件消息时用来指定房间"""
        with self._lock:
            for info in self._rooms.values():
                if info.room_id is not None and info.room_id == self._primary_id:
                    return info.room_key
            for info in self._rooms.values():
                if info.room_id is not None:
                    return info.room_key
        return None

    def find_room_key(self, room_id: Optional[int]):
        """按房间ID找 RoomKey"""
        if room_id is None:
            return None
        with self._lock:
            for info in self._rooms.values():
                if info.room_id == room_id:
                    return info.room_key
        return None

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [info.to_dict() for info in self._rooms.values()]

    def all_room_ids(self) -> List[int]:
        with self._lock:
            return sorted({info.room_id for info in self._rooms.values() if info.room_id is not None})

    def __len__(self):
        with self._lock:
            return len(self._rooms)
