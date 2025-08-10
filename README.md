# Raspberry Pi Router Screen

A small 128x64 SH1106 OLED status screen for a Raspberry Pi Router. It cycles through network and system stats allowing you to monitor the router.

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

Enable I2C:
```bash
sudo raspi-config nonint do_i2c 0
sudo apt update
sudo apt install -y i2c-tools
i2cdetect -y 1   # should show 0x3c
```

## Software requirements
```bash
sudo apt install -y python3-pil python3-smbus
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

If your OLED uses a different I2C address or needs rotation, edit `make_device()` in `status_screen.py`.

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
- Check logs: `journalctl -u nftables`, `journalctl -u dnsmasq`, or your service
- Show current nftables rules (if you use them): `sudo nft list ruleset`

## License
GPL-3.0 (see LICENSE).
