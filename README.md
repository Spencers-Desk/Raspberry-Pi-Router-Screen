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

Enable I2C:
```bash
sudo raspi-config nonint do_i2c 0
sudo apt update
sudo apt install -y i2c-tools
i2cdetect -y 1   # should show 0x3c
```

## Software requirements
```bash
sudo apt install -y python3-pil python3-smbus python3-rpi.gpio
pip3 install --upgrade luma.oled
```

Optional tools used if present:
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

If your OLED uses a different I2C address or needs rotation, edit `make_device()` in `status_screen.py`. To use another GPIO pin, change `SCREENSAVER_BUTTON_PIN`.

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
- Check your button wiring (BCM 17 with pull-up to 3.3V, button to GND)
- Logs: `journalctl -u your-service-name`

## License
GPL-3.0 (see LICENSE).
