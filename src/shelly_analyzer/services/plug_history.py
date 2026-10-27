"""Energy history for Shelly plugs and other switch devices without an EMData log.

Owns: (1) ``PlugEnergyRecorder`` - turns the live ``Switch.GetStatus`` energy
counter (``aenergy.total``, Wh) into rows of the ``samples`` table; (2) the
Shelly Cloud import of device names and past consumption (``run_cloud_import``).
Needs: the Database (``storage.db``), the live samples of ``switch`` devices, and
for the cloud import ``cfg.plug_history.cloud_server`` / ``cloud_auth_key``.
Changes: ``samples`` rows of switch devices (``hourly_energy`` follows from
them); device names through the ``apply_names`` callback of the caller.
Tests: ``tests/test_plug_history.py``.

Row format: one ``samples`` row per interval, stamped at the interval end (live)
or at the bucket start (cloud). ``a_total_act_energy`` holds the interval energy
in Wh, so ``core.energy.calculate_energy`` takes its "interval" branch and the
Costs tab, the plots and the reports read the same kWh.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests

logger = logging.getLogger(__name__)

FLUSH_SECONDS = 60
# A gap longer than this (app was down) is spread evenly over hourly rows.
SEGMENT_SECONDS = 3600
# Shelly Cloud allows one request per second.
CLOUD_PAUSE_SECONDS = 1.1


# ── pure helpers ────────────────────────────────────────────────────────────

def counter_delta(prev_wh: Optional[float], cur_wh: float) -> float:
    """Energy (Wh) since the previous counter reading.

    A smaller reading means the counter was reset; then the new reading is the
    energy since the reset."""
    if prev_wh is None:
        return 0.0
    d = float(cur_wh) - float(prev_wh)
    return float(cur_wh) if d < 0 else d


def split_interval(t0: int, t1: int, wh: float, step: int = SEGMENT_SECONDS) -> List[Tuple[int, float]]:
    """Spread ``wh`` evenly over (t0, t1] as rows that end on ``step`` boundaries.

    Returns ``[(row_end_ts, wh)]``. An interval of ``step`` or less stays one row."""
    t0, t1 = int(t0), int(t1)
    if t1 - t0 <= step:
        return [(t1, float(wh))]
    out: List[Tuple[int, float]] = []
    cur, total = t0, t1 - t0
    while cur < t1:
        nxt = min(t1, (cur // step + 1) * step)
        out.append((nxt, float(wh) * (nxt - cur) / total))
        cur = nxt
    return out


def sum_tariff_rows(history: List[Dict[str, Any]]) -> List[Tuple[str, float]]:
    """Shelly Cloud returns one row per tariff for each bucket; add them up.

    Returns ``[(datetime_text, wh)]`` sorted by time."""
    sums: Dict[str, float] = {}
    for r in history or []:
        if not isinstance(r, dict) or r.get("missing"):
            continue
        dt = str(r.get("datetime") or "")
        if not dt:
            continue
        try:
            wh = float(r.get("consumption") or 0.0)
        except (TypeError, ValueError):
            continue
        sums[dt] = sums.get(dt, 0.0) + wh
    return sorted(sums.items())


def normalize_server(text: str) -> str:
    """``https://<host>`` from a host, a URL, or a device's cloud config value
    such as ``shelly-73-eu.shelly.cloud:6022/jrpc``."""
    t = str(text or "").strip()
    t = re.sub(r"^[a-z]+://", "", t, flags=re.I)
    host = t.split("/", 1)[0].split(":", 1)[0].strip()
    return f"https://{host}" if host else ""


def cloud_id_for_device(device: Any) -> Optional[str]:
    """The Shelly Cloud id (MAC, lowercase) of a configured device, from its key
    (``shellyplugus-c049ef8c3400``), or None."""
    m = re.search(r"([0-9a-f]{12})$", str(getattr(device, "key", "") or "").lower())
    return m.group(1) if m else None


def match_cloud_devices(cloud_devices: List[Dict[str, Any]], app_devices: List[Any]) -> Dict[str, Dict[str, Any]]:
    """Map app device key -> cloud device entry, by MAC in the key or by IP."""
    by_id: Dict[str, Dict[str, Any]] = {}
    by_ip: Dict[str, Dict[str, Any]] = {}
    for c in cloud_devices or []:
        cid = str(c.get("id") or "").lower()
        ch = int(c.get("channel") or 0)
        base = cid.split("_", 1)[0]
        by_id[f"{base}:{ch}"] = c
        if c.get("ip"):
            by_ip[f"{str(c['ip']).strip()}:{ch}"] = c
    out: Dict[str, Dict[str, Any]] = {}
    for d in app_devices or []:
        ch = int(getattr(d, "em_id", 0) or 0)
        mac = cloud_id_for_device(d)
        hit = by_id.get(f"{mac}:{ch}") if mac else None
        if hit is None:
            hit = by_ip.get(f"{str(getattr(d, 'host', '') or '').strip()}:{ch}")
        if hit is not None:
            out[d.key] = hit
    return out


def rows_frame(rows: List[Tuple[int, float, float, Optional[float], Optional[float]]]) -> pd.DataFrame:
    """``samples`` frame from ``(ts, wh, avg_w, voltage, current)`` tuples."""
    return pd.DataFrame({
        "timestamp": [int(r[0]) for r in rows],
        "a_total_act_energy": [float(r[1]) for r in rows],
        "b_total_act_energy": [0.0] * len(rows),
        "c_total_act_energy": [0.0] * len(rows),
        "energy_kwh": [float(r[1]) / 1000.0 for r in rows],
        "total_power": [float(r[2]) for r in rows],
        "a_act_power": [float(r[2]) for r in rows],
        "a_voltage": [r[3] for r in rows],
        "a_current": [r[4] for r in rows],
    })


# ── live recorder ───────────────────────────────────────────────────────────

class PlugEnergyRecorder:
    """Writes one ``samples`` row per plug about every minute from the live counter.

    The last counter reading is kept in ``state_path`` so energy used while the
    app was down is counted on the next start (spread over hourly rows)."""

    def __init__(self, db: Any, state_path: Path, flush_seconds: int = FLUSH_SECONDS) -> None:
        self.db = db
        self.state_path = Path(state_path)
        self.flush_seconds = int(flush_seconds)
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, Any]] = self._load()
        self._acc: Dict[str, Dict[str, Any]] = {}

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}
        except Exception:
            return {}

    def _save_locked(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state), encoding="utf-8")
            tmp.replace(self.state_path)
        except Exception:
            logger.debug("plug energy state not saved", exc_info=True)

    def since(self, device_key: str) -> Optional[int]:
        """Time from which this recorder counts the device's energy, or None."""
        with self._lock:
            st = self._state.get(device_key)
            return int(st["since"]) if st and st.get("since") else None

    def observe(self, device_key: str, ts: int, raw: Dict[str, Any]) -> int:
        """Feed one ``Switch.GetStatus`` result; returns the number of rows written."""
        en = raw.get("aenergy") if isinstance(raw, dict) else None
        if not isinstance(en, dict) or en.get("total") is None:
            return 0
        try:
            total = float(en["total"])
            power = float(raw.get("apower") or 0.0)
        except (TypeError, ValueError):
            return 0
        ts = int(ts)
        with self._lock:
            acc = self._acc.setdefault(device_key, {"p": 0.0, "n": 0, "v": None, "i": None})
            acc["p"] += power
            acc["n"] += 1
            acc["v"] = raw.get("voltage", acc["v"])
            acc["i"] = raw.get("current", acc["i"])
            st = self._state.get(device_key)
            if st is None:
                self._state[device_key] = {"wh": total, "ts": ts, "since": ts}
                self._save_locked()
                return 0
            if ts - int(st["ts"]) < self.flush_seconds:
                return 0
            wh = counter_delta(st.get("wh"), total)
            avg_w = acc["p"] / max(1, acc["n"])
            segs = split_interval(int(st["ts"]), ts, wh)
            rows = []
            prev = int(st["ts"])
            for end, seg_wh in segs:
                dur = max(1, end - prev)
                w = avg_w if len(segs) == 1 else seg_wh * 3600.0 / dur
                rows.append((end, seg_wh, w, acc["v"], acc["i"]))
                prev = end
            st["wh"], st["ts"] = total, ts
            self._acc[device_key] = {"p": 0.0, "n": 0, "v": None, "i": None}
            self._save_locked()
        try:
            return int(self.db.insert_dataframe(device_key, rows_frame(rows)))
        except Exception:
            logger.warning("plug energy rows for %s not written", device_key, exc_info=True)
            return 0


# ── Shelly Cloud ────────────────────────────────────────────────────────────

class CloudError(RuntimeError):
    pass


def _cloud_request(method: str, url: str, key: str, params: Optional[Dict[str, Any]] = None,
                   timeout: float = 60.0) -> Dict[str, Any]:
    params = dict(params or {}, auth_key=key)
    try:
        if method == "POST":
            resp = requests.post(url, data=params, timeout=timeout)
        else:
            resp = requests.get(url, params=params, timeout=timeout)
    except requests.RequestException as e:
        # The exception text contains the request, and with it the key.
        raise CloudError(f"Shelly Cloud request failed ({type(e).__name__})") from None
    try:
        data = resp.json()
    except ValueError:
        raise CloudError(f"Shelly Cloud: HTTP {resp.status_code}") from None
    if resp.status_code != 200:
        raise CloudError(f"Shelly Cloud: HTTP {resp.status_code} {str(data)[:200]}")
    if isinstance(data, dict) and data.get("isok") is False:
        raise CloudError(f"Shelly Cloud: {str(data.get('errors') or data)[:200]}")
    return data if isinstance(data, dict) else {"data": data}


def server_from_device(host: str, timeout: float = 5.0) -> str:
    """The account's cloud server, read from a device (``Cloud.GetConfig``)."""
    try:
        r = requests.get(f"http://{host}/rpc/Cloud.GetConfig", timeout=timeout)
        return normalize_server((r.json() or {}).get("server") or "")
    except Exception:
        return ""


def cloud_lists(server: str, key: str) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """(devices, {room_id: room_name}) from the Shelly web app's list endpoint.

    This endpoint is not part of the documented API; a changed shape is a CloudError."""
    data = _cloud_request("POST", f"{server}/interface/device/get_all_lists", key)
    body = data.get("data") if isinstance(data.get("data"), dict) else {}
    devs = body.get("devices")
    rooms = body.get("rooms")
    if not isinstance(devs, dict):
        raise CloudError("Shelly Cloud: unexpected device list")
    rmap = {str(r.get("id")): str(r.get("name") or "") for r in (rooms or {}).values() if isinstance(r, dict)}
    return [d for d in devs.values() if isinstance(d, dict)], rmap


def cloud_consumption(server: str, key: str, cloud_id: str, channel: int,
                      date_from: datetime, date_to: datetime) -> Tuple[str, str, List[Tuple[str, float]]]:
    """(timezone, interval, [(local datetime text, wh)]) for one device and range.

    One day gives hourly buckets, up to about 90 days daily buckets."""
    data = _cloud_request("GET", f"{server}/v2/statistics/power-consumption", key, {
        "id": cloud_id, "channel": int(channel), "date_range": "custom",
        "date_from": date_from.strftime("%Y-%m-%d %H:%M:%S"),
        "date_to": date_to.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return (str(data.get("timezone") or ""), str(data.get("interval") or ""),
            sum_tariff_rows(data.get("history") or []))


def _bucket_rows(buckets: List[Tuple[str, float]], interval: str, tz: ZoneInfo,
                 cutoff_ts: int, existing: List[int]) -> List[Tuple[int, float, float, Optional[float], Optional[float]]]:
    """Rows for complete buckets that end before ``cutoff_ts`` and hold no sample yet."""
    rows = []
    for text, wh in buckets:
        if wh <= 0:
            continue
        try:
            start_local = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        except ValueError:
            continue
        end_local = start_local + (timedelta(hours=1) if interval == "hour" else timedelta(days=1))
        b0, b1 = int(start_local.timestamp()), int(end_local.timestamp())
        if b1 > cutoff_ts:
            continue
        if any(b0 <= t < b1 for t in existing):
            continue
        rows.append((b0, wh, wh * 3600.0 / max(1, b1 - b0), None, None))
    return rows


def backfill_device(db: Any, server: str, key: str, cloud_id: str, channel: int, device_key: str, *,
                    start: datetime, hourly_days: int, cutoff_ts: int, tz_name: str,
                    progress: Optional[Callable[[str], None]] = None,
                    stop: Optional[threading.Event] = None,
                    pause: float = CLOUD_PAUSE_SECONDS) -> int:
    """Import past consumption of one device; returns the number of rows written.

    Daily rows from ``start`` to ``hourly_days`` before today, hourly rows after
    that. Buckets that end after ``cutoff_ts`` (local recording started) or that
    already hold samples are skipped, so a re-run never counts energy twice."""
    tz = ZoneInfo(tz_name)
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    hourly_start = today - timedelta(days=max(1, int(hourly_days)) - 1)
    ranges: List[Tuple[datetime, datetime]] = []
    cur = start.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    while cur < hourly_start:
        nxt_month = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        end = min(nxt_month, hourly_start)
        ranges.append((cur, end - timedelta(seconds=1)))
        cur = end
    day = max(hourly_start, cur)
    while day <= today:
        ranges.append((day, day + timedelta(days=1) - timedelta(seconds=1)))
        day += timedelta(days=1)

    written = 0
    for i, (a, b) in enumerate(ranges):
        if stop is not None and stop.is_set():
            break
        if progress:
            progress(f"{device_key}: {a:%Y-%m-%d} ({i + 1}/{len(ranges)})")
        tz_resp, interval, buckets = cloud_consumption(server, key, cloud_id, channel, a, b)
        btz = ZoneInfo(tz_resp) if tz_resp else tz
        a_ts = int(a.replace(tzinfo=btz).timestamp())
        b_ts = int((b + timedelta(seconds=1)).replace(tzinfo=btz).timestamp())
        existing = db.sample_timestamps(device_key, a_ts, b_ts)
        rows = _bucket_rows(buckets, interval, btz, cutoff_ts, existing)
        if rows:
            written += int(db.insert_dataframe(device_key, rows_frame(rows)))
        time.sleep(pause)
    return written


# ── import job ──────────────────────────────────────────────────────────────

class CloudImportJob:
    """One background import at a time; ``status()`` is polled by the Settings page."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._status: Dict[str, Any] = {"running": False, "message": "", "error": "", "rows": 0, "names": 0}

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self._status.update(kw)

    def start(self, state: Any, apply_names: Callable[[Dict[str, str]], None], history: bool = True) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._status = {"running": True, "message": "Starting…", "error": "", "rows": 0, "names": 0}
            self._thread = threading.Thread(target=self._run, args=(state, apply_names, history),
                                             name="PlugCloudImport", daemon=True)
            self._thread.start()
            return True

    def _run(self, state: Any, apply_names: Callable[[Dict[str, str]], None], history: bool) -> None:
        try:
            self._run_inner(state, apply_names, history)
        except Exception as e:
            logger.warning("Shelly Cloud import failed: %s", e)
            self._set(error=str(e))
        finally:
            self._set(running=False)

    def _run_inner(self, state: Any, apply_names: Callable[[Dict[str, str]], None], history: bool) -> None:
        from shelly_analyzer.io.config import app_timezone
        cfg = state.cfg
        ph = cfg.plug_history
        key = str(ph.cloud_auth_key or "").strip()
        if not key:
            raise CloudError("No Shelly Cloud key (Settings -> Plug history)")
        devices = [d for d in cfg.devices if str(getattr(d, "kind", "")) == "switch"]
        server = normalize_server(ph.cloud_server)
        if not server:
            for d in devices:
                server = server_from_device(d.host)
                if server:
                    break
        if not server:
            raise CloudError("No Shelly Cloud server (Settings -> Plug history)")

        self._set(message="Reading the device list…")
        cloud_devs, rooms = cloud_lists(server, key)
        matched = match_cloud_devices(cloud_devs, cfg.devices)
        names = {k: str(c.get("name") or "").strip() for k, c in matched.items() if str(c.get("name") or "").strip()}
        changed = {k: n for k, n in names.items() if any(d.key == k and d.name != n for d in cfg.devices)}
        if changed:
            apply_names(changed)
        self._set(names=len(changed), message=f"Names: {len(changed)} updated")
        if not history:
            self._set(message=f"Done: {len(changed)} names updated")
            return

        cutoff_year = datetime.now().year - 1  # same window as the retention policy
        start = datetime(cutoff_year, 1, 1)
        bg = getattr(state, "_bg", None)
        recorder = getattr(bg, "plug_recorder", None) if bg is not None else None
        db = state.storage.db
        total = 0
        tz_name = app_timezone()
        for d in devices:
            c = matched.get(d.key)
            if c is None:
                continue
            since = recorder.since(d.key) if recorder is not None else None
            cutoff = int(since) if since else int(time.time())
            total += backfill_device(
                db, server, key, str(c.get("id")).split("_", 1)[0], int(c.get("channel") or 0), d.key,
                start=start, hourly_days=int(ph.cloud_hourly_days or 31), cutoff_ts=cutoff, tz_name=tz_name,
                progress=lambda m: self._set(message=m), stop=self._stop)
            self._set(rows=total)
        self._set(rows=total, message=f"Done: {len(changed)} names updated, {total} history rows imported")


JOB = CloudImportJob()
