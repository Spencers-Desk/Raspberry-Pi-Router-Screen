#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Small status dashboard for a Raspberry Pi router on a 128x64 SH1106 I2C OLED.

Pages:
  1) Hostname, time, uptime
  2) WAN IP, Internet reachability, Wi-Fi SSID & RSSI (wlan0)
  3) LAN IP (eth0), DHCP lease count (dnsmasq)
  4) Tailscale IPv4 (if installed)
  5) Throughput (kbit/s) for eth0 and wlan0
  6) CPU temp, load, memory used

Requirements:
  sudo apt install python3-pil python3-smbus i2c-tools python3-rpi.gpio
  pip3 install luma.oled
"""

import os
import time
import socket
import subprocess
from datetime import datetime, timedelta

from PIL import Image, ImageDraw, ImageFont

from luma.core.interface.serial import i2c
from luma.oled.device import sh1106

# --- Button toggle (GPIO) -----------------------------------------------------
import RPi.GPIO as GPIO
SCREENSAVER_BUTTON_PIN = 17  # BCM pin number; wire to a momentary button to GND
screensaver_mode = False

def _toggle_mode(channel):
    """GPIO callback: toggle between page rotation and screensaver."""
    global screensaver_mode
    screensaver_mode = not screensaver_mode

def setup_button(pin=SCREENSAVER_BUTTON_PIN):
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)  # button to GND
    GPIO.add_event_detect(pin, GPIO.FALLING, callback=_toggle_mode, bouncetime=300)

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


def get_mem_used_mb():
    """
    Read MemTotal and MemAvailable from /proc/meminfo.
    """
    try:
        data = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                data[k.strip()] = v.strip()
        def kB(name):
            return int(data[name].split()[0])
        total = kB("MemTotal")
        avail = kB("MemAvailable")
        used = total - avail
        return int(used / 1024)
    except Exception:
        return None


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
    width, height = device.width, device.height
    image = Image.new("1", (width, height))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    draw.text((0, 0), "WAN/ Wi-Fi", font=font, fill=255)
    draw.text((0, 12), f"WAN: {wan_ip}", font=font, fill=255)
    status = "OK" if ok else "NO NET"
    draw.text((0, 24), f"NET: {status}", font=font, fill=255)
    draw.text((0, 36), f"SSID: {ssid[:16]}", font=font, fill=255)
    # RSSI bar: assume -30dBm best, -90 worst
    frac = 0.0
    if rssi is not None:
        frac = max(0.0, min(1.0, (rssi + 90) / 60.0))
    bar(draw, 0, 50, width, 10, frac)
    device.display(image)

# --- Fun: Bouncing Raspberry "logo" ------------------------------------------
# from PIL import Image, ImageDraw, ImageFont  # already imported above
def frame_raspberry(image, x, y, scale=1.0):
    """
    Draw a simple raspberry (3-circle berry + leaves) onto the provided 1-bit PIL Image.
    (x, y) is the top-left of the drawing box.
    """
    draw = ImageDraw.Draw(image)
    # Basic proportions for a ~32x32 berry before scaling
    S = scale
    bx, by = int(x), int(y)

    # Berry body: three overlapping circles
    # Center positions relative to (x,y)
    circles = [
        (bx + int(10*S), by + int(10*S), int(10*S)),  # left
        (bx + int(22*S), by + int(10*S), int(10*S)),  # right
        (bx + int(16*S), by + int(4*S),  int(10*S)),  # top
    ]
    for cx, cy, r in circles:
        draw.ellipse((cx-r, cy-r, cx+r, cy+r), fill=1, outline=1)

    # Leaves: two small circles/ovals on top
    draw.ellipse((bx + int(10*S), by + int(-2*S), bx + int(16*S), by + int(4*S)), fill=1, outline=1)
    draw.ellipse((bx + int(16*S), by + int(-2*S), bx + int(22*S), by + int(4*S)), fill=1, outline=1)

def bouncing_raspberry(device, seconds=8, fps=25):
    """
    Animate a bouncing raspberry within the OLED bounds.
    When screensaver_mode is toggled off, this returns immediately.
    """
    global screensaver_mode
    width, height = device.width, device.height
    # Start roughly centered; sprite is ~32x32
    w, h = 32, 32
    x, y = (width - w) // 2, (height - h) // 2
    vx, vy = 1, 1  # pixels per frame
    dt = 1.0 / max(1, fps)
    end_time = time.time() + seconds

    while time.time() < end_time and screensaver_mode:
        img = Image.new("1", (width, height))
        # Draw the sprite
        frame_raspberry(img, x, y, scale=1.0)
        # Optional caption
        ImageDraw.Draw(img).text((0, 0), "Raspberry Pi", font=ImageFont.load_default(), fill=1)
        device.display(img)

        # Advance position
        x += vx
        y += vy

        # Bounce on edges
        if x <= 0 or x + w >= width:
            vx = -vx
            x = max(0, min(x, width - w))
        if y <= 10 or y + h >= height:  # keep below caption line
            vy = -vy
            y = max(10, min(y, height - h))

        time.sleep(dt)


# -----------------------------
# Main loop
# -----------------------------

def main():
    device = make_device()
    tput = Throughput()
    setup_button()
    pages = ["host", "wan", "lan", "ts", "tput", "sys"]
    page_idx = 0
    page_sw_every = 5.0   # seconds per page
    last_switch = 0.0

    try:
        while True:
            # If in screensaver mode, run the animation until toggled off
            if screensaver_mode:
                bouncing_raspberry(device, seconds=3600, fps=25)
                continue

            now = time.time()
            if now - last_switch >= page_sw_every:
                page_idx = (page_idx + 1) % len(pages)
                last_switch = now

            page = pages[page_idx]

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
                    # compute over ~1s for a smooth number
                    time.sleep(1.0)
                    eth = tput.kbit_s("eth0")
                    wlan = tput.kbit_s("wlan0")
                    draw_throughput(device, eth, wlan)

                elif page == "sys":
                    temp = get_cpu_temp_c()
                    load = get_loadavg()
                    mem = get_mem_used_mb()
                    lines = [
                        "SYSTEM",
                        f"temp  {temp:.1f}C" if temp is not None else "temp  ?",
                        f"load  {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}",
                        f"mem   {mem} MB used" if mem is not None else "mem   ?",
                        "",
                    ]
                    draw_lines(device, lines)

            except Exception:
                # Fail safe: briefly show an error page then continue.
                draw_lines(device, ["OLED error", "retrying..."])
                time.sleep(0.5)

            if page != "tput":
                time.sleep(1.0)
    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()
