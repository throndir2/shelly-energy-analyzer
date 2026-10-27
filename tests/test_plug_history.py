"""Plug energy history (services/plug_history.py).

Plugs have no EMData log, so the app turns the live energy counter into
samples rows, and can import past consumption from Shelly Cloud.
"""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shelly_analyzer.core.energy import calculate_energy
from shelly_analyzer.io.config import DeviceConfig, load_config, save_config
from shelly_analyzer.io.database import EnergyDB
from shelly_analyzer.services import plug_history as ph


def test_counter_delta_handles_reset():
    assert ph.counter_delta(None, 500.0) == 0.0
    assert ph.counter_delta(1000.0, 1012.5) == 12.5
    # Counter reset to 0 and counted 7 Wh since then.
    assert ph.counter_delta(1000.0, 7.0) == 7.0


def test_split_interval_spreads_long_gaps_over_hours():
    assert ph.split_interval(100, 160, 5.0) == [(160, 5.0)]
    rows = ph.split_interval(3000, 3000 + 7800, 130.0)
    assert [r[0] for r in rows] == [3600, 7200, 10800]
    assert abs(sum(r[1] for r in rows) - 130.0) < 1e-9
    assert abs(rows[0][1] - 130.0 * 600 / 7800) < 1e-9


def test_sum_tariff_rows_adds_rows_of_the_same_bucket():
    hist = [
        {"datetime": "2026-10-25 00:00:00", "consumption": 3.95, "tariff_id": "0"},
        {"datetime": "2026-10-25 00:00:00", "consumption": 5068.88, "tariff_id": "1"},
        {"datetime": "2026-10-24 00:00:00", "consumption": 5.13, "tariff_id": "0"},
        {"datetime": "2026-10-24 00:00:00", "consumption": 3877.45, "tariff_id": "1"},
        {"datetime": "2026-10-23 00:00:00", "consumption": 99.0, "missing": True},
    ]
    assert ph.sum_tariff_rows(hist) == [("2026-10-24 00:00:00", 3882.58), ("2026-10-25 00:00:00", 5072.83)]


def test_normalize_server_and_device_matching():
    assert ph.normalize_server("shelly-73-eu.shelly.cloud:6022/jrpc") == "https://shelly-73-eu.shelly.cloud"
    assert ph.normalize_server("https://shelly-73-eu.shelly.cloud/") == "https://shelly-73-eu.shelly.cloud"
    devs = [DeviceConfig(key="shellyplugus-c049ef8c3400", name="x", host="192.168.50.10", kind="switch"),
            DeviceConfig(key="192_168_50_95", name="y", host="192.168.50.95", kind="switch")]
    cloud = [{"id": "c049ef8c3400", "channel": 0, "name": "Media Server Plug", "ip": "192.168.50.10"},
             {"id": "c049ef8c0724", "channel": 0, "name": "Bedroom Air Conditioner", "ip": "192.168.50.95"}]
    m = ph.match_cloud_devices(cloud, devs)
    assert m["shellyplugus-c049ef8c3400"]["name"] == "Media Server Plug"
    assert m["192_168_50_95"]["name"] == "Bedroom Air Conditioner"


def _db():
    d = tempfile.mkdtemp()
    return EnergyDB(Path(d) / "energy.db"), Path(d)


def test_recorder_writes_interval_energy_that_calculate_energy_reads():
    db, d = _db()
    rec = ph.PlugEnergyRecorder(db, d / "state.json", flush_seconds=60)
    key = "shellyplugus-c049ef8c3400"
    assert rec.observe(key, 1_000_000, {"apower": 250.0, "aenergy": {"total": 1000.0}}) == 0
    assert rec.observe(key, 1_000_030, {"apower": 250.0, "aenergy": {"total": 1002.0}}) == 0
    assert rec.observe(key, 1_000_060, {"apower": 250.0, "aenergy": {"total": 1004.2}}) == 1
    # A restart after 2 h: the state file keeps the baseline, energy is spread per hour.
    rec2 = ph.PlugEnergyRecorder(db, d / "state.json", flush_seconds=60)
    assert rec2.since(key) == 1_000_000
    n = rec2.observe(key, 1_000_060 + 7200, {"apower": 100.0, "aenergy": {"total": 1204.2}})
    assert n == 3
    df = calculate_energy(db.query_samples(key))
    assert abs(float(df["energy_kwh"].sum()) - 0.2042) < 1e-9
    # A counter reset never gives negative energy.
    rec2.observe(key, 1_000_060 + 7300, {"apower": 100.0, "aenergy": {"total": 3.0}})
    df = calculate_energy(db.query_samples(key))
    assert abs(float(df["energy_kwh"].sum()) - 0.2072) < 1e-9


def test_backfill_skips_filled_and_recent_buckets_and_is_idempotent():
    db, _ = _db()
    key = "shellyplugus-c049ef8b65a0"
    tz = ZoneInfo("America/Los_Angeles")
    day = datetime(2026, 10, 20)

    def fake(server, k, cid, ch, a, b):
        if (b - a).days >= 1:  # month chunk -> daily buckets
            return ("America/Los_Angeles", "day", [(f"{a:%Y-%m}-{n:02d} 00:00:00", 1000.0) for n in range(a.day, b.day + 1)])
        return ("America/Los_Angeles", "hour", [(f"{a:%Y-%m-%d} {h:02d}:00:00", 100.0) for h in range(24)])

    # Local recording started at noon on 2026-10-21: later buckets come from the recorder.
    cutoff = int(datetime(2026, 10, 21, 12, tzinfo=tz).timestamp())
    with mock.patch.object(ph, "cloud_consumption", side_effect=fake), \
         mock.patch.object(ph, "datetime") as m_dt:
        m_dt.now.return_value = datetime(2026, 10, 21, 18, tzinfo=tz)
        m_dt.strptime = datetime.strptime
        kw = dict(start=datetime(2026, 10, 1), hourly_days=2, cutoff_ts=cutoff, tz_name="America/Los_Angeles", pause=0)
        n1 = ph.backfill_device(db, "https://x", "k", "c049ef8b65a0", 0, key, **kw)
        n2 = ph.backfill_device(db, "https://x", "k", "c049ef8b65a0", 0, key, **kw)
    # Oct 1-19 daily (19 rows), Oct 20 hourly (24), Oct 21 hours 0-11 (12).
    assert n1 == 19 + 24 + 12, n1
    assert n2 == 0
    df = calculate_energy(db.query_samples(key))
    assert abs(float(df["energy_kwh"].sum()) - (19 * 1.0 + 24 * 0.1 + 12 * 0.1)) < 1e-9
    del day


def test_config_round_trip_keeps_plug_history():
    f = tempfile.mktemp(suffix=".json")
    try:
        with open(f, "w") as fh:
            fh.write('{"version":"0","devices":[],"plug_history":{"enabled":true,"cloud_server":"shelly-73-eu.shelly.cloud","cloud_auth_key":"abc","cloud_hourly_days":10}}')
        cfg = load_config(f)
        assert cfg.plug_history.enabled and cfg.plug_history.cloud_auth_key == "abc"
        assert cfg.plug_history.cloud_hourly_days == 10
        save_config(cfg, f)
        again = load_config(f)
        assert again.plug_history == cfg.plug_history
        with open(f, "w") as fh:
            fh.write('{"version":"0","devices":[]}')
        assert load_config(f).plug_history.enabled is False
    finally:
        try:
            os.unlink(f)
        except OSError:
            pass


if __name__ == "__main__":
    import inspect as _inspect
    _mod = sys.modules[__name__]
    _fns = [(n, f) for n, f in vars(_mod).items() if n.startswith("test_") and callable(f)]
    _fns.sort(key=lambda kv: _inspect.getsourcelines(kv[1])[1])
    for _n, _f in _fns:
        _f()
        print("OK  %s" % _n)
    print("\n%d tests passed (%s)." % (len(_fns), os.path.basename(__file__)))
