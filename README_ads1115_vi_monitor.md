# ADS1115 Voltage + Current Sensor Monitor — Installation and Use

**Script:** `ads1115_vi_monitor.py`
**Author:** M C Michael · **Professor:** Dr. Bing Ouyang
**Harbor Branch Oceanographic Institute, Florida Atlantic University, Fort Pierce, FL**

Reads a current sensor on **A0** and a voltage sense line on **A1** through an
ADS1115 16-bit ADC on a Raspberry Pi. Three modes: a pass/fail health check, a
zero-point calibration routine, and a live monitor with optional CSV logging.

---

## 1. Parts

| Item | Notes |
|---|---|
| Raspberry Pi (any 40-pin model) | Pi 3/4/5 or Zero 2 W all work |
| ADS1115 breakout | 4-channel, 16-bit, I²C. ADS1015 also works but is 12-bit |
| Current sensor | ACS712 / ACS758 hall module, **or** a shunt + INA181/INA210 amplifier |
| 2 resistors for the voltage divider | 1 % metal film; see §3.3 for values |
| 0.1 µF ceramic capacitor | A0 to GND, right at the ADS1115 — optional but kills hall-sensor jitter |
| Jumper wires | Keep the analog leads short |

---

## 2. Before you wire anything

Two limits decide whether this works or releases smoke:

1. **Absolute maximum input.** Any ADS1115 input pin must stay between
   `GND − 0.3 V` and `VDD + 0.3 V`. If the ADS1115 runs on 3.3 V, nothing above
   ~3.6 V may touch A0 or A1 — ever, including transients.
2. **Measurement range.** Separate from the above. The programmable gain sets
   the span the ADC can *resolve*; signals beyond it read as a railed value.

| `GAIN` | Range | Resolution (LSB) |
|---|---|---|
| `2/3` | ±6.144 V | 187.5 µV |
| `1` *(script default)* | ±4.096 V | 125 µV |
| `2` | ±2.048 V | 62.5 µV |
| `4` | ±1.024 V | 31.25 µV |
| `8` | ±0.512 V | 15.6 µV |
| `16` | ±0.256 V | 7.8 µV |

The measurement range can never usefully exceed VDD, whatever the gain says.

**The ACS712 trap.** A 5 V-powered ACS712 idles at ~2.5 V and swings toward 0 V
and 5 V. That exceeds both the ±4.096 V span at `GAIN = 1` *and* the absolute
input limit of a 3.3 V-powered ADS1115. Pick one:

- Power the ADS1115 from **5 V** and set `GAIN = 2/3` (±6.144 V). Simplest.
  The Pi's I²C lines tolerate the 5 V pull-ups on most ADS1115 breakouts, but
  check your board.
- Keep the ADS1115 on 3.3 V and put a **2:1 divider** on the ACS712 output,
  then halve `CURRENT_SENSITIVITY_V_PER_A` and `CURRENT_ZERO_VOLTS` to match.

---

## 3. Wiring

### 3.1 ADS1115 to the Pi

| ADS1115 | Pi 40-pin header |
|---|---|
| VDD | 3V3 (pin 1) or 5V (pin 2) — see §2 |
| GND | GND (pin 6) |
| SCL | GPIO3 / SCL (pin 5) |
| SDA | GPIO2 / SDA (pin 3) |
| ADDR | GND → `0x48` · VDD → `0x49` · SDA → `0x4A` · SCL → `0x4B` |

Most breakouts have on-board 10 kΩ I²C pull-ups, so no extras are needed.

### 3.2 Current sensor → A0

The sensor goes **in series** with the load, in the current path. Its signal
output goes to A0.

```
   supply + ---[ current sensor ]--- load + ---...--- load - --- supply -
                      |
                   OUT pin ----------> ADS1115 A0
                      |
                   (0.1 uF to GND)
```

All grounds must be common: Pi GND, ADS1115 GND, sensor GND, and the circuit
being measured. Without a shared reference the readings are meaningless.

### 3.3 Voltage sense → A1

Never connect the bus directly. Divide it down:

```
    V_bus ---[ R_TOP ]---+--- A1
                         |
                    [ R_BOTTOM ]
                         |
                        GND
```

`V_A1 = V_bus × R_BOTTOM / (R_TOP + R_BOTTOM)`

| Bus | R_TOP | R_BOTTOM | Ratio | A1 at full bus |
|---|---|---|---|---|
| 0–15 V | 100 kΩ | 10 kΩ | 11.0 | 1.36 V |
| 0–30 V | 100 kΩ | 10 kΩ | 11.0 | 2.73 V |
| 0–60 V | 220 kΩ | 10 kΩ | 23.0 | 2.61 V |

Aim for 50–80 % of the gain range at your maximum expected voltage — that
leaves headroom for surges without wasting resolution. **Measure the divider
output with a meter at full bus voltage before connecting A1.**

---

## 4. Raspberry Pi setup

### 4.1 Enable I²C

```bash
sudo raspi-config nonint do_i2c 0
sudo apt update
sudo apt install -y python3-pip python3-venv i2c-tools
sudo reboot
```

### 4.2 Confirm the ADC is on the bus

```bash
i2cdetect -y 1
```

Expect `48` in the grid (or `49`/`4a`/`4b` per the ADDR pin). If the grid is
empty, stop here and fix the wiring — nothing downstream will work.

### 4.3 Install the library

Raspberry Pi OS Bookworm and newer block system-wide `pip`, so use a virtual
environment:

```bash
python3 -m venv --system-site-packages ~/adcenv
source ~/adcenv/bin/activate
pip install adafruit-circuitpython-ads1x15
```

On older releases (Bullseye and earlier) a plain `pip3 install
adafruit-circuitpython-ads1x15` is fine.

Remember to `source ~/adcenv/bin/activate` in each new shell, or call the
interpreter by full path: `~/adcenv/bin/python3 ads1115_vi_monitor.py`.

### 4.4 Copy the script across

```bash
scp ads1115_vi_monitor.py pi@raspberrypi.local:~/
chmod +x ~/ads1115_vi_monitor.py
```

---

## 5. Calibration

Do this once per hardware build. The script is only as accurate as these five
numbers.

### Step 1 — Open the calibration block

Everything editable sits between the `CALIBRATION` banner and `--- Acquisition ---`
near the top of the file.

### Step 2 — Voltage channel

Measure the two divider resistors with a meter and enter the **actual** values:

```python
R_TOP = 99_400.0        # not 100k — what the meter says
R_BOTTOM = 9_980.0
```

Then apply a known voltage, compare the script's reading to a meter, and put
any residual difference in `VOLTAGE_OFFSET` (volts, subtracted after scaling).

### Step 3 — Current sensor type

```python
CURRENT_SENSOR = "acs712"   # or "shunt"
```

For a hall module, set the sensitivity for your variant:

| Part | Range | `CURRENT_SENSITIVITY_V_PER_A` |
|---|---|---|
| ACS712-05B | ±5 A | `0.185` |
| ACS712-20A | ±20 A | `0.100` |
| ACS712-30A | ±30 A | `0.066` |

For a shunt + amplifier, set `SHUNT_RESISTANCE_OHMS` and `SHUNT_AMP_GAIN`
instead; the script computes `I = V_out / (R_shunt × gain)`.

### Step 4 — Zero the current sensor

Disconnect the load so no current flows, then:

```bash
python3 ads1115_vi_monitor.py --zero
```

It averages A0 for five seconds and prints a line to paste back:

```
  mean         = 2.49312 V
  stdev        = 1.840 mV

Set in the calibration block:
  CURRENT_ZERO_VOLTS = 2.49312
```

Every hall sensor sits tens of millivolts off its nominal VCC/2. Skipping this
step is the usual cause of a constant phantom current. If the stdev exceeds
10 mV, add the 0.1 µF cap and shorten the leads before continuing.

### Step 5 — Set the plausibility limits

```python
EXPECTED_VOLTAGE_RANGE = (0.0, 30.0)     # what your bus should ever show
EXPECTED_CURRENT_RANGE = (-20.0, 20.0)
```

These drive the self-test. Tighten them to your actual system — generic limits
produce a generic (and useless) pass.

---

## 6. Running it

### Health check

```bash
python3 ads1115_vi_monitor.py --selftest
python3 ads1115_vi_monitor.py --selftest --quiescent   # system idle: also checks the zero point
```

```
ADS1115 sensor self-test  v1.0
  M C Michael -- Harbor Branch Oceanographic Institute, Florida Atlantic University
  address 0x48 | gain 1 (+/-4.096 V) | 128 SPS | 16 samples averaged

  [ OK ] A0 current: +1.482 A (ADC +2.6413 V, sigma=0.91 mV)
  [ OK ] A1 voltage: +12.104 V (ADC +1.1004 V, sigma=0.24 mV)

  raw counts: A0=21184  A1=8824  (+/-32767 full scale)
  derived:    12.104 V  +1.482 A  +17.94 W

Result: PASS -- both channels respond and read in range.
```

Exit codes: **0** pass · **1** pass with warnings · **2** fail. That makes it
usable in a test fixture or a cron check:

```bash
python3 ads1115_vi_monitor.py --selftest || echo "sensor fault" | mail -s alert you@fau.edu
```

### Live monitor

```bash
python3 ads1115_vi_monitor.py                      # 2 readings/s to the terminal
python3 ads1115_vi_monitor.py --hz 5               # faster
python3 ads1115_vi_monitor.py --log run1.csv       # also write CSV
python3 ads1115_vi_monitor.py --log run1.csv --hz 10 --duration 600   # 10 min capture
```

```
    time      volts       amps      watts      A0 (V)     A1 (V)
------------------------------------------------------------------
14:22:07     12.104     +1.482     +17.94      2.6413     1.1004
14:22:07     12.101     +1.479     +17.89      2.6410     1.1001
```

`Ctrl-C` stops cleanly and closes the log file.

### All options

| Flag | Effect |
|---|---|
| `--selftest` | one health check, then exit with 0/1/2 |
| `--quiescent` | with `--selftest`: assert the system is idle so the zero point is checked too |
| `--zero` | measure the no-load current offset |
| `--zero-seconds N` | averaging window for `--zero` (default 5) |
| `--hz N` | monitor update rate (default 2) |
| `--duration N` | stop the monitor after N seconds |
| `--log FILE` | write readings to CSV |
| `--address 0x49` | non-default I²C address |

### CSV format

```
timestamp_iso,epoch_s,volts,amps,watts,a0_adc_v,a1_adc_v,a0_raw,a1_raw
2026-10-01T14:22:07,1759328527.412,12.1041,1.4823,17.9429,2.641300,1.100400,21184,8824
```

Raw ADC volts and counts are logged alongside the scaled values, so if the
calibration turns out wrong you can re-derive engineering units from an
existing dataset instead of repeating the run.

---

## 7. What the self-test is checking

| Condition | Verdict | Likely cause |
|---|---|---|
| Reading ≥ 98 % of full scale | **FAIL** | input over-range, or `GAIN` set too high |
| 0 V with σ < 2 mV | **FAIL** | pin shorted to GND, or sensor unpowered |
| σ > 50 mV | **WARN** | floating input, no shared ground, long unshielded leads |
| Scaled value outside expected range | **FAIL** | wrong calibration constants, or miswired |
| Idle A0 far from `CURRENT_ZERO_VOLTS` | **WARN** | re-run `--zero` |
| Current flowing while bus reads ~0 V | **WARN** | the two channels disagree — one is lying |

The σ (standard deviation across the 16 averaged samples) is the useful
diagnostic here. A disconnected ADC input *floats* and picks up mains hum, so
it reads noisy; a shorted one reads dead quiet. Those two failures look
identical if you only print the mean.

---

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| `Could not open the ADS1115 at 0x48` | `i2cdetect -y 1` shows nothing → wiring or I²C not enabled (`sudo raspi-config nonint do_i2c 0`, then reboot) |
| `Missing library: board` | virtual environment not activated, or `adafruit-circuitpython-ads1x15` not installed |
| Device appears at `0x49`–`0x4B` | ADDR pin is not tied to GND; pass `--address 0x49` or re-tie it |
| Voltage reads ~11× too low or too high | `USE_DIVIDER` wrong, or `R_TOP`/`R_BOTTOM` swapped |
| Constant non-zero current at idle | `CURRENT_ZERO_VOLTS` not calibrated — run `--zero` |
| Current sign inverted | sensor installed backwards; swap its IP+/IP− terminals, or negate `CURRENT_SENSITIVITY_V_PER_A` |
| Readings jump around by amps | no common ground, or no 0.1 µF cap on A0; also try raising `SAMPLES_PER_READING` |
| Both channels railed at full scale | input exceeds VDD — disconnect immediately and re-check the divider |
| Values drift as the load heats | hall sensors drift with temperature; re-zero at operating temperature |

For a rate sanity check: with the defaults (`DATA_RATE = 128` SPS,
`SAMPLES_PER_READING = 16`) each reported reading needs 32 conversions across
the two channels — about 0.25 s, so `--hz` above ~4 is silently limited by the
ADC. Raise `DATA_RATE` (860 SPS is the ADS1115 maximum, shared across
channels) or lower `SAMPLES_PER_READING` if you need faster logging; both
trade noise rejection for speed.

---

## 9. Running at boot (optional)

```bash
sudo tee /etc/systemd/system/vimonitor.service >/dev/null <<'EOF'
[Unit]
Description=ADS1115 voltage/current monitor
After=multi-user.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi
ExecStart=/home/pi/adcenv/bin/python3 /home/pi/ads1115_vi_monitor.py --hz 1 --log /home/pi/vi_log.csv
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now vimonitor.service
systemctl status vimonitor.service
journalctl -u vimonitor.service -f
```

For long deployments, rotate the CSV (a 1 Hz log is roughly 8 MB/day) or have
the service write to a date-stamped filename.

---

## 10. Safety

- Verify every divider output with a meter **before** it touches the ADS1115.
- Never exceed `VDD + 0.3 V` on any analog input, including transients.
- Fuse the circuit under test. A hall sensor carries full load current through
  its conductor path, and a shunt dissipates `I²R`.
- Treat this as instrumentation, not protection. It reports what it measures;
  it does not disconnect anything.
- Mains voltage is out of scope for this setup. Measuring it needs isolation
  hardware, not a resistor divider.
