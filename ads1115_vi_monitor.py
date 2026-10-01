#!/usr/bin/env python3
"""
ads1115_vi_monitor.py -- Read a voltage + current sensor pair through an
ADS1115 ADC on a Raspberry Pi, and report whether the sensors look healthy.

Author:      M C Michael
Professor:   Dr. Bing Ouyang
Affiliation: Harbor Branch Oceanographic Institute
             Florida Atlantic University (FAU)
             Fort Pierce, Florida, USA

Wiring
------
    ADS1115            Raspberry Pi (40-pin header)
    VDD      -------->  3V3   (pin 1)   [5V is OK too if your board is 5V-tolerant]
    GND      -------->  GND   (pin 6)
    SCL      -------->  GPIO3 / SCL (pin 5)
    SDA      -------->  GPIO2 / SDA (pin 3)
    ADDR     -------->  GND  = 0x48 (default) | VDD = 0x49 | SDA = 0x4A | SCL = 0x4B

    A0  <-- CURRENT sensor output  (e.g. ACS712 / ACS758 / shunt amplifier)
    A1  <-- VOLTAGE sense output   (resistor divider tap)

    *** The ADS1115 input pins must never exceed VDD + 0.3 V. Scale the bus
        voltage down with a divider and confirm the divider output with a
        meter before connecting A1. ***

Setup
-----
    sudo raspi-config nonint do_i2c 0          # enable the I2C bus
    sudo apt install -y python3-pip i2c-tools
    pip3 install adafruit-circuitpython-ads1x15
    i2cdetect -y 1                             # expect "48" in the grid

Usage
-----
    python3 ads1115_vi_monitor.py                       # live monitor
    python3 ads1115_vi_monitor.py --selftest            # one-shot health check
    python3 ads1115_vi_monitor.py --zero                # measure the current
                                                        #   sensor's no-load
                                                        #   offset, then exit
    python3 ads1115_vi_monitor.py --log run1.csv --hz 5
"""

from __future__ import annotations

import argparse
import csv
import signal
import statistics
import sys
import time
from dataclasses import dataclass, field

__author__ = "M C Michael"
__professor__ = "Dr. Bing Ouyang"
__affiliation__ = ("Harbor Branch Oceanographic Institute, "
                   "Florida Atlantic University")
__version__ = "1.0"

# ----------------------------------------------------------------------------
# CALIBRATION -- edit this block to match your hardware
# ----------------------------------------------------------------------------

I2C_ADDRESS = 0x48          # ADDR pin tied low

# --- Voltage channel (A1) ---------------------------------------------------
# A resistor divider scales the bus voltage into the ADC range:
#
#     V_bus ---[ R_top ]---+--- A1
#                          |
#                        [ R_bot ]
#                          |
#                         GND
#
# Measure the two resistors with a meter and put the real values here; 1 %
# resistors still drift, and the measured values are what set your accuracy.
R_TOP = 100_000.0           # ohms, bus side
R_BOTTOM = 10_000.0         # ohms, ground side
VOLTAGE_DIVIDER_RATIO = (R_TOP + R_BOTTOM) / R_BOTTOM   # 11.0 with the above
VOLTAGE_OFFSET = 0.0        # volts, subtracted after scaling (trim to a meter)

# Set to False if A1 is driven directly by a sensor with its own output scaling
# (then set VOLTAGE_DIVIDER_RATIO to that sensor's volts-out-per-volt-in).
USE_DIVIDER = True

# --- Current channel (A0) ---------------------------------------------------
# Hall-effect modules (ACS712 and friends) output VCC/2 at zero current and
# swing around it. Shunt amplifiers (INA181, INA210) sit at ~0 V at zero
# current and only swing one way.
CURRENT_SENSOR = "acs712"   # "acs712" | "shunt"

# ACS712 variants: 5 A -> 185 mV/A, 20 A -> 100 mV/A, 30 A -> 66 mV/A
CURRENT_SENSITIVITY_V_PER_A = 0.100

# Sensor output at zero current. For a 5 V ACS712 this is nominally 2.500 V,
# but every part is a few tens of mV off -- run --zero and paste the result.
CURRENT_ZERO_VOLTS = 2.500

# Shunt-amplifier path instead: I = V_out / (R_shunt * GAIN)
SHUNT_RESISTANCE_OHMS = 0.010
SHUNT_AMP_GAIN = 50.0

# Current smaller than this reads as exactly 0 A (kills hall-sensor jitter).
CURRENT_DEADBAND_A = 0.05

# --- Acquisition ------------------------------------------------------------
GAIN = 1                    # 2/3=+/-6.144V  1=+/-4.096V  2=+/-2.048V
                            # 4=+/-1.024V    8=+/-0.512V  16=+/-0.256V
DATA_RATE = 128             # samples/s: 8,16,32,64,128,250,475,860
SAMPLES_PER_READING = 16    # averaged per reported value
DEFAULT_HZ = 2.0            # reported readings per second

# --- Plausibility limits used by the health checks --------------------------
EXPECTED_VOLTAGE_RANGE = (0.0, 30.0)    # volts, what the bus should ever show
EXPECTED_CURRENT_RANGE = (-20.0, 20.0)  # amps
ADC_FULL_SCALE_FRACTION = 0.98          # above this = input is railed
FLOATING_INPUT_NOISE_V = 0.002          # stdev below this on a railed/odd pin
                                        #   suggests a stuck, not floating, pin

GAIN_FULL_SCALE_V = {
    2 / 3: 6.144, 1: 4.096, 2: 2.048, 4: 1.024, 8: 0.512, 16: 0.256,
}

CH_CURRENT = 0              # A0
CH_VOLTAGE = 1              # A1


# ----------------------------------------------------------------------------
# Hardware access
# ----------------------------------------------------------------------------

def open_adc(address: int = I2C_ADDRESS):
    """Return (ads, {channel_index: AnalogIn}). Exits with advice on failure."""
    try:
        import board
        import busio
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn
    except ImportError as exc:
        sys.exit(
            f"Missing library: {exc.name}\n"
            "  pip3 install adafruit-circuitpython-ads1x15\n"
            "(These modules only import on a Raspberry Pi with I2C enabled.)"
        )

    try:
        i2c = busio.I2C(board.SCL, board.SDA)
        ads = ADS.ADS1115(i2c, address=address)
    except Exception as exc:                      # noqa: BLE001 - report verbatim
        sys.exit(
            f"Could not open the ADS1115 at 0x{address:02X}: {exc}\n"
            "Check: I2C enabled (sudo raspi-config), wiring of SDA/SCL/VDD/GND,\n"
            "and that `i2cdetect -y 1` lists the address."
        )

    ads.gain = GAIN
    ads.data_rate = DATA_RATE
    channels = {
        CH_CURRENT: AnalogIn(ads, ADS.P0),
        CH_VOLTAGE: AnalogIn(ads, ADS.P1),
    }
    return ads, channels


def sample_channel(chan, n: int = SAMPLES_PER_READING):
    """Average n single-ended reads. Returns (mean_volts, stdev_volts, raw)."""
    volts, raws = [], []
    for _ in range(n):
        volts.append(chan.voltage)
        raws.append(chan.value)
    mean_v = statistics.fmean(volts)
    stdev_v = statistics.stdev(volts) if len(volts) > 1 else 0.0
    return mean_v, stdev_v, int(statistics.fmean(raws))


# ----------------------------------------------------------------------------
# Conversion
# ----------------------------------------------------------------------------

def adc_to_bus_voltage(v_adc: float) -> float:
    ratio = VOLTAGE_DIVIDER_RATIO if USE_DIVIDER else 1.0
    return v_adc * ratio - VOLTAGE_OFFSET


def adc_to_current(v_adc: float) -> float:
    if CURRENT_SENSOR == "acs712":
        amps = (v_adc - CURRENT_ZERO_VOLTS) / CURRENT_SENSITIVITY_V_PER_A
    elif CURRENT_SENSOR == "shunt":
        amps = v_adc / (SHUNT_RESISTANCE_OHMS * SHUNT_AMP_GAIN)
    else:
        raise ValueError(f"Unknown CURRENT_SENSOR {CURRENT_SENSOR!r}")
    return 0.0 if abs(amps) < CURRENT_DEADBAND_A else amps


@dataclass
class Reading:
    t: float
    v_adc_current: float
    v_adc_voltage: float
    noise_current: float
    noise_voltage: float
    raw_current: int
    raw_voltage: int
    amps: float = field(init=False)
    volts: float = field(init=False)

    def __post_init__(self) -> None:
        self.amps = adc_to_current(self.v_adc_current)
        self.volts = adc_to_bus_voltage(self.v_adc_voltage)

    @property
    def watts(self) -> float:
        return self.volts * self.amps


def read_once(channels) -> Reading:
    vc, nc, rc = sample_channel(channels[CH_CURRENT])
    vv, nv, rv = sample_channel(channels[CH_VOLTAGE])
    return Reading(time.time(), vc, vv, nc, nv, rc, rv)


# ----------------------------------------------------------------------------
# Health checks -- "is the sensor actually working?"
# ----------------------------------------------------------------------------

def check_channel(name: str, v_adc: float, noise: float, scaled: float,
                  unit: str, expected: tuple[float, float],
                  zero_ref: float | None) -> list[tuple[str, str]]:
    """Return a list of (level, message); level in {'ok','warn','fail'}."""
    out: list[tuple[str, str]] = []
    fs = GAIN_FULL_SCALE_V[GAIN]

    if abs(v_adc) >= fs * ADC_FULL_SCALE_FRACTION:
        out.append(("fail", f"{name}: ADC railed at {v_adc:+.4f} V "
                            f"(full scale {fs:.3f} V). Input over-range or "
                            f"GAIN set too high."))
    elif abs(v_adc) < 0.0005 and noise < FLOATING_INPUT_NOISE_V:
        out.append(("fail", f"{name}: reads 0 V and is dead quiet "
                            f"(sigma={noise*1000:.2f} mV). Pin looks shorted to "
                            f"GND or the sensor is unpowered."))
    elif noise > 0.050:
        out.append(("warn", f"{name}: noisy, sigma={noise*1000:.1f} mV. "
                            f"Floating input, missing ground reference, or "
                            f"long unshielded leads."))

    lo, hi = expected
    if not (lo <= scaled <= hi):
        out.append(("fail", f"{name}: {scaled:+.3f} {unit} is outside the "
                            f"expected {lo} to {hi} {unit}. Check the "
                            f"calibration constants and the wiring."))

    if zero_ref is not None and abs(v_adc - zero_ref) > 0.5:
        out.append(("warn", f"{name}: quiescent output {v_adc:.4f} V is far "
                            f"from the configured zero point {zero_ref:.4f} V. "
                            f"Run --zero with no load connected."))

    if not out:
        out.append(("ok", f"{name}: {scaled:+.3f} {unit} "
                          f"(ADC {v_adc:+.4f} V, sigma={noise*1000:.2f} mV)"))
    return out


def selftest(channels, quiescent: bool) -> int:
    """Print a health report. Returns a process exit code (0 = all OK)."""
    print(f"ADS1115 sensor self-test  v{__version__}")
    print(f"  {__author__} -- {__affiliation__}")
    print(f"  address 0x{I2C_ADDRESS:02X} | gain {GAIN} "
          f"(+/-{GAIN_FULL_SCALE_V[GAIN]:.3f} V) | {DATA_RATE} SPS | "
          f"{SAMPLES_PER_READING} samples averaged")
    print()

    r = read_once(channels)
    results: list[tuple[str, str]] = []
    results += check_channel(
        "A0 current", r.v_adc_current, r.noise_current, r.amps, "A",
        EXPECTED_CURRENT_RANGE,
        CURRENT_ZERO_VOLTS if (quiescent and CURRENT_SENSOR == "acs712") else None,
    )
    results += check_channel(
        "A1 voltage", r.v_adc_voltage, r.noise_voltage, r.volts, "V",
        EXPECTED_VOLTAGE_RANGE, None,
    )

    # Cross-check: current flowing with no bus voltage is not physical.
    if abs(r.amps) > 0.25 and r.volts < 0.5:
        results.append(("warn", "Cross-check: non-zero current with ~0 V on "
                                "the bus. One of the two channels is lying."))

    icon = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
    for level, msg in results:
        print(f"  {icon[level]} {msg}")

    print()
    print(f"  raw counts: A0={r.raw_current}  A1={r.raw_voltage}  "
          f"(+/-32767 full scale)")
    print(f"  derived:    {r.volts:.3f} V  {r.amps:+.3f} A  {r.watts:+.2f} W")

    if any(level == "fail" for level, _ in results):
        print("\nResult: FAIL -- at least one channel is not reading sensibly.")
        return 2
    if any(level == "warn" for level, _ in results):
        print("\nResult: PASS WITH WARNINGS -- readings are plausible but "
              "check the notes above.")
        return 1
    print("\nResult: PASS -- both channels respond and read in range.")
    return 0


def measure_zero(channels, seconds: float = 5.0) -> None:
    """Measure the current sensor's no-load output so it can be pasted back."""
    print(f"Measuring A0 zero point for {seconds:.0f} s.")
    print("Disconnect the load / make sure NO current is flowing.\n")
    samples: list[float] = []
    t_end = time.time() + seconds
    while time.time() < t_end:
        samples.append(channels[CH_CURRENT].voltage)
        time.sleep(0.01)
    mean_v = statistics.fmean(samples)
    sd = statistics.stdev(samples)
    print(f"  n            = {len(samples)}")
    print(f"  mean         = {mean_v:.5f} V")
    print(f"  stdev        = {sd*1000:.3f} mV")
    print(f"  peak-to-peak = {(max(samples)-min(samples))*1000:.3f} mV")
    print(f"\nSet in the calibration block:\n  CURRENT_ZERO_VOLTS = {mean_v:.5f}")
    if sd > 0.010:
        print("\nWarning: >10 mV of noise at idle. Add a 0.1 uF cap from A0 to "
              "GND and keep the sensor leads short.")


# ----------------------------------------------------------------------------
# Live monitor
# ----------------------------------------------------------------------------

_running = True


def _stop(signum, frame) -> None:        # noqa: ARG001 - signal handler
    global _running
    _running = False


def monitor(channels, hz: float, log_path: str | None,
            duration: float | None) -> None:
    period = 1.0 / hz
    writer = None
    fh = None
    if log_path:
        fh = open(log_path, "w", newline="")
        writer = csv.writer(fh)
        writer.writerow(["timestamp_iso", "epoch_s", "volts", "amps", "watts",
                         "a0_adc_v", "a1_adc_v", "a0_raw", "a1_raw"])
        print(f"Logging to {log_path}")

    print(f"{'time':>8}  {'volts':>9}  {'amps':>9}  {'watts':>9}   "
          f"{'A0 (V)':>9}  {'A1 (V)':>9}")
    print("-" * 66)

    t_start = time.time()
    try:
        while _running:
            loop_start = time.time()
            r = read_once(channels)
            stamp = time.strftime("%H:%M:%S", time.localtime(r.t))
            print(f"{stamp:>8}  {r.volts:9.3f}  {r.amps:+9.3f}  "
                  f"{r.watts:+9.2f}   {r.v_adc_current:9.4f}  "
                  f"{r.v_adc_voltage:9.4f}", flush=True)
            if writer:
                writer.writerow([
                    time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(r.t)),
                    f"{r.t:.3f}", f"{r.volts:.4f}", f"{r.amps:.4f}",
                    f"{r.watts:.4f}", f"{r.v_adc_current:.6f}",
                    f"{r.v_adc_voltage:.6f}", r.raw_current, r.raw_voltage,
                ])
                fh.flush()
            if duration and (time.time() - t_start) >= duration:
                break
            time.sleep(max(0.0, period - (time.time() - loop_start)))
    finally:
        if fh:
            fh.close()
            print(f"\nClosed {log_path}")


# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Read a voltage (A1) and current (A0) sensor via ADS1115.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--selftest", action="store_true",
                   help="run one health check and exit with 0/1/2")
    p.add_argument("--quiescent", action="store_true",
                   help="with --selftest: assert the system is idle, so the "
                        "current zero point is also checked")
    p.add_argument("--zero", action="store_true",
                   help="measure the no-load offset of the current sensor")
    p.add_argument("--zero-seconds", type=float, default=5.0)
    p.add_argument("--hz", type=float, default=DEFAULT_HZ,
                   help=f"monitor update rate (default {DEFAULT_HZ})")
    p.add_argument("--duration", type=float, default=None,
                   help="stop the monitor after this many seconds")
    p.add_argument("--log", metavar="CSV", default=None,
                   help="append readings to a CSV file")
    p.add_argument("--address", type=lambda s: int(s, 0), default=I2C_ADDRESS,
                   help="I2C address (default 0x48)")
    args = p.parse_args(argv)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    _, channels = open_adc(args.address)

    if args.zero:
        measure_zero(channels, args.zero_seconds)
        return 0
    if args.selftest:
        return selftest(channels, args.quiescent)
    monitor(channels, args.hz, args.log, args.duration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
