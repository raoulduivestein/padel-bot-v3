# VPS Deploy Ubuntu

Aanname: Ubuntu VPS, app in `/opt/padel-bot`, system user `padelbot`.

## Installatie

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git wget xvfb xauth

wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb

sudo useradd --system --create-home --shell /usr/sbin/nologin padelbot
sudo mkdir -p /opt/padel-bot
sudo chown -R padelbot:padelbot /opt/padel-bot
```

Upload/kopieer dit project naar `/opt/padel-bot`, bijvoorbeeld met `scp`, `rsync` of `git clone`.

```bash
cd /opt/padel-bot
sudo -u padelbot python3 -m venv .venv
sudo -u padelbot .venv/bin/pip install -r requirements.txt
sudo -u padelbot cp config/config.example.json config/config.json
sudo -u padelbot nano config/config.json
```

Zet in `config/config.json` minimaal:

- `username`
- `password`
- `device_id`
- `signature_mode`: `davidlloyd_v1`
- `padel.run_time.prep`
- `padel.run_time.booking`
- `padel.days_ahead`
- members/courts/rules

## Handmatig testen

```bash
cd /opt/padel-bot
sudo -u padelbot .venv/bin/python -m app.runner --attempts 1
```

Let op: zonder `--wait` probeert dit direct te boeken op basis van de gegenereerde slots.

## Web UI service

De webservice gebruikt Selenium/Chrome voor WhatsApp Web. Op een VPS zonder desktop draait Chrome via `xvfb-run`; dat staat al in `deploy/padel-bot.service`.

```bash
sudo cp deploy/padel-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now padel-bot.service
sudo systemctl status padel-bot.service
```

De web UI luistert standaard op `127.0.0.1:18018`. Zet er bij voorkeur Nginx met basic auth of een SSH tunnel voor.

Open daarna de frontend, ga naar het WhatsApp-tabblad en scan de QR-code. De Chrome-sessie wordt bewaard in `state/whatsapp-selenium-profile`.

## Dagelijkse automatische run

```bash
sudo cp deploy/padel-bot-run.service /etc/systemd/system/
sudo cp deploy/padel-bot-run.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now padel-bot-run.timer
systemctl list-timers padel-bot-run.timer
```

De timer start standaard om `07:55`. De runner wacht daarna zelf tot:

- `padel.run_time.prep` voor auth refresh/fresh login
- `padel.run_time.booking` voor de echte boekingspoging

## Logs

```bash
journalctl -u padel-bot.service -f
journalctl -u padel-bot-run.service -n 200
journalctl -u padel-bot-run.timer -n 50
```

## Timer aanpassen

Wijzig `/etc/systemd/system/padel-bot-run.timer`:

```ini
OnCalendar=*-*-* 07:55:00
```

Daarna:

```bash
sudo systemctl daemon-reload
sudo systemctl restart padel-bot-run.timer
```
