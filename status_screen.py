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

Button (BCM17 -> GND) cycles modes: Pages -> Screensaver -> Off.

Place Raspberry_Pi_Logo.bmp (1‑bit or any format convertible) in the same directory for the screensaver.
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

# --- Debug (manual toggle if needed) ------------------------------------------
DEBUG = False
def debug(msg): 
    if DEBUG:
        try:
            print(f"[SCREEN] {time.strftime('%H:%M:%S')} {msg}", flush=True)
        except Exception:
            pass

# --- Button toggle (GPIO) -----------------------------------------------------
try:
    import RPi.GPIO as GPIO
except Exception:
    GPIO = None
    debug("GPIO import failed")

SCREENSAVER_BUTTON_PIN = 17  # BCM (GPIO) pin number; wire to a momentary button to GND

# Display modes
MODE_PAGES = 0
MODE_SAVER = 1
MODE_OFF   = 2
display_mode = MODE_PAGES

# Button fallback / polling state
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
    debug(f"Mode -> {display_mode}")

def setup_button(pin=SCREENSAVER_BUTTON_PIN):
    global BUTTON_POLLING, _btn_last_state
    if GPIO is None:
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
        debug(f"Edge detect on BCM {pin}")
    except Exception:
        debug("Edge detect failed; using polling")
        try:
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            _btn_last_state = GPIO.input(pin)
            BUTTON_POLLING = True
        except Exception:
            BUTTON_POLLING = False
            debug("Polling setup failed")

def poll_button(pin=SCREENSAVER_BUTTON_PIN):
    """
    Polling fallback for button (falling edge). Software debounce.
    """
    global _btn_last_state, _btn_last_toggle_ts
    if not BUTTON_POLLING or GPIO is None:
        return
    try:
        state = GPIO.input(pin)
    except Exception:
        return
    if _btn_last_state == 1 and state == 0:  # falling edge
        now = time.time()
        if (now - _btn_last_toggle_ts) * 1000.0 > BTN_DEBOUNCE_MS:
            _btn_last_toggle_ts = now
            _cycle_mode()
    _btn_last_state = state

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

# --- Utility helpers ----------------------------------------------------------
def run(cmd, timeout=1.0):
    """
    Run a shell command, return (stdout_str, returncode).
    On failure or timeout, returns ("", nonzero).
    """
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=timeout, shell=True)
        return out.decode("utf-8", "ignore").strip(), 0
    except Exception:
        return "", 1

def get_hostname(): return socket.gethostname()
def get_time_str(): return datetime.now().strftime("%H:%M:%S")

def get_uptime_str():
    try:
        with open("/proc/uptime") as f:
            seconds = float(f.read().split()[0])
        td = timedelta(seconds=int(seconds))
        days = td.days
        hours, rem = divmod(td.seconds, 3600)
        minutes, _ = divmod(rem, 60)
        return f"{days}d {hours:02d}:{minutes:02d}" if days else f"{hours:02d}:{minutes:02d}"
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
    if "Not connected." in out:
        return ("wifi: not conn", None)
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
    return (ssid, rssi)


def count_dnsmasq_leases(path="/var/lib/misc/dnsmasq.leases"):
    """
    Count active DHCP leases by reading dnsmasq's leases file.
    """
    try:
        with open(path) as f:
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
    def __init__(self): self.prev = {}
    def read_bytes(self, iface):
        try:
            with open(f"/sys/class/net/{iface}/statistics/rx_bytes") as f:
                rx = int(f.read().strip())
            with open(f"/sys/class/net/{iface}/statistics/tx_bytes") as f:
                tx = int(f.read().strip())
            return rx, tx
        except Exception:
            return None, None
    def kbit_s(self, iface):
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
        rx_k = int(((rx - rx_prev) * 8) / 1000 / dt)
        tx_k = int(((tx - tx_prev) * 8) / 1000 / dt)
        self.prev[iface] = (now, rx, tx)
        return (max(0, rx_k), max(0, tx_k))


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
        return os.getloadavg()
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
        def kB(n): return int(data[n].split()[0])
        total = kB("MemTotal"); avail = kB("MemAvailable")
        used = total - avail
        return int(used / 1024), int(total / 1024)
    except Exception:
        return (None, None)

class CPUUsage:
    """
    Track CPU usage percentage using /proc/stat deltas.
    """
    def __init__(self): self.prev_total = None; self.prev_idle = None
    def percent(self):
        try:
            with open("/proc/stat") as f:
                line = f.readline()
            if not line.startswith("cpu "):
                return 0.0
            parts = list(map(int, line.split()[1:9]))
            user, nice, system, idle, iowait, irq, softirq, steal = parts
            idle_all = idle + iowait
            total = sum(parts)
            if self.prev_total is None:
                self.prev_total, self.prev_idle = total, idle_all
                return 0.0
            dt_total = total - self.prev_total
            dt_idle = idle_all - self.prev_idle
            self.prev_total, self.prev_idle = total, idle_all
            if dt_total <= 0:
                return 0.0
            return max(0.0, min(100.0, (dt_total - dt_idle) / dt_total * 100.0))
        except Exception:
            return 0.0


# --- Display helpers ----------------------------------------------------------
def make_device(i2c_port=1, i2c_address=0x3C, rotate=0):
    serial = i2c(port=i2c_port, address=i2c_address)
    return sh1106(serial, rotate=rotate)

def draw_lines(device, lines, small=False):
    """
    Draw up to 6 lines of text neatly spaced on the 128x64 canvas.
    """
    image = Image.new("1", (device.width, device.height))
    d = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    line_h = 10 if small else 11
    y = 0
    for text in lines[:6]:
        d.text((0, y), text, font=font, fill=255)
        y += line_h
    device.display(image)

def blank_screen(device):
    """Blank (turn off) the OLED contents."""
    try:
        device.display(Image.new("1", (device.width, device.height), 0))
    except Exception:
        pass

def bar(draw, x, y, w, h, frac):
    """
    Simple horizontal bar for visual throughput or RSSI.
    """
    frac = max(0.0, min(1.0, frac))
    draw.rectangle((x, y, x+w-1, y+h-1), outline=1, fill=0)
    fw = int((w - 2) * frac)
    if fw > 0:
        draw.rectangle((x+1, y+1, x+1+fw, y+h-2), outline=1, fill=1)

def draw_throughput(device, eth_k, wlan_k):
    """
    Draw a slightly richer page with bars.
    eth_k, wlan_k: tuples (rx_kbit, tx_kbit)
    Bar scale: total (RX+TX) capped at ~5 Mbit/s (adjust divisor if desired).
    """
    width = device.width
    image = Image.new("1", (width, device.height))
    d = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    d.text((0, 0), "THROUGHPUT (kbit/s)", font=font, fill=255)
    erx, etx = eth_k
    d.text((0, 14), f"eth0 RX:{erx:4d}  TX:{etx:4d}", font=font, fill=255)
    bar(d, 0, 24, width, 8, min(1.0, (erx + etx)/5000.0))
    wrx, wtx = wlan_k
    d.text((0, 38), f"wlan0 RX:{wrx:4d} TX:{wtx:4d}", font=font, fill=255)
    bar(d, 0, 48, width, 8, min(1.0, (wrx + wtx)/5000.0))
    device.display(image)

def draw_wifi_page(device, wan_ip, ssid, rssi, ok):
    """
    WAN / Wi-Fi page with explicit signal line (dBm); removed unlabeled bar.
    """
    image = Image.new("1", (device.width, device.height))
    d = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    d.text((0, 0), "WAN/ Wi-Fi", font=font, fill=255)
    d.text((0, 12), f"WAN: {wan_ip}", font=font, fill=255)
    d.text((0, 24), f"NET: {'OK' if ok else 'NO NET'}", font=font, fill=255)
    d.text((0, 36), f"SSID: {ssid[:16]}", font=font, fill=255)
    d.text((0, 48), "sig: ?" if rssi is None else f"sig: {rssi}dBm", font=font, fill=255)
    device.display(image)

def draw_system_page(device, temp_c, cpu_pct, load_tuple, mem_used_mb, mem_total_mb):
    """
    SYSTEM page without icons: temp, cpu %, load averages, memory used/total.
    """
    image = Image.new("1", (device.width, device.height))
    d = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    d.text((0, 0), "SYSTEM", font=font, fill=255)
    d.text((0, 12), f"temp  {temp_c:.1f}C" if temp_c is not None else "temp  ?", font=font, fill=255)
    d.text((0, 24), f"cpu   {cpu_pct:5.1f}%", font=font, fill=255)
    d.text((0, 36), f"load  {load_tuple[0]:.2f} {load_tuple[1]:.2f} {load_tuple[2]:.2f}", font=font, fill=255)
    if mem_used_mb is not None and mem_total_mb is not None:
        d.text((0, 48), f"mem   {mem_used_mb}/{mem_total_mb} MB", font=font, fill=255)
    else:
        d.text((0, 48), "mem   ?", font=font, fill=255)
    device.display(image)

# --- Screensaver bitmap -------------------------------------------------------
SCREENSAVER_BITMAP_FILENAME = "Raspberry_Pi_Logo.bmp"
_SAVER_SPRITE = None

def load_saver_sprite(max_side=48):
    """
    Load Raspberry_Pi_Logo.bmp from the script directory.
    Convert to 1-bit and shrink if larger than max_side.
    """
    global _SAVER_SPRITE
    if _SAVER_SPRITE is not None:
        return _SAVER_SPRITE
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SCREENSAVER_BITMAP_FILENAME)
        img = Image.open(path)
        if img.mode != "1":
            img = img.convert("L").point(lambda v: 255 if v > 128 else 0, mode="1")
        w, h = img.size
        if max(w, h) > max_side:
            scale = max_side / max(w, h)
            img = img.resize((max(1, int(w*scale)), max(1, int(h*scale))), Image.NEAREST)
        _SAVER_SPRITE = img
    except Exception:
        ph = Image.new("1", (24, 24), 0)
        d = ImageDraw.Draw(ph)
        d.rectangle((0, 0, 23, 23), outline=1, fill=0)
        d.text((4, 8), "NO", font=ImageFont.load_default(), fill=1)
        d.text((4, 16), "IMG", font=ImageFont.load_default(), fill=1)
        _SAVER_SPRITE = ph
    return _SAVER_SPRITE

def bouncing_raspberry(device, fps=25):
    """
    Bounce the bitmap sprite (no extra title text).
    """
    global display_mode
    sprite = load_saver_sprite()
    sw, sh = sprite.size
    width, height = device.width, device.height
    if width == 0 or height == 0:
        return
    # Safety shrink (unexpected)
    if sw > width or sh > height:
        scale = min(width / sw, height / sh, 1.0)
        sprite = sprite.resize((max(1, int(sw*scale)), max(1, int(sh*scale))), Image.NEAREST)
        sw, sh = sprite.size
    x, y = (width - sw)//2, (height - sh)//2
    vx, vy = 1, 1
    dt = 1.0 / max(1, fps)
    while display_mode == MODE_SAVER:
        poll_button()
        if display_mode != MODE_SAVER:
            break
        frame = Image.new("1", (width, height))
        frame.paste(sprite, (x, y), sprite)
        device.display(frame)
        x += vx; y += vy
        if x <= 0 or x + sw >= width:
            vx = -vx; x = max(0, min(x, width - sw))
        if y <= 0 or y + sh >= height:
            vy = -vy; y = max(0, min(y, height - sh))
        time.sleep(dt)
    time.sleep(0.05)

# --- Main loop ----------------------------------------------------------------
def main():
    debug("Starting")
    try:
        device = make_device()
    except Exception as e:
        print(f"OLED init failed: {e}")
        traceback.print_exc()
        return
    tput = Throughput()
    cpu_usage = CPUUsage()
    setup_button()
    pages = ["host", "wan", "lan", "ts", "tput", "sys"]
    page_idx = 0
    page_sw_every = 5.0
    last_switch = 0.0
    off_cleared = False
    try:
        while True:
            poll_button()
            mode = display_mode
            if mode == MODE_SAVER:
                if off_cleared: off_cleared = False
                bouncing_raspberry(device)
                continue
            if mode == MODE_OFF:
                if not off_cleared:
                    blank_screen(device)
                    off_cleared = True
                time.sleep(0.25)
                continue
            else:
                if off_cleared:
                    off_cleared = False
            now = time.time()
            if now - last_switch >= page_sw_every:
                page_idx = (page_idx + 1) % len(pages)
                last_switch = now
            page = pages[page_idx]
            try:
                if page == "host":
                    draw_lines(device, [
                        get_hostname(),
                        f"time  {get_time_str()}",
                        f"up    {get_uptime_str()}",
                        f"WAN   {get_default_route_ip()}",
                        f"LAN   {get_iface_ip('eth0')}",
                    ])
                elif page == "wan":
                    wan_ip = get_default_route_ip()
                    ssid, rssi = get_wifi_info("wlan0")
                    draw_wifi_page(device, wan_ip, ssid, rssi, internet_ok())
                elif page == "lan":
                    draw_lines(device, [
                        "LAN / DHCP",
                        f"eth0  {get_iface_ip('eth0')}",
                        f"leases {count_dnsmasq_leases()}",
                        "",
                        "",
                    ])
                elif page == "ts":
                    draw_lines(device, [
                        "TAILSCALE",
                        f"IP4  {get_tailscale_ip()}",
                        "",
                        "",
                        "",
                    ])
                elif page == "tput":
                    sleep_poll(1.0)
                    draw_throughput(device, tput.kbit_s("eth0"), tput.kbit_s("wlan0"))
                elif page == "sys":
                    temp = get_cpu_temp_c()
                    load = get_loadavg()
                    cpu_pct = cpu_usage.percent()
                    mem_used, mem_total = get_mem_usage_mb()
                    draw_system_page(device, temp, cpu_pct, load, mem_used, mem_total)
            except Exception:
                draw_lines(device, ["OLED error", "retrying..."])
                time.sleep(0.5)
            if page != "tput":
                sleep_poll(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        if GPIO:
            try: GPIO.cleanup()
            except Exception: pass

if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()