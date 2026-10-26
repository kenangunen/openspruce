"""Background worker that talks to the Roborock robot.

Runs its own asyncio loop on a daemon thread. The UI never touches library
objects directly: it reads plain-dict snapshots via ``Backend.snapshot`` and
asks for actions via ``Backend.run(action_name, *args)``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
from typing import Any, Callable

from roborock.const import (
    FILTER_REPLACE_TIME,
    MAIN_BRUSH_REPLACE_TIME,
    SENSOR_DIRTY_REPLACE_TIME,
    SIDE_BRUSH_REPLACE_TIME,
)
from roborock.data import UserData
from roborock.data.v1.v1_code_mappings import RoborockInCleaning
from roborock.devices.device_manager import UserParams, create_device_manager
from roborock.devices.file_cache import FileCache
from roborock.devices.traits.v1.consumeable import ConsumableAttribute
from roborock.roborock_typing import RoborockCommand

from .config import CACHE_FILE, MAP_FILE, PREFS_FILE, USER_FILE

_LOGGER = logging.getLogger(__name__)

POLL_ACTIVE = 8      # seconds, while cleaning / returning
POLL_IDLE = 30       # seconds, while docked
SLOW_REFRESH = 300   # seconds, consumables / dnd / volume / routines
MAP_ACTIVE = 30      # seconds between map refreshes while cleaning
MAP_IDLE = 900

ACTIVE_STATES = {
    "starting", "cleaning", "returning_home", "spot_cleaning", "docking",
    "going_to_target", "zoned_cleaning", "segment_cleaning", "mapping",
    "going_to_wash_the_mop", "remote_control_active", "manual_mode",
}
RUNNING_STATES = ACTIVE_STATES - {"returning_home", "docking"}


def _load_prefs() -> dict:
    try:
        return json.loads(PREFS_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_prefs(prefs: dict) -> None:
    try:
        PREFS_FILE.write_text(json.dumps(prefs, indent=2))
    except OSError:
        pass


def _pct_left(used: int | None, total: int) -> int | None:
    if used is None:
        return None
    return max(0, min(100, round((total - used) * 100 / total)))


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


class Backend:
    def __init__(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="roborock")
        self._lock = threading.Lock()
        self._snapshot: dict = {"phase": "starting", "message": "Bağlanıyor..."}
        self._wake = None  # asyncio.Event, created inside loop

        self.prefs = _load_prefs()
        self._cache: FileCache | None = None
        self._manager = None
        self._devices: list = []
        self._device = None
        self._v1 = None
        self._rooms: list[tuple[int, str]] = []
        self._routines: list[tuple[int, str]] = []
        self._last_slow = 0.0
        self._last_error: str | None = None
        self._map_uri: str | None = None
        self._map_time = 0.0

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        self._thread.start()

    @property
    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snapshot)

    def run(self, action: str, *args: Any) -> None:
        """Schedule an action coroutine (method named ``a_<action>``)."""
        fn = getattr(self, f"a_{action}")

        async def wrapper() -> None:
            try:
                if self._v1 is None:
                    raise RuntimeError("Robot henüz bağlı değil")
                await fn(*args)
                self._last_error = None
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Action %s failed", action)
                self._last_error = f"{action}: {err}"
            await asyncio.sleep(1.5)
            await self._refresh_fast()
            self._publish()

        asyncio.run_coroutine_threadsafe(wrapper(), self._loop)

    def set_pref(self, key: str, value: Any) -> None:
        self.prefs[key] = value
        _save_prefs(self.prefs)

    def republish(self) -> None:
        """Rebuild the snapshot (e.g. after a pref change) from the loop thread."""
        self._loop.call_soon_threadsafe(self._publish)

    # ---------------------------------------------------------------- internals
    def _thread_main(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    def _set_phase(self, phase: str, message: str) -> None:
        with self._lock:
            self._snapshot = {"phase": phase, "message": message}
        self._on_change()

    async def _main(self) -> None:
        self._wake = asyncio.Event()
        while True:
            try:
                await self._connect()
                break
            except FileNotFoundError:
                self._set_phase("login", "Giriş yapılmamış — Terminal'de giriş betiğini çalıştırın")
                await asyncio.sleep(15)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Connect failed")
                self._set_phase("error", f"Bağlantı hatası: {err}")
                await asyncio.sleep(30)

        while True:
            try:
                await self._refresh_fast()
                if time.monotonic() - self._last_slow > SLOW_REFRESH:
                    await self._refresh_slow()
                active = self._snapshot.get("state_key") in ACTIVE_STATES
                if time.monotonic() - self._map_time > (MAP_ACTIVE if active else MAP_IDLE):
                    await self._refresh_map()
                self._publish()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Refresh failed: %s", err)
                self._last_error = f"Yenileme: {err}"
                self._publish()
            state = self._snapshot.get("state_key")
            delay = POLL_ACTIVE if state in ACTIVE_STATES else POLL_IDLE
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _connect(self) -> None:
        if not USER_FILE.exists():
            raise FileNotFoundError
        data = json.loads(USER_FILE.read_text())
        user_params = UserParams(username=data["email"], user_data=UserData.from_dict(data["user_data"]))
        # A half-written cache file (e.g. two copies of the app running at once)
        # makes pickle fail with "Ran out of input": drop it and rediscover.
        if CACHE_FILE.exists():
            try:
                import pickle
                pickle.loads(CACHE_FILE.read_bytes())
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Cache file unreadable, deleting it")
                CACHE_FILE.unlink(missing_ok=True)
        self._cache = FileCache(CACHE_FILE)
        self._set_phase("starting", "Cihazlar bulunuyor...")
        self._manager = await create_device_manager(user_params, cache=self._cache)
        devices = await self._manager.get_devices()
        self._devices = [d for d in devices if d.v1_properties is not None]
        if not self._devices:
            raise RuntimeError("Hesapta desteklenen bir süpürge bulunamadı")
        await self._select_device(self.prefs.get("device_id"))

    async def _select_device(self, duid: str | None) -> None:
        dev = next((d for d in self._devices if d.duid == duid), self._devices[0])
        self._device = dev
        self._v1 = dev.v1_properties
        self.set_pref("device_id", dev.duid)
        self._set_phase("starting", f"{dev.name} bağlanıyor...")
        for _ in range(60):
            if dev.is_connected:
                break
            await asyncio.sleep(1)
        # live push updates (state, battery...) from the robot
        _safe(lambda: self._v1.status.add_update_listener(self._on_push))
        self._last_slow = 0
        self._rooms = []
        self._routines = []
        self._map_uri = None
        await self._refresh_fast()
        await self._refresh_slow()
        self._publish()
        await self._refresh_map()
        self._publish()

    def _on_push(self) -> None:
        # Called inside the loop thread.
        self._publish()

    async def _refresh_fast(self) -> None:
        if self._v1 is None:
            return
        await self._v1.status.refresh()

    async def _refresh_slow(self) -> None:
        v1 = self._v1
        self._last_slow = time.monotonic()
        for name in ("consumables", "dnd", "sound_volume", "child_lock", "mop_dryer"):
            trait = getattr(v1, name, None)
            if trait is None:
                continue
            try:
                await trait.refresh()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("refresh %s failed: %s", name, err)
        try:
            await v1.rooms.refresh()
            self._rooms = sorted(
                ((r.segment_id, r.name) for r in (v1.rooms.rooms or [])), key=lambda x: x[1].lower()
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("rooms refresh failed: %s", err)
        try:
            routines = await v1.routines.get_routines()
            self._routines = [(r.id, r.name) for r in routines]
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("routines failed: %s", err)
        if self._cache is not None:
            try:
                await self._cache.flush()
            except Exception:  # noqa: BLE001
                pass

    async def _refresh_map(self) -> None:
        self._map_time = time.monotonic()
        try:
            await self._v1.map_content.refresh()
            img = self._v1.map_content.image_content
            if img:
                MAP_FILE.write_bytes(img)
                self._map_uri = "data:image/png;base64," + base64.b64encode(img).decode()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("map refresh failed: %s", err)

    def _publish(self) -> None:
        if self._v1 is None:
            return
        try:
            snap = self._build_snapshot()
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("snapshot failed")
            snap = {"phase": "error", "message": f"Durum okunamadı: {err}"}
        with self._lock:
            self._snapshot = snap
        self._on_change()

    def _build_snapshot(self) -> dict:
        dev, v1 = self._device, self._v1
        st = v1.status
        feats = v1.device_features
        dock = getattr(feats, "dock_features", None)

        state_key = st.state.name if st.state is not None else None
        snap: dict[str, Any] = {
            "phase": "ready",
            "devices": [(d.duid, d.name) for d in self._devices],
            "device_id": dev.duid,
            "name": dev.name,
            "model": _safe(lambda: dev.product.model, ""),
            "connected": _safe(lambda: dev.is_connected, False),
            "local": _safe(lambda: dev.is_local_connected, False),
            "state_key": state_key,
            "battery": st.battery,
            "charging": _safe(lambda: st.is_battery_charging, False),
            "area_m2": _safe(lambda: st.square_meter_clean_area),
            "clean_min": round(st.clean_time / 60) if st.clean_time else None,
            "clean_percent": st.clean_percent,
            "error_key": st.error_code.name if st.error_code else None,
            "error_code": int(st.error_code) if st.error_code else 0,
            "dock_error_key": st.dock_error_status.name if st.dock_error_status else None,
            "dock_error_code": int(st.dock_error_status) if st.dock_error_status else 0,
            "dry_status": st.dry_status,
            "unfinished": (st.in_cleaning.name
                           if st.in_cleaning not in (None, RoborockInCleaning.complete) else None),
            "last_error": self._last_error,
            "rooms": list(self._rooms),
            "routines": list(self._routines),
            "selected_rooms": [s for s in self.prefs.get("selected_rooms", []) if s in {r[0] for r in self._rooms}],
            "repeat": int(self.prefs.get("repeat", 1)),
            "map": self._map_uri,
        }

        snap["clean_modes"] = [m.value for m in _safe(lambda: st.cleaning_mode_options, [])]
        snap["clean_mode"] = _safe(lambda: st.current_cleaning_mode_name)
        snap["fan_options"] = [(m.code, m.value) for m in _safe(lambda: st.fan_speed_options, [])]
        snap["fan"] = st.fan_power
        snap["water_options"] = [(c, n) for c, n in _safe(lambda: st.water_mode_mapping, {}).items()]
        snap["water"] = st.water_box_mode
        snap["mop_options"] = [(m.code, m.value) for m in _safe(lambda: st.mop_route_options, [])]
        snap["mop"] = st.mop_mode

        c = v1.consumables
        cons = [
            ("main_brush_work_time", "Ana fırça", _pct_left(c.main_brush_work_time, MAIN_BRUSH_REPLACE_TIME),
             _safe(lambda: round(c.main_brush_time_left / 3600))),
            ("side_brush_work_time", "Yan fırça", _pct_left(c.side_brush_work_time, SIDE_BRUSH_REPLACE_TIME),
             _safe(lambda: round(c.side_brush_time_left / 3600))),
            ("filter_work_time", "Filtre", _pct_left(c.filter_work_time, FILTER_REPLACE_TIME),
             _safe(lambda: round(c.filter_time_left / 3600))),
            ("sensor_dirty_time", "Sensörler", _pct_left(c.sensor_dirty_time, SENSOR_DIRTY_REPLACE_TIME),
             _safe(lambda: round(c.sensor_time_left / 3600))),
        ]
        snap["consumables"] = [x for x in cons if x[2] is not None]

        snap["has_dock"] = bool(dock and dock.has_dock)
        snap["can_collect"] = bool(dock and dock.has_dock and not dock.is_pure_wash)
        snap["can_wash"] = bool(dock and dock.has_dock and not dock.is_pure_collect)
        snap["can_dry"] = v1.mop_dryer is not None
        snap["auto_dry"] = _safe(lambda: v1.mop_dryer.is_on) if v1.mop_dryer else None
        snap["dnd"] = _safe(lambda: v1.dnd.is_on)
        snap["volume"] = _safe(lambda: v1.sound_volume.volume)
        snap["child_lock"] = _safe(lambda: v1.child_lock.is_on) if v1.child_lock else None
        return snap

    async def _cmd(self, command: RoborockCommand | str, params: Any = None) -> Any:
        return await self._v1.command.send(command, params)

    # ----------------------------------------------------------------- actions
    async def a_refresh(self) -> None:
        await self._refresh_slow()
        await self._refresh_map()

    async def a_refresh_fast(self) -> None:
        pass  # run() refreshes status after every action

    async def a_start(self) -> None:
        """Start, or resume the unfinished task with the matching command.

        ``app_start`` always starts a whole-home clean, so a paused (or docked
        mid-task) room or zone clean must be resumed with its own command.
        """
        await self._v1.status.refresh()
        unfinished = self._v1.status.in_cleaning
        if unfinished == RoborockInCleaning.segment_clean_not_complete:
            await self._cmd(RoborockCommand.RESUME_SEGMENT_CLEAN)
        elif unfinished == RoborockInCleaning.zone_clean_not_complete:
            await self._cmd(RoborockCommand.RESUME_ZONED_CLEAN)
        else:
            await self._cmd(RoborockCommand.APP_START)

    async def a_pause(self) -> None:
        await self._cmd(RoborockCommand.APP_PAUSE)

    async def a_stop(self) -> None:
        await self._cmd(RoborockCommand.APP_STOP)

    async def a_dock(self) -> None:
        await self._cmd(RoborockCommand.APP_CHARGE)

    async def a_find(self) -> None:
        await self._cmd(RoborockCommand.FIND_ME)

    async def a_spot(self) -> None:
        await self._cmd(RoborockCommand.APP_SPOT)

    async def a_clean_rooms(self, segment_ids: list[int], repeat: int) -> None:
        try:
            await self._cmd(RoborockCommand.APP_SEGMENT_CLEAN, [{"segments": segment_ids, "repeat": repeat}])
        except Exception:  # noqa: BLE001 - older firmware format
            await self._cmd(RoborockCommand.APP_SEGMENT_CLEAN, segment_ids)

    async def a_set_clean_mode(self, mode: str) -> None:
        await self._v1.status.set_cleaning_mode(mode)

    async def a_set_fan(self, code: int) -> None:
        await self._cmd(RoborockCommand.SET_CUSTOM_MODE, [code])

    async def a_set_water(self, code: int) -> None:
        await self._cmd(RoborockCommand.SET_WATER_BOX_CUSTOM_MODE, [code])

    async def a_set_mop(self, code: int) -> None:
        await self._cmd(RoborockCommand.SET_MOP_MODE, [code])

    async def a_collect_dust(self) -> None:
        await self._cmd(RoborockCommand.APP_START_COLLECT_DUST)

    async def a_start_wash(self) -> None:
        await self._cmd(RoborockCommand.APP_START_WASH)

    async def a_stop_wash(self) -> None:
        await self._cmd(RoborockCommand.APP_STOP_WASH)

    async def a_start_dry(self) -> None:
        await self._v1.mop_dryer.start_dry()

    async def a_stop_dry(self) -> None:
        await self._v1.mop_dryer.stop_dry()

    async def a_toggle_auto_dry(self) -> None:
        d = self._v1.mop_dryer
        await (d.disable() if d.is_on else d.enable())

    async def a_toggle_dnd(self) -> None:
        d = self._v1.dnd
        await (d.disable() if d.is_on else d.enable())
        await d.refresh()

    async def a_toggle_child_lock(self) -> None:
        d = self._v1.child_lock
        await (d.disable() if d.is_on else d.enable())
        await d.refresh()

    async def a_set_volume(self, volume: int) -> None:
        await self._v1.sound_volume.set_volume(volume)

    async def a_reset_consumable(self, key: str) -> None:
        await self._v1.consumables.reset_consumable(ConsumableAttribute.from_str(key))

    async def a_resolve_error(self) -> None:
        await self._v1.status.resolve_error()

    async def a_run_routine(self, routine_id: int) -> None:
        await self._v1.routines.execute_routine(routine_id)

    async def a_select_device(self, duid: str) -> None:
        await self._select_device(duid)

    async def a_refresh_map(self) -> None:
        await self._refresh_map()

    async def a_save_map(self, then: Callable[[str], None]) -> None:
        await self._v1.map_content.refresh()
        img = self._v1.map_content.image_content
        if not img:
            raise RuntimeError("Harita alınamadı")
        MAP_FILE.write_bytes(img)
        then(str(MAP_FILE))
