# Raspberry Pi Router Screen

A small 128x64 SH1106 OLED dashboard for a Raspberry Pi used as a router/firewall. It cycles through network and system stats to make a headless setup easier to monitor.

Note: The nftables NAT screen and related functions were removed.

## What it shows
1) Hostname, time, uptime  
2) WAN IPv4, Internet reachability, Wi‑Fi SSID & RSSI (wlan0)  
3) LAN IPv4 (eth0), DHCP lease count (dnsmasq)  
4) Tailscale IPv4 (if installed)  
5) Throughput (kbit/s) for eth0 and wlan0  
6) CPU temperature, load, memory used

## Hardware
- Raspberry Pi with I2C enabled
- 128x64 SH1106 OLED (I2C, default address 0x3C)
- Optional: momentary push button to toggle a screensaver
  - Wire one side to BCM 17, the other to GND (internal pull-up is enabled)

## Software requirements
```bash
sudo apt install -y python3-pil python3-smbus python3-rpi.gpio python3-luma.oled
```

## Wire the Screen and Button
OLED SH1106 I2C:
- Pin 1 - VCC->3V3
- Pin 3 - SDA->GPIO2 (SDA)
- Pin 5 - SCL->GPIO3 (SCL)
- Pin 7 - GND->GND
Button (screensaver toggle):
- Pin 11 - GPIO17
- Pin 14 - GND

## Install
1) Update OS and install tools/deps:
```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y git i2c-tools python3-pil python3-smbus python3-rpi.gpio python3-luma.oled

```
2) Enable I2C:
```bash
sudo raspi-config
```
Choose "Interface Options"
Choose "I2C"
Say <Yes>

3) Verify the OLED is visible on I2C bus 1:
```bash
i2cdetect -y 1   # expect 0x3c
```

4) Get the code:
```bash
git clone https://github.com/your/repo.git
cd Raspberry-Pi-Router-Screen
```

5) Test run:
```bash
python3 status_screen.py
```
- If your display needs rotation or a different I2C address, edit `make_device()` in `status_screen.py`.
- Press the button on BCM 17 to toggle the bouncing Raspberry screensaver on/off.

6) Optional: run on boot via systemd (adjust paths as needed):
Add your user to needed groups (for non-root access) and reboot:
```bash
sudo usermod -aG i2c,gpio $USER
sudo reboot
```

```bash
sudo tee /etc/systemd/system/pi-router-oled.service >/dev/null <<'UNIT'
[Unit]
Description=Pi Router OLED Status Screen
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/Raspberry-Pi-Router-Screen/status_screen.py
WorkingDirectory=/home/pi/Raspberry-Pi-Router-Screen
User=pi
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now pi-router-oled.service
systemctl status pi-router-oled.service --no-pager
```

Verify logs if needed:
```bash
journalctl -u pi-router-oled.service -e
```

## Optional tools used if present
- dnsmasq (for DHCP lease count)
- tailscale (for Tailscale IP)

## Run
```bash
cd Raspberry-Pi-Router-Screen
python3 status_screen.py
```

Behavior:
- By default, the app rotates through the pages every 5 seconds.
- Press the button (BCM 17 to GND) to toggle a bouncing Raspberry screensaver on/off.

## Services (optional)
Create a simple systemd service to start on boot:
```ini
[Unit]
Description=Pi Router OLED Status Screen
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /path/to/Raspberry-Pi-Router-Screen/status_screen.py
Restart=on-failure
User=pi
WorkingDirectory=/path/to/Raspberry-Pi-Router-Screen

[Install]
WantedBy=multi-user.target
```

## Troubleshooting
- Verify the display appears on I2C: `i2cdetect -y 1`
- Button not working / "Failed to add edge detection": run with sudo, or ensure RPi.GPIO is installed; the app will fall back to polling automatically
- Check your button wiring (BCM 17 with pull-up to 3.3V, button to GND)
- Logs: `journalctl -u your-service-name`

## License
GPL-3.0 (see LICENSE).
