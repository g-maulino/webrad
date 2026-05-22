# webradio

Automated web radio system built on nginx-rtmp, FFmpeg and Python.

Continuously streams an HLS audio feed with automatic switching between three modes:
- **Live**: incoming RTMP stream from OBS Studio or Mixxx
- **Scheduled**: day-ordered playlist read from a MariaDB database
- **Random**: shuffle playback of local files when no schedule is defined

HLS segments are enriched with ID3 metadata (title, artist) via an external C binary `ts_inject`, compatible with HLS.js, Safari and VLC.

---

## System Requirements

| Component | Minimum version | Notes |
|---|---|---|
| Debian / Ubuntu | 10 (Buster) / 20.04 | Tested on these distributions |
| Python | 3.7 | Available by default on Debian 10 |
| nginx | 1.14 | With the `nginx-rtmp` module |
| FFmpeg | 4.1 | Available via `apt` |
| MariaDB | 10.3 | MySQL 5.7+ compatible |
| ts_inject | — | C binary to compile (see below) |

---

## Python Dependencies

```bash
pip install aiohttp PyMySQL
```

Optional dependency (ID3 tag reading in random mode):

```bash
pip install mutagen
```

If `PyMySQL` is missing, the supervisor starts in random mode only without a fatal error.

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_ORG/webrad.git /opt/webradio
cd /opt/webradio
```

### 2. Create the directory structure

```bash
mkdir -p /opt/webradio/{hls,music,logs,scripts}
```

### 3. Install nginx with the nginx-rtmp module

```bash
apt install libnginx-mod-rtmp
```

Copy and enable the nginx configuration:

```bash
cp config/nginx-rtmp.conf /etc/nginx/sites-available/webradio
ln -s /etc/nginx/sites-available/webradio /etc/nginx/sites-enabled/webradio
nginx -t && systemctl reload nginx
```

Copy the Web player:

```bash
cp web/player/index.html /var/www/html/webradio/
```

### 4. Install the shell scripts

```bash
cp scripts/on_publish.sh scripts/on_unpublish.sh /opt/webradio/scripts/
chmod +x /opt/webradio/scripts/*.sh
```

### 5. Compile ts_inject

```bash
gcc -O2 -o /usr/local/bin/ts_inject src/ts_inject.c
chmod +x /usr/local/bin/ts_inject
```

### 6. Create the MariaDB database

```bash
mysql -u root -p < db/01_create_database.sql
```

Create the application user:

```sql
CREATE USER 'webradio_user'@'localhost' IDENTIFIED BY 'ChangeMe!';
GRANT SELECT, INSERT, UPDATE, DELETE ON webradio.* TO 'webradio_user'@'localhost';
FLUSH PRIVILEGES;
```

Update the credentials in `supervisor.py` (the `CFG` section):

```python
"db_host": "localhost",
"db_user": "webradio_user",
"db_pass": "ChangeMe!",
```

### 7. Install Python dependencies

```bash
pip install aiohttp PyMySQL mutagen
```

### 8. Normalise audio files

All files must be in M4A format (AAC, 44100 Hz, stereo) to avoid timestamp issues at track boundaries. A normalisation script is provided:

```bash
bash scripts/normalize_music.sh /opt/webradio/music
```

Or manually:

```bash
for f in /opt/webradio/music/*.{flac,mp3,ogg,wav,aac}; do
    [ -f "$f" ] || continue
    ffmpeg -i "$f" -c:a aac -b:a 256k -ar 44100 -ac 2 "${f%.*}.m4a" && rm "$f"
done
```

> This step will be removed in a future release to avoid any quality loss. The plan is to add a bitstream check that validates loudness level and sample rate consistency instead.

### 9. Populate the database (optional — first run)

A separate script scans the `music/` folder and inserts tracks into the database:

```bash
python3 scripts/import_music.py /opt/webradio/music
```

To create a test schedule for today:

```sql
INSERT INTO programmation (date_prog, ordre, id_titre)
SELECT CURDATE(), (@n := @n + 1), id_titre
FROM titre, (SELECT @n := 0) init
ORDER BY RAND()
LIMIT 20;
```

> The import script uses a double-underscore `__` separator in the filename to split artist and title when file metadata is absent:
> `<Artist>__<Title>.extension`
> This allows the database to be populated even from files with no embedded tags.

The script **does not create duplicates**: a file that has already been imported (same path) is silently skipped on subsequent runs.

### Automation (cron)

```cron
# Scan every night at 03:00
0 3 * * * DB_PASS=ChangeMe! python3 /opt/webradio/db/02_import_music.py \
    --dir /var/lib/webradio/music >> /var/log/webradio-import.log 2>&1
```

---

## Web Interface (PHP)

### Database Configuration

Edit `web/config.php` or set the environment variables:

```bash
export DB_HOST=localhost
export DB_PORT=3306
export DB_NAME=webradio
export DB_USER=webradio_user
export DB_PASS=ChangeMe!
```

### Deployment

```bash
# Copy web files into your DocumentRoot
sudo cp web/*.php /var/www/html/webradio/adm/

# Permissions
sudo chown -R www-data:www-data /var/www/html/webradio/adm
```

### Available Pages

| URL | Description |
|-----|-------------|
| `/webradio/adm/editor.php` | Edit tracks, artists and genres |
| `/webradio/adm/editor.php?tab=artistes` | Direct link to the Artists tab |
| `/webradio/adm/editor.php?tab=genres` | Direct link to the Genres tab |
| `/webradio/adm/programmation.php` | Today's schedule |
| `/webradio/adm/programmation.php?date=2025-12-25` | Schedule for a specific date |

---

## Schedule Page Features

- **Date navigation**: Previous / Today / Next buttons plus a date picker
- **Automatic air-time calculation** starting from 00:00
- **Reordering** via ▲/▼ buttons
- **Entry deletion** with automatic order recalculation
- **Day copy**: duplicates an entire day's schedule to another date

---

## Security

> ⚠️ These pages do not include an authentication system.
> In production, protect them with one of the following:

- `.htpasswd` (Apache) or `auth_basic` (Nginx)
- A full PHP session-based login system
- A reverse proxy with authentication (Nginx + OAuth2 Proxy, etc.)

### 10. Start the supervisor

```bash
python3 /opt/webradio/supervisor.py
```

---

## systemd Service

To start the supervisor automatically at boot:

```ini
# /etc/systemd/system/webradio.service
[Unit]
Description=Web Radio Supervisor
After=network.target nginx.service mariadb.service
Wants=mariadb.service

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/webradio
ExecStart=/usr/bin/python3 /opt/webradio/supervisor.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now webradio
```

---

## OBS Studio Configuration

In **Settings → Stream**:

| Field | Value |
|---|---|
| Service | Custom |
| Server | `rtmp://YOUR_IP/live` |
| Stream Key | `stream` |

For Mixxx, configure the broadcast to the same RTMP URL.

---

## Output URLs

| Purpose | URL |
|---|---|
| HLS playlist (CDN pull) | `http://YOUR_IP:8080/live/stream.m3u8` |
| Supervisor monitoring | `http://127.0.0.1:8089/status` |
| Today's schedule | `http://127.0.0.1:8089/schedule` |
| nginx-rtmp stat (XML) | `http://127.0.0.1:8080/stat` |

---

## Post-start Checks

```bash
# Supervisor status
curl http://127.0.0.1:8089/status

# Schedule loaded for today
curl http://127.0.0.1:8089/schedule

# HLS segments being generated
ls -lh /opt/webradio/hls/

# Inspect a segment
ffprobe -v error -show_streams -select_streams a /opt/webradio/hls/seg00005.ts

# Live logs
tail -f /opt/webradio/logs/supervisor.log
```

---

## Hot-reload the Schedule

After inserting new tracks into MariaDB, reload without restarting:

```bash
curl -X POST http://127.0.0.1:8089/reload_schedule
```

---

## Update the Now-Playing Metadata in LIVE Mode

When OBS is active, push the current track title via the API:

```bash
curl -X PUT http://127.0.0.1:8089/set_track \
     -H "Content-Type: application/json" \
     -d '{"title": "Bohemian Rhapsody", "artist": "Queen"}'
```

---

## Documentation

- [Architecture and API reference](docs/ARCHITECTURE.en.md)

---

## License

MIT
