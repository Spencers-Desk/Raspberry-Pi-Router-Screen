# Raspberry Pi Router Screen

This is a simple script that writes system info to a small 128x64 SH1106 OLED dashboard for a Raspberry Pi router. It has a system info, screen saver, and poweroff mode.

## What it shows
1) Hostname, time, uptime  
2) WAN IPv4, Internet reachability, Wi‑Fi SSID & RSSI (wlan0)  
3) LAN IPv4 (eth0), DHCP lease count (dnsmasq)  
4) Tailscale IPv4 (if installed)  
5) Throughput (kbit/s) for eth0 and wlan0  
6) CPU temperature, load, memory used

## Hardware
- Raspberry Pi
- 128x64 SH1106 OLED (I2C, default address 0x3C)
- Optional: momentary push button to toggle between info, screensaver, and blank screen

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

Create the systemd service file:
```bash
sudo nano /etc/systemd/system/pi-router-oled.service
```

Paste the following content into the editor:
Make sure you replace all of the "pi"s in the file with the username your pi uses (if you aren't using the default pi)
```ini
[Unit]
Description=Pi Router OLED Status Screen
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/Raspberry-Pi-Router-Screen/status_screen.py
WorkingDirectory=/home/pi/Raspberry-Pi-Router-Screen
User=pi
Restart=on-failure
ExecStop=/usr/bin/python3 /home/pi/Raspberry-Pi-Router-Screen/poweroff_display.py

[Install]
WantedBy=multi-user.target
```

Save and exit (Ctrl+O, Enter, Ctrl+X), then enable and start it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pi-router-oled.service
systemctl status pi-router-oled.service --no-pager
```

If the service is failing, check the logs using:
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
- Default mode: rotates through pages every 5 seconds.
- Button cycles modes: Pages -> Screensaver (bouncing Raspberry) -> Off (display blank) -> Pages ...
- In Off mode the display is blanked but the button still works to wake it (next press returns to Pages).

## Troubleshooting
- Verify the display appears on I2C: `i2cdetect -y 1`
- Logs: `journalctl -u your-service-name`