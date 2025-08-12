#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Small status dashboard for a Raspberry Pi router on a 128x64 SH1106 I2C OLED.

Pages:
  1) Hostname, time, uptime
  2) WAN IP, Internet reachability, Wi-Fi SSID, RSSI (dBm)
  3) LAN IP (eth0), DHCP lease count (dnsmasq)
  4) Tailscale IPv4 (if installed)
  5) Throughput (kbit/s) for eth0 and wlan0
  6) CPU temp, CPU usage %, load, memory used/total (MB)

Requirements:
  sudo apt install python3-pil python3-smbus i2c-tools
  (optional for button) sudo apt install python3-rpi.gpio
  pip3 install luma.oled

Environment:
  SCREEN_DEBUG=1          Enable debug logging
  SCREEN_LOG=/path/file   Append debug log to file
  SCREENSAVER_BITMAP=path Override screensaver bitmap (default: raspberry.bmp)
"""

import os
import time
import socket
import subprocess
from datetime import datetime, timedelta
import traceback

from PIL import Image, ImageDraw, ImageFont

from luma.core.interface.serial import i2c
from luma.oled.device import sh1106

# --- Debug / logging helpers --------------------------------------------------
DEBUG = os.environ.get("SCREEN_DEBUG", "0") not in ("0", "", "false", "False")
LOG_PATH = os.environ.get("SCREEN_LOG", "")  # optional file path

def debug(msg):
    if not DEBUG:
        return
    line = f"[SCREEN] {time.strftime('%H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

# --- Button toggle (GPIO) -----------------------------------------------------
try:
    import RPi.GPIO as GPIO
except Exception as e:
    GPIO = None
    debug(f"GPIO import failed: {e}")

SCREENSAVER_BUTTON_PIN = 17  # BCM pin number; wire to a momentary button to GND

# Display modes
MODE_PAGES = 0
MODE_SAVER = 1
MODE_OFF   = 2
display_mode = MODE_PAGES

# Button fallback / polling state
BUTTON_EVENT_OK = False
BUTTON_POLLING = False
_btn_last_state = 1
_btn_last_toggle_ts = 0.0
BTN_DEBOUNCE_MS = 250

def _cycle_mode(channel=None):
    """
    Cycle display mode: PAGES -> SAVER -> OFF -> PAGES ...
    """
    global display_mode
    display_mode = (display_mode + 1) % 3  # 0..2
    debug(f"Button: cycle -> mode {display_mode}")

def setup_button(pin=SCREENSAVER_BUTTON_PIN):
    global BUTTON_EVENT_OK, BUTTON_POLLING, _btn_last_state
    if GPIO is None:
        debug("GPIO unavailable; button disabled")
        return
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        # Remove any prior registrations just in case
        try:
            GPIO.remove_event_detect(pin)
        except Exception:
            pass
        GPIO.add_event_detect(pin, GPIO.FALLING, callback=_cycle_mode, bouncetime=300)
        BUTTON_EVENT_OK = True
        BUTTON_POLLING = False
        debug(f"Button edge-detect active on BCM {pin}")
    except Exception as e:
        debug(f"Edge detect setup failed ({e}); falling back to polling")
        BUTTON_EVENT_OK = False
        try:
            # Ensure pin still configured
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            _btn_last_state = GPIO.input(pin)
            BUTTON_POLLING = True
            debug("Polling mode enabled for button")
        except Exception as e2:
            BUTTON_POLLING = False
            debug(f"Polling fallback failed: {e2}; button disabled")

def poll_button(pin=SCREENSAVER_BUTTON_PIN):
    """
    Polling fallback for button (falling edge). Software debounce.
    """
    global _btn_last_state, _btn_last_toggle_ts
    if not BUTTON_POLLING or GPIO is None:
        return
    try:
        state = GPIO.input(pin)
    except Exception as e:
        debug(f"GPIO input error: {e}")
        return
    if _btn_last_state == 1 and state == 0:  # falling edge
        now = time.time()
        if (now - _btn_last_toggle_ts) * 1000.0 > BTN_DEBOUNCE_MS:
            _btn_last_toggle_ts = now
            _cycle_mode()
    _btn_last_state = state

# Responsive sleep with button polling
def sleep_poll(duration, slice_sec=0.05):
    """
    Sleep up to 'duration' seconds in small slices while polling the button
    so mode changes are near-immediate. Exits early if mode leaves PAGES.
    """
    start = time.time()
    while True:
        poll_button()
        if display_mode != MODE_PAGES:
            break
        now = time.time()
        if now - start >= duration:
            break
        time.sleep(min(slice_sec, duration - (now - start)))

# -----------------------------
# Utility helpers (shell-safe)
# -----------------------------

def run(cmd, timeout=1.0):
    """
    Run a shell command, return (stdout_str, returncode).
    On failure or timeout, returns ("", nonzero).
    """
    try:
        out = subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, timeout=timeout, shell=True
        )
        return out.decode("utf-8", "ignore").strip(), 0
    except Exception:
        return "", 1


def get_hostname():
    return socket.gethostname()


def get_time_str():
    # Local time in HH:MM:SS; tweak as you prefer
    return datetime.now().strftime("%H:%M:%S")


def get_uptime_str():
    try:
        with open("/proc/uptime") as f:
            seconds = float(f.read().split()[0])
        td = timedelta(seconds=int(seconds))
        # Short human-ish form: Dd HH:MM
        days = td.days
        hours, rem = divmod(td.seconds, 3600)
        minutes, _ = divmod(rem, 60)
        if days > 0:
            return f"{days}d {hours:02d}:{minutes:02d}"
        return f"{hours:02d}:{minutes:02d}"
    except Exception:
        return "uptime: ?"    


def get_iface_ip(iface):
    """
    Return IPv4 address of an interface via `ip -4 addr show`.


    """
    out, rc = run(f"/sbin/ip -4 addr show dev {iface}")
    if rc != 0 or "inet " not in out:
        return "-"
    # Parse like: "inet 192.168.1.2/24 ..."


    for line in out.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            return line.split()[1].split("/")[0]
    return "-"    


def get_default_route_ip():
    """
    Use a route lookup to a public IP to infer the WAN source IP (works for NATed).
    """
    out, rc = run("/sbin/ip -4 route get 1.1.1.1")
    if rc == 0 and "src " in out:
        try:
            return out.split("src ")[1].split()[0]
        except Exception:
            pass
    return "-"    


def internet_ok():
    """
    Cheap reachability: one ping to Cloudflare DNS with a 1s timeout.
    """
    _, rc = run("/bin/ping -c1 -W1 1.1.1.1")
    return rc == 0


def get_wifi_info(iface="wlan0"):
    """
    Parse `iw dev wlan0 link` for SSID and RSSI. Returns (ssid, rssi_dBm or None).
    """
    out, rc = run(f"/usr/sbin/iw dev {iface} link")
    if rc != 0:
        return ("wifi: down", None)
    ssid, rssi = "-", None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            ssid = line.split("SSID:", 1)[1].strip()
        if line.startswith("signal:"):
            # e.g., "signal: -57 dBm"
            try:
                rssi = int(line.split()[1])
            except Exception:
                rssi = None
    if "Not connected." in out:
        return ("wifi: not conn", None)
    return (ssid, rssi)


def count_dnsmasq_leases(path="/var/lib/misc/dnsmasq.leases"):
    """
    Count active DHCP leases by reading dnsmasq's leases file.
    """
    try:
        with open(path, "r") as f:
            # Each non-empty line is a lease record
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


def get_tailscale_ip():
    """
    Get the first IPv4 address from Tailscale (if installed).
    """
    out, rc = run("/usr/bin/tailscale ip -4")
    if rc != 0 or not out:
        return "-"
    # tailscale ip prints one per line; prefer the 100.x address
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    if not lines:
        return "-"
    # Heuristic: choose 100.x first, else first line
    for l in lines:
        if l.startswith("100."):
            return l
    return lines[0]


class Throughput:
    """
    Track per-interface throughput from rx/tx byte counters.
    """
    def __init__(self):
        self.prev = {}  # iface -> (ts, rx_bytes, tx_bytes)

    def read_bytes(self, iface):
        try:
            with open(f"/sys/class/net/{iface}/statistics/rx_bytes") as f:
                rx = int(f.read().strip())
            with open(f"/sys/class/net/{iface}/statistics/tx_bytes") as f:
                tx = int(f.read().strip())
            return (rx, tx)
        except Exception:
            return (None, None)

    def kbit_s(self, iface, interval=1.0):
        """
        Return (rx_kbit_s, tx_kbit_s). On first call per iface, returns (0,0).
        """
        now = time.time()
        rx, tx = self.read_bytes(iface)
        if rx is None or tx is None:
            return (0, 0)
        if iface not in self.prev:
            self.prev[iface] = (now, rx, tx)
            return (0, 0)
        ts_prev, rx_prev, tx_prev = self.prev[iface]
        dt = max(0.001, now - ts_prev)
        rx_kbit = int(((rx - rx_prev) * 8) / 1000 / dt)
        tx_kbit = int(((tx - tx_prev) * 8) / 1000 / dt)
        self.prev[iface] = (now, rx, tx)
        return (max(0, rx_kbit), max(0, tx_kbit))


def get_cpu_temp_c():
    """
    Prefer sysfs thermal zone; fall back to vcgencmd if needed.
    """
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return float(f.read().strip()) / 1000.0
    except Exception:
        out, rc = run("/usr/bin/vcgencmd measure_temp")
        if rc == 0 and "temp=" in out:
            try:
                return float(out.split("temp=")[1].split("'")[0])
            except Exception:
                return None
    return None


def get_loadavg():
    try:
        one, five, fifteen = os.getloadavg()
        return (one, five, fifteen)
    except Exception:
        return (0.0, 0.0, 0.0)


def get_mem_usage_mb():
    """
    Return (used_mb, total_mb) from /proc/meminfo.
    """
    try:
        data = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                data[k.strip()] = v.strip()
        def kB(name): return int(data[name].split()[0])
        total = kB("MemTotal")
        avail = kB("MemAvailable")
        used = total - avail
        return int(used / 1024), int(total / 1024)
    except Exception:
        return None, None

class CPUUsage:
    """
    Track CPU usage percentage using /proc/stat deltas.
    """
    def __init__(self):
        self.prev_total = None
        self.prev_idle = None

    def percent(self):
        try:
            with open("/proc/stat") as f:
                line = f.readline()
            if not line.startswith("cpu "):
                return 0.0
            parts = line.split()
            # user nice system idle iowait irq softirq steal
            vals = list(map(int, parts[1:9]))
            user, nice, system, idle, iowait, irq, softirq, steal = vals
            idle_all = idle + iowait
            total = sum(vals)
            if self.prev_total is None:
                self.prev_total = total
                self.prev_idle = idle_all
                return 0.0
            dt_total = total - self.prev_total
            dt_idle = idle_all - self.prev_idle
            self.prev_total = total
            self.prev_idle = idle_all
            if dt_total <= 0:
                return 0.0
            usage = (dt_total - dt_idle) / dt_total * 100.0
            return max(0.0, min(100.0, usage))
        except Exception:
            return 0.0


# -----------------------------
# Display helpers
# -----------------------------

def make_device(i2c_port=1, i2c_address=0x3C, rotate=0):
    serial = i2c(port=i2c_port, address=i2c_address)
    dev = sh1106(serial, rotate=rotate)
    # Set contrast if you like; 0..255
    # dev.contrast(255)
    return dev


def draw_lines(device, lines, small=False):
    """
    Draw up to 6 lines of text neatly spaced on the 128x64 canvas.
    """
    width = device.width
    height = device.height
    image = Image.new("1", (width, height))
    draw = ImageDraw.Draw(image)
    # Use default bitmap font for crispness
    font = ImageFont.load_default()
    line_h = 10 if small else 11
    y = 0
    for text in lines[:6]:
        draw.text((0, y), text, font=font, fill=255)
        y += line_h
    device.display(image)


def bar(draw, x, y, w, h, frac):
    """
    Simple horizontal bar for visual throughput or RSSI.
    """
    frac = max(0.0, min(1.0, frac))
    draw.rectangle((x, y, x+w-1, y+h-1), outline=1, fill=0)
    fill_w = int((w-2) * frac)
    if fill_w > 0:
        draw.rectangle((x+1, y+1, x+1+fill_w, y+h-2), outline=1, fill=1)


def draw_throughput(device, eth_k, wlan_k):
    """
    Draw a slightly richer page with bars.
    eth_k, wlan_k: tuples (rx_kbit, tx_kbit)
    Bar scale: total (RX+TX) capped at ~5 Mbit/s (adjust divisor if desired).
    """
    width, height = device.width, device.height
    image = Image.new("1", (width, height))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    # Titles
    draw.text((0, 0), "THROUGHPUT (kbit/s)", font=font, fill=255)
    # ETH0
    erx, etx = eth_k
    draw.text((0, 14), f"eth0 RX:{erx:4d}  TX:{etx:4d}", font=font, fill=255)
    bar(draw, 0, 24, width, 8, min(1.0, (erx + etx) / 5000.0))  # scale bar vs ~5 Mbit/s

    # WLAN0
    wrx, wtx = wlan_k
    draw.text((0, 38), f"wlan0 RX:{wrx:4d} TX:{wtx:4d}", font=font, fill=255)
    bar(draw, 0, 48, width, 8, min(1.0, (wrx + wtx) / 5000.0))

    device.display(image)


def draw_wifi_page(device, wan_ip, ssid, rssi, ok):
    """
    WAN / Wi-Fi page with explicit signal line (dBm); removed unlabeled bar.
    """
    width, height = device.width, device.height
    image = Image.new("1", (width, height))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    draw.text((0, 0), "WAN/ Wi-Fi", font=font, fill=255)
    draw.text((0, 12), f"WAN: {wan_ip}", font=font, fill=255)
    status = "OK" if ok else "NO NET"
    draw.text((0, 24), f"NET: {status}", font=font, fill=255)
    draw.text((0, 36), f"SSID: {ssid[:16]}", font=font, fill=255)
    sig_txt = "sig: ?" if rssi is None else f"sig: {rssi}dBm"
    draw.text((0, 48), sig_txt, font=font, fill=255)
    device.display(image)


def draw_system_page(device, temp_c, cpu_pct, load_tuple, mem_used_mb, mem_total_mb):
    """
    SYSTEM page without icons: temp, cpu %, load averages, memory used/total.
    """
    width, height = device.width, device.height
    image = Image.new("1", (width, height))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    draw.text((0, 0), "SYSTEM", font=font, fill=255)
    draw.text((0, 12), f"temp  {temp_c:.1f}C" if temp_c is not None else "temp  ?", font=font, fill=255)
    draw.text((0, 24), f"cpu   {cpu_pct:5.1f}%", font=font, fill=255)
    draw.text((0, 36), f"load  {load_tuple[0]:.2f} {load_tuple[1]:.2f} {load_tuple[2]:.2f}", font=font, fill=255)
    if mem_used_mb is not None and mem_total_mb is not None:
        draw.text((0, 48), f"mem   {mem_used_mb}/{mem_total_mb} MB", font=font, fill=255)
    else:
        draw.text((0, 48), "mem   ?", font=font, fill=255)
    device.display(image)

# -----------------------------
# Screensaver bitmap config
SCREENSAVER_BITMAP_PATH = os.environ.get("SCREENSAVER_BITMAP", "raspberry.bmp")
_SAVER_SPRITE = None

def _resolve_bitmap_path(p):
    """
    Return an absolute path for the bitmap. If relative, try:
      1) Current working directory
      2) Script directory
    """
    if os.path.isabs(p):
        return p
    # Try as-is first
    if os.path.exists(p):
        return os.path.abspath(p)
    # Try script dir
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        cand = os.path.join(script_dir, p)
        if os.path.exists(cand):
            return cand
    except Exception:
        pass
    return p  # return original (load will fail and fallback will trigger)

def load_saver_sprite(max_side=42):
    """
    Load and cache the screensaver bitmap from SCREENSAVER_BITMAP_PATH.
    - Converts to 1-bit
    - Trims surrounding black border
    - Scales so the longer side <= max_side (nearest neighbor)
    """
    global _SAVER_SPRITE
    if _SAVER_SPRITE is not None:
        return _SAVER_SPRITE
    try:
        p = _resolve_bitmap_path(SCREENSAVER_BITMAP_PATH)
        img = Image.open(p)
        if img.mode != "1":
            img = img.convert("L").point(lambda v: 255 if v > 128 else 0, mode="1")
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        w, h = img.size
        scale = min(1.0, max_side / max(w, h))
        if scale < 1.0:
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            img = img.resize(new_size, Image.NEAREST)
        _SAVER_SPRITE = img
        debug(f"Screensaver sprite loaded: {p} size={_SAVER_SPRITE.size}")
        return _SAVER_SPRITE
    except Exception as e:
        debug(f"Failed to load screensaver bitmap '{SCREENSAVER_BITMAP_PATH}': {e}")
        placeholder = Image.new("1", (32, 32), 0)
        d = ImageDraw.Draw(placeholder)
        d.rectangle((0, 0, 31, 31), outline=1, fill=0)
        d.text((4, 10), "NO", font=ImageFont.load_default(), fill=1)
        d.text((4, 20), "IMG", font=ImageFont.load_default(), fill=1)
        _SAVER_SPRITE = placeholder
        return _SAVER_SPRITE

def bouncing_raspberry(device, fps=25):
    """
    Run while in MODE_SAVER using the external bitmap sprite.
    """
    global display_mode
    sprite = load_saver_sprite()
    sw, sh = sprite.size
    width, height = device.width, device.height
    if width == 0 or height == 0:
        debug("Device dimensions invalid; aborting screensaver")
        return
    # Safety: shrink sprite if larger than display (unexpected)
    if sw > width or sh > height:
        scale = min(width / sw, height / sh, 1.0)
        if scale < 1.0:
            new_size = (max(1, int(sw * scale)), max(1, int(sh * scale)))
            sprite = sprite.resize(new_size, Image.NEAREST)
            sw, sh = sprite.size
    x, y = (width - sw) // 2, (height - sh) // 2
    vx, vy = 1, 1
    dt = 1.0 / max(1, fps)
    font = ImageFont.load_default()
    debug("Entering screensaver")
    while display_mode == MODE_SAVER:
        poll_button()
        if display_mode != MODE_SAVER:
            break
        frame = Image.new("1", (width, height))
        # Title (remove if pure image desired)
        ImageDraw.Draw(frame).text((0, 0), "Raspberry Pi", font=font, fill=1)
        frame.paste(sprite, (x, y), sprite)
        device.display(frame)
        x += vx
        y += vy
        if x <= 0 or x + sw >= width:
            vx = -vx
            x = max(0, min(x, width - sw))
        if y <= 10 or y + sh >= height:
            vy = -vy
            y = max(10, min(y, height - sh))
        time.sleep(dt)
    debug("Leaving screensaver")
    time.sleep(0.05)

# -----------------------------
# Main loop
# -----------------------------

def main():
    debug("Starting status_screen")
    try:
        device = make_device()
        debug("OLED device initialized")
    except Exception as e:
        debug(f"OLED init failed: {e}")
        traceback.print_exc()
        return
    tput = Throughput()
    cpu_usage = CPUUsage()
    setup_button()
    pages = ["host", "wan", "lan", "ts", "tput", "sys"]
    page_idx = 0
    page_sw_every = 5.0
    last_switch = 0.0
    off_cleared = False  # track if we've already blanked in OFF mode

    try:
        while True:
            # Always poll in case event detect missed or we are in fallback
            poll_button()

            mode = display_mode

            if mode == MODE_SAVER:
                if DEBUG: debug("Mode = SAVER")
                if off_cleared: debug("Leaving OFF mode")
                bouncing_raspberry(device)  # returns when mode changes
                continue
            if mode == MODE_OFF:
                if DEBUG: debug("Mode = OFF")
                if not off_cleared:
                    blank_screen(device)
                    off_cleared = True
                time.sleep(0.25)
                continue
            else:
                if DEBUG and off_cleared: debug("Mode = PAGES (resuming)")
                off_cleared = False

            now = time.time()
            if now - last_switch >= page_sw_every:
                page_idx = (page_idx + 1) % len(pages)
                last_switch = now

            page = pages[page_idx]

            if DEBUG:
                debug(f"Page={page}")

            try:
                if page == "host":
                    lines = [
                        f"{get_hostname()}",
                        f"time  {get_time_str()}",
                        f"up    {get_uptime_str()}",
                        f"WAN   {get_default_route_ip()}",
                        f"LAN   {get_iface_ip('eth0')}",
                    ]
                    draw_lines(device, lines)
                elif page == "wan":
                    wan_ip = get_default_route_ip()
                    ssid, rssi = get_wifi_info("wlan0")
                    ok = internet_ok()
                    draw_wifi_page(device, wan_ip, ssid, rssi, ok)
                elif page == "lan":
                    leases = count_dnsmasq_leases()
                    lines = [
                        "LAN / DHCP",
                        f"eth0  {get_iface_ip('eth0')}",
                        f"leases {leases}",
                        "",
                        "",
                    ]
                    draw_lines(device, lines)
                elif page == "ts":
                    lines = [
                        "TAILSCALE",
                        f"IP4  {get_tailscale_ip()}",
                        "",
                        "",
                        "",
                    ]
                    draw_lines(device, lines)
                elif page == "tput":
                    # replace fixed 1s sleep with responsive sleep
                    sleep_poll(1.0)
                    eth = tput.kbit_s("eth0")
                    wlan = tput.kbit_s("wlan0")
                    draw_throughput(device, eth, wlan)
                elif page == "sys":
                    temp = get_cpu_temp_c()
                    load = get_loadavg()
                    cpu_pct = cpu_usage.percent()
                    mem_used, mem_total = get_mem_usage_mb()
                    draw_system_page(device, temp, cpu_pct, load, mem_used, mem_total)
            except Exception as e:
                debug(f"Page render error ({page}): {e}")
                traceback.print_exc()
                draw_lines(device, ["OLED error", "retrying..."])
                time.sleep(0.5)

            if page != "tput":
                sleep_poll(1.0)
    except KeyboardInterrupt:
        debug("Interrupted by user")
    except Exception as e:
        debug(f"Fatal loop error: {e}")
        traceback.print_exc()
    finally:
        if GPIO:
            try:
                GPIO.cleanup()
                debug("GPIO cleaned up")
            except Exception as e:
                debug(f"GPIO cleanup error: {e}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        debug(f"Top-level exception: {e}")
        traceback.print_exc()