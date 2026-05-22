#!/usr/bin/env python3
"""
supervisor.py — Superviseur Web Radio (nginx-rtmp + FFmpeg)
======================================================================
Nouveauté : source de la playlist depuis la base MariaDB `webradio`.

Logique de sélection des morceaux (mode FALLBACK) :
  1. Au démarrage (et à minuit), charge la programmation du jour depuis
     la table `programmation` (jointure titre + artiste), triée par `ordre`.
  2. Si la programmation de la journée est vide → mode aléatoire depuis
     le dossier music (comportement historique inchangé).
  3. Une fois la programmation épuisée → rebascule en mode aléatoire
     pour le reste de la journée.

Tables utilisées (lecture seule) :
  programmation  (id_prog, date_prog, ordre, id_titre)
  titre          (id_titre, nom_titre, chemin, id_artiste, id_genre, duree)
  artiste        (id_artiste, nom_artiste)

Dépendances :
  pip install aiohttp PyMySQL

Usage :
  python3 supervisor.py
"""

import asyncio
import logging
import os
import random
import signal
import threading
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from typing import Optional, List, Tuple

# =============================================================================
# SEGMENT WATCHER — surveille les nouveaux .ts et appelle ts_inject
# =============================================================================
_TS_PACKET_SIZE = 188


class SegmentWatcher:
    """
    Surveille le dossier HLS et appelle le binaire `ts_inject` pour injecter
    les métadonnées ID3 dans chaque nouveau segment .ts dès sa création.
    """

    def __init__(self, hls_dir: str, ts_inject: str = "/usr/local/bin/ts_inject",
                 poll_interval: float = 0.5, pmt_pid: int = 0x1000):
        self._dir       = Path(hls_dir)
        self._ts_inject = ts_inject
        self._poll      = poll_interval
        self._pmt_pid   = pmt_pid
        self._stop      = threading.Event()
        self._thread    = None
        self._lock      = threading.Lock()
        self._title     = ''
        self._artist    = ''
        self._seen      = set()

    def set_track(self, title: str, artist: str = ''):
        with self._lock:
            self._title  = title
            self._artist = artist
        log.info("[ID3] Nouveau morceau → '%s' / '%s'", title, artist)

    def _get_track(self):
        with self._lock:
            return self._title, self._artist

    def start(self):
        if not os.access(self._ts_inject, os.X_OK):
            log.error("[ID3] ts_inject introuvable ou non exécutable : %s", self._ts_inject)
            return
        self._stop.clear()
        self._seen.clear()
        if self._dir.exists():
            self._seen = {f.name for f in self._dir.glob('*.ts')}
        self._thread = threading.Thread(target=self._run, daemon=True, name='SegmentWatcher')
        self._thread.start()
        log.info("[ID3] SegmentWatcher démarré sur %s", self._dir)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("[ID3] SegmentWatcher arrêté")

    def _run(self):
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception as e:
                log.debug("[ID3] Watcher erreur : %s", e)
            self._stop.wait(self._poll)

    def _scan(self):
        title, artist = self._get_track()
        if not title:
            return
        for ts_file in sorted(self._dir.glob('*.ts')):
            if ts_file.name in self._seen:
                continue
            try:
                s1 = ts_file.stat().st_size
                self._stop.wait(0.15)
                s2 = ts_file.stat().st_size
                if s1 != s2 or s1 < _TS_PACKET_SIZE:
                    continue
            except FileNotFoundError:
                continue
            self._inject(ts_file, title, artist)
            self._seen.add(ts_file.name)

    def _inject(self, ts_file: Path, title: str, artist: str):
        cmd = [
            self._ts_inject,
            "--input",   str(ts_file),
            "--title",   title,
            "--pmt-pid", str(self._pmt_pid),
        ]
        if artist:
            cmd += ["--artist", artist]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                log.debug("[ID3] %s", result.stderr.strip())
            else:
                log.warning("[ID3] ts_inject échoué sur %s : %s",
                            ts_file.name, result.stderr.strip())
        except subprocess.TimeoutExpired:
            log.warning("[ID3] ts_inject timeout sur %s", ts_file.name)
        except FileNotFoundError:
            log.error("[ID3] ts_inject introuvable : %s", self._ts_inject)

    def reset_seen(self):
        with self._lock:
            self._seen.clear()


# =============================================================================
# IMPORTS RÉSEAU
# =============================================================================
try:
    from aiohttp import web, ClientSession, ClientTimeout
except ImportError:
    print("pip install aiohttp")
    sys.exit(1)

try:
    import pymysql
    import pymysql.cursors
    HAS_DB = True
except ImportError:
    HAS_DB = False
    print("[DB] PyMySQL absent — pip install PyMySQL")
    print("[DB] Fonctionnement en mode aléatoire uniquement")


# =============================================================================
# CONFIGURATION
# =============================================================================
CFG = {
    # Chemins
    "hls_dir":    "/opt/webradio/hls",
    "music_dir":  "/opt/webradio/music",
    "log_file":   "/opt/webradio/logs/supervisor.log",
    "flag_file":  "/tmp/webradio_live.flag",

    # FFmpeg
    "ffmpeg":       "/usr/bin/ffmpeg",
    "rtmp_source":  "rtmp://127.0.0.1/live/stream",
    "hls_output":   "/opt/webradio/hls/stream.m3u8",
    "hls_seg_pat":  "/opt/webradio/hls/seg%05d.ts",
    "hls_time":     3,
    "hls_list":     10,
    "aac_bitrate":  "192k",

    # ts_inject
    "ts_inject": "/usr/local/bin/ts_inject",
    "pmt_pid":   0x1000,

    # Base de données MariaDB
    "db_host":   "localhost",
    "db_port":   3306,
    "db_name":   "webradio",
    "db_user":   "webradio_user",
    "db_pass":   "ChangeMe!",
    "db_charset":"utf8mb4",

    # Comportement
    "webhook_port": 8089,
    "grace_s":      8,
    "flag_poll_s":  2,
    "stat_poll_s":  10,
    "nginx_stat":   "http://127.0.0.1/stat",

    # Formats audio acceptés
    "audio_exts": {".m4a"},
}
# =============================================================================

# Création des répertoires AVANT le logging (Python 3.7 : FileNotFoundError)
for _d in [CFG["hls_dir"], CFG["music_dir"], Path(CFG["log_file"]).parent]:
    os.makedirs(_d, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(CFG["log_file"], encoding="utf-8"),
    ],
)
log = logging.getLogger("supervisor")


# =============================================================================
# COUCHE BASE DE DONNÉES
# =============================================================================

def _db_connect():
    """Ouvre une connexion PyMySQL. Lève une exception si indisponible."""
    return pymysql.connect(
        host    = CFG["db_host"],
        port    = CFG["db_port"],
        db      = CFG["db_name"],
        user    = CFG["db_user"],
        password= CFG["db_pass"],
        charset = CFG["db_charset"],
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
    )


def db_load_schedule(for_date: date = None) -> List[dict]:
    """
    Charge la programmation d'une journée depuis MariaDB.

    Retourne une liste ordonnée de dicts :
        [{"nom_titre": "...", "nom_artiste": "...", "chemin": "...",
          "duree": 240, "ordre": 1}, ...]

    Retourne [] si la journée est vide, si PyMySQL est absent,
    ou si la connexion échoue.
    """
    if not HAS_DB:
        return []

    if for_date is None:
        for_date = date.today()

    query = """
        SELECT
            p.ordre,
            t.nom_titre,
            t.chemin,
            t.duree,
            a.nom_artiste
        FROM programmation p
        JOIN titre   t ON t.id_titre   = p.id_titre
        JOIN artiste a ON a.id_artiste = t.id_artiste
        WHERE p.date_prog = %s
        ORDER BY p.ordre ASC
    """
    try:
        conn = _db_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(query, (for_date.isoformat(),))
                rows = cur.fetchall()
        finally:
            conn.close()

        log.info("[DB] Programmation du %s : %d titre(s)", for_date, len(rows))
        return list(rows)

    except Exception as e:
        log.warning("[DB] Impossible de charger la programmation : %s", e)
        return []


# =============================================================================
# PLAYLIST — source DB ou dossier selon disponibilité
# =============================================================================

class TrackInfo:
    """Représente un morceau quelle que soit sa source (DB ou fichier)."""
    __slots__ = ("path", "title", "artist")

    def __init__(self, path: Path, title: str, artist: str):
        self.path   = path
        self.title  = title
        self.artist = artist

    def __repr__(self):
        return f"<TrackInfo '{self.title}' — {self.artist}>"


class Playlist:
    """
    Gère la file de lecture.

    Comportement :
      - Si une programmation DB existe pour aujourd'hui → on la suit dans
        l'ordre, puis on bascule en mode aléatoire quand elle est épuisée.
      - Sinon → lecture aléatoire depuis le dossier music (comportement
        historique, sans répétition immédiate).

    La programmation est rechargée automatiquement à minuit.
    """

    def __init__(self, music_dir: str, exts):
        self.music_dir   = Path(music_dir)
        self.exts        = exts
        self._queue      = []        # List[TrackInfo]
        self._last_path  = None      # Path du dernier morceau joué
        self._db_done    = False     # programmation DB épuisée ?
        self._loaded_day = None      # date pour laquelle la DB a été chargée
        self._load_db_schedule()

    # ------------------------------------------------------------------ DB ---

    def _load_db_schedule(self):
        """Charge (ou recharge) la programmation du jour depuis la DB."""
        today = date.today()
        rows  = db_load_schedule(today)
        self._loaded_day = today
        self._db_done    = False

        if rows:
            self._queue = []
            for row in rows:
                path = Path(row["chemin"])
                if not path.is_file():
                    log.warning("[PLAYLIST] Fichier introuvable, ignoré : %s", row["chemin"])
                    continue
                self._queue.append(TrackInfo(
                    path   = path,
                    title  = row["nom_titre"],
                    artist = row["nom_artiste"],
                ))
            if self._queue:
                log.info("[PLAYLIST] %d titres chargés depuis la DB pour le %s",
                         len(self._queue), today)
                return
            # Tous les chemins étaient invalides → fallback aléatoire
            log.warning("[PLAYLIST] Aucun fichier valide dans la programmation DB, mode aléatoire")

        # Pas de programmation ou tous les fichiers manquants
        self._db_done = True
        self._fill_random()

    def _fill_random(self):
        """Remplit la queue avec les fichiers du dossier en ordre aléatoire."""
        files = [
            f for f in self.music_dir.rglob("*")
            if f.suffix.lower() in self.exts and f.is_file()
        ]
        if not files:
            log.warning("[PLAYLIST] Aucun fichier audio dans %s", self.music_dir)
            return
        random.shuffle(files)
        # Évite de rejouer le dernier morceau en tête de nouvelle liste
        if self._last_path and len(files) > 1 and files[0] == self._last_path:
            files[0], files[1] = files[1], files[0]
        # Convertit en TrackInfo en lisant les tags du fichier
        queue = []
        for f in files:
            title, artist = _extract_tags(f)
            queue.append(TrackInfo(path=f, title=title, artist=artist))
        self._queue = queue
        log.info("[PLAYLIST] Mode aléatoire : %d fichiers", len(self._queue))

    # ---------------------------------------------------------------- next ---

    def next(self) -> Optional[TrackInfo]:
        """Retourne le prochain TrackInfo à jouer."""

        # Recharger la programmation à minuit
        today = date.today()
        if self._loaded_day != today:
            log.info("[PLAYLIST] Nouveau jour (%s), rechargement DB", today)
            self._load_db_schedule()

        # Queue vide → recharger selon le contexte
        if not self._queue:
            if not self._db_done:
                # Programmation DB épuisée → bascule aléatoire
                log.info("[PLAYLIST] Programmation DB épuisée, bascule aléatoire")
                self._db_done = True
            self._fill_random()
            if not self._queue:
                return None

        track = self._queue.pop(0)
        self._last_path = track.path
        log.info("[PLAYLIST] → '%s' — %s  (%s)",
                 track.title, track.artist, track.path.name)
        return track

    def reload_today(self):
        """Force le rechargement de la programmation du jour (appelable depuis l'API)."""
        log.info("[PLAYLIST] Rechargement forcé de la programmation")
        self._load_db_schedule()


# =============================================================================
# LECTURE DES TAGS ID3 NATIFS (mutagen optionnel)
# =============================================================================

def _extract_tags(filepath: Path) -> Tuple[str, str]:
    """
    Retourne (title, artist).
    Utilise mutagen si disponible, sinon extrait l'artiste et le titre depuis le nom avec __ en séparateur
    Sinon le nom de fichier sans extension.
    Appelé uniquement en mode aléatoire (en mode DB, les tags viennent de la DB).
    """
    filename = str(filepath.name)
    if "__" in filename:
        name_without_ext = filename.rsplit(".", 1)[0]
        artist, title = name_without_ext.split("__", 1)
    else:
        title  = filepath.stem
        artist = ''
        
    try:
        from mutagen import File as MutagenFile
        tags = MutagenFile(filepath, easy=True)
        if tags:
            title  = str(tags.get('title',  [title])[0])
            artist = str(tags.get('artist', [''])[0])
    except ImportError:
        pass
    except Exception as e:
        log.debug("[TAGS] Lecture échouée pour %s : %s", filepath.name, e)
    return title, artist


# =============================================================================
# ÉTAT GLOBAL
# =============================================================================

class State:
    def __init__(self):
        self.live            = False
        self.mode            = "starting"   # "live" | "fallback" | "starting"
        self.listeners       = 0
        self.ffmpeg          = None
        self.last_publish    = 0.0
        self.last_unpublish  = 0.0
        self.segments        = 0
        self.current_title   = None
        self.current_artist  = None
        self.playlist_source = "db"         # "db" | "random"
        self.playlist        = Playlist(CFG["music_dir"], CFG["audio_exts"])
        self._lock           = asyncio.Lock()
        self._grace_task     = None

    def to_dict(self):
        return {
            "mode":             self.mode,
            "live":             self.live,
            "listeners":        self.listeners,
            "segments":         self.segments,
            "current_title":    self.current_title,
            "current_artist":   self.current_artist,
            "playlist_source":  self.playlist_source,
            "last_publish":     self.last_publish,
            "last_unpublish":   self.last_unpublish,
            "ffmpeg_pid": (
                self.ffmpeg.pid
                if self.ffmpeg and self.ffmpeg.poll() is None
                else None
            ),
        }


S = State()

_seg_watcher = SegmentWatcher(
    hls_dir   = CFG["hls_dir"],
    ts_inject = CFG["ts_inject"],
    pmt_pid   = CFG["pmt_pid"],
)


# =============================================================================
# AUDIO PIPE — flux PCM continu vers FFmpeg HLS
# =============================================================================

class AudioPipe:
    """
    Décode chaque morceau en PCM via FFmpeg (-re = vitesse réelle) et envoie
    le flux continu sur stdin du process FFmpeg HLS.
    Le muxeur HLS ne voit jamais de coupure → pas de saut de segments.
    """

    def __init__(self):
        self._stop      = threading.Event()
        self._proc_hls  = None
        self._thread    = None

    def start(self, playlist: Playlist, hls_args: list):
        self._stop.clear()
        cmd_hls = [
            CFG["ffmpeg"], "-loglevel", "warning",
            "-fflags", "+genpts",
            "-f", "s16le", "-ar", "44100", "-ac", "2",
            "-i", "pipe:0",
            "-vn",
        ] + hls_args
        log.info("[PIPE] Lancement FFmpeg HLS : %s", " ".join(cmd_hls))
        self._proc_hls = subprocess.Popen(
            cmd_hls, stdin=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self._thread = threading.Thread(
            target=self._feed, args=(playlist,), daemon=True, name="audio-pipe"
        )
        self._thread.start()
        return self._proc_hls

    def _decode_to_pcm(self, filepath: Path):
        cmd = [
            CFG["ffmpeg"], "-loglevel", "error",
            "-re",                   # vitesse réelle (1x)
            "-i", str(filepath),
            "-vn",
            "-f", "s16le", "-ar", "44100", "-ac", "2",
            "pipe:1",
        ]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def _feed(self, playlist: Playlist):
        CHUNK = 4096 * 4   # ~23ms à 44100Hz stéréo s16le
        while not self._stop.is_set():
            track = playlist.next()
            if track is None:
                log.warning("[PIPE] Aucun fichier disponible, attente 5s")
                self._stop.wait(5)
                continue

            # Mise à jour de l'état global et du SegmentWatcher (ID3)
            S.current_title   = track.title
            S.current_artist  = track.artist
            S.playlist_source = "random" if playlist._db_done else "db"
            _seg_watcher.set_track(track.title, track.artist)

            proc = self._decode_to_pcm(track.path)
            try:
                while not self._stop.is_set():
                    chunk = proc.stdout.read(CHUNK)
                    if not chunk:
                        break
                    proc_hls = self._proc_hls
                    if proc_hls is None or proc_hls.stdin is None:
                        self._stop.set()
                        break
                    try:
                        proc_hls.stdin.write(chunk)
                    except (BrokenPipeError, AttributeError):
                        log.warning("[PIPE] HLS stdin fermé")
                        self._stop.set()
                        break
            finally:
                proc.stdout.close()
                proc.wait()

        if self._proc_hls and self._proc_hls.stdin:
            try:
                self._proc_hls.stdin.close()
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        if self._proc_hls:
            try:
                self._proc_hls.stdin.close()
            except Exception:
                pass
            try:
                self._proc_hls.terminate()
                self._proc_hls.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._proc_hls.kill()
                self._proc_hls.wait()
            except Exception:
                pass
        self._proc_hls = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None


_audio_pipe = AudioPipe()


# =============================================================================
# ARGUMENTS HLS COMMUNS
# =============================================================================

def _hls_args() -> list:
    return [
        "-c:a", "aac",
        "-profile:a", "aac_low",    # AAC-LC strict, pas de HE-AAC
        "-b:a", CFG["aac_bitrate"],
        "-ar", "44100",
        "-ac", "2",
        "-f", "hls",
        "-hls_time",             str(CFG["hls_time"]),
        "-hls_list_size",        str(CFG["hls_list"]),
        "-hls_flags",            "delete_segments+discont_start+omit_endlist",
        "-hls_segment_type",     "mpegts",
        "-hls_segment_filename", CFG["hls_seg_pat"],
        CFG["hls_output"],
    ]


# =============================================================================
# PILOTAGE FFMPEG
# =============================================================================

def stop_ffmpeg():
    _seg_watcher.stop()
    _audio_pipe.stop()
    proc = S.ffmpeg
    if proc and proc.poll() is None:
        log.info("[FFMPEG] Arrêt PID %d", proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    S.ffmpeg = None


def start_fallback():
    stop_ffmpeg()
    os.makedirs(CFG["hls_dir"], exist_ok=True)
    log.info("[FALLBACK] Démarrage pipe audio continu")
    _seg_watcher.reset_seen()
    _seg_watcher.start()
    S.ffmpeg = _audio_pipe.start(S.playlist, _hls_args())
    S.mode = "fallback"


def start_live():
    stop_ffmpeg()
    os.makedirs(CFG["hls_dir"], exist_ok=True)
    _seg_watcher.reset_seen()
    _seg_watcher.set_track("Live")
    _seg_watcher.start()
    cmd = [
        CFG["ffmpeg"], "-loglevel", "warning",
        "-fflags", "+genpts",
        "-i", CFG["rtmp_source"],
        "-vn",
    ] + _hls_args()
    log.info("[LIVE] FFmpeg : %s", " ".join(cmd))
    S.ffmpeg = subprocess.Popen(cmd, stderr=subprocess.PIPE)
    S.mode = "live"
    S.current_title  = None
    S.current_artist = None


# =============================================================================
# BASCULEMENT
# =============================================================================

async def go_live():
    async with S._lock:
        if S.mode == "live":
            return
        if S._grace_task and not S._grace_task.done():
            S._grace_task.cancel()
        log.info("[SWITCH] → LIVE (2s wait)")
        await asyncio.sleep(2)   # laisse le temps à nginx-rtmp d'indexer le flux
        start_live()


async def go_fallback():
    async with S._lock:
        if S.mode == "fallback":
            return
        log.info("[SWITCH] → FALLBACK")
        start_fallback()


async def delayed_fallback():
    try:
        log.info("[SWITCH] Grâce %ds avant fallback...", CFG["grace_s"])
        await asyncio.sleep(CFG["grace_s"])
        if not S.live:
            await go_fallback()
    except asyncio.CancelledError:
        log.info("[SWITCH] Grâce annulée (OBS reconnecté)")


def trigger_live():
    S.live = True
    S.last_publish = time.time()
    asyncio.create_task(go_live())


def trigger_fallback():
    S.live = False
    S.last_unpublish = time.time()
    if S._grace_task and not S._grace_task.done():
        S._grace_task.cancel()
    S._grace_task = asyncio.create_task(delayed_fallback())


# =============================================================================
# WEBHOOKS HTTP
# =============================================================================

async def _json(req):
    try:
        return await req.json()
    except Exception:
        return {}


async def hook_publish(req):
    body = await _json(req)
    log.info("[WEBHOOK] on_publish %s", body)
    trigger_live()
    return web.Response(text="ok")


async def hook_unpublish(req):
    body = await _json(req)
    log.info("[WEBHOOK] on_unpublish %s", body)
    trigger_fallback()
    return web.Response(text="ok")


async def hook_status(req):
    return web.json_response(S.to_dict())


async def hook_set_track(req):
    """
    PUT /set_track  {"title": "...", "artist": "..."}
    Met à jour les métadonnées ID3 à la volée (mode LIVE).
    """
    body   = await _json(req)
    title  = body.get("title", "").strip()
    artist = body.get("artist", "").strip()
    if not title:
        return web.Response(status=400, text="title requis")
    _seg_watcher.set_track(title, artist)
    S.current_title  = title
    S.current_artist = artist
    log.info("[TRACK] Mise à jour manuelle → '%s' / '%s'", title, artist)
    return web.json_response({"ok": True, "title": title, "artist": artist})


async def hook_reload_schedule(req):
    """
    POST /reload_schedule
    Force le rechargement immédiat de la programmation du jour depuis la DB.
    Utile après avoir inséré de nouveaux titres sans redémarrer le superviseur.
    """
    S.playlist.reload_today()
    source = "db" if not S.playlist._db_done else "random"
    queued = len(S.playlist._queue)
    log.info("[API] Programmation rechargée : %d titres, source=%s", queued, source)
    return web.json_response({
        "ok":      True,
        "date":    date.today().isoformat(),
        "source":  source,
        "queued":  queued,
    })


async def hook_schedule_preview(req):
    """
    GET /schedule
    Retourne la programmation restante pour aujourd'hui (lecture seule, sans consommer la queue).
    """
    items = [
        {
            "position": i + 1,
            "title":    t.title,
            "artist":   t.artist,
            "file":     t.path.name,
        }
        for i, t in enumerate(S.playlist._queue)
    ]
    return web.json_response({
        "date":   date.today().isoformat(),
        "source": "random" if S.playlist._db_done else "db",
        "count":  len(items),
        "items":  items,
    })


# =============================================================================
# TÂCHES DE FOND
# =============================================================================

async def flag_watcher():
    """Surveille le flag fichier créé par on_publish.sh (double sécurité)."""
    flag = Path(CFG["flag_file"])
    prev = flag.exists()
    while True:
        await asyncio.sleep(CFG["flag_poll_s"])
        cur = flag.exists()
        if cur and not prev:
            log.info("[FLAG] Flag apparu → live")
            if not S.live:
                trigger_live()
        elif not cur and prev:
            log.info("[FLAG] Flag disparu → fallback")
            if S.live:
                trigger_fallback()
        prev = cur


async def stat_poller():
    """Poll de l'API stat XML nginx-rtmp (filet de sécurité)."""
    async with ClientSession() as session:
        while True:
            await asyncio.sleep(CFG["stat_poll_s"])
            try:
                async with session.get(
                    CFG["nginx_stat"], timeout=ClientTimeout(total=3)
                ) as resp:
                    if resp.status != 200:
                        continue
                    text   = await resp.text()
                    root   = ET.fromstring(text)
                    active = any(
                        s.findtext("active") == "1"
                        for s in root.findall(".//stream")
                    )
                    if active and not S.live:
                        log.info("[STAT] Stream détecté via /stat → live")
                        trigger_live()
                    elif not active and S.live:
                        log.info("[STAT] Aucun stream via /stat → fallback")
                        trigger_fallback()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("[STAT] Erreur poll nginx stat : %s", e)


async def midnight_reloader():
    """Recharge automatiquement la programmation à minuit."""
    while True:
        now     = datetime.now()
        # Calcule les secondes jusqu'à minuit + 5s de marge
        seconds = (24 * 3600) - (now.hour * 3600 + now.minute * 60 + now.second) + 5
        log.info("[DB] Prochain rechargement DB dans %dh%02dm",
                 seconds // 3600, (seconds % 3600) // 60)
        await asyncio.sleep(seconds)
        log.info("[DB] Rechargement automatique de la programmation (minuit)")
        S.playlist.reload_today()


async def ffmpeg_watchdog():
    """Redémarre FFmpeg automatiquement en cas de crash."""
    while True:
        await asyncio.sleep(5)
        proc = S.ffmpeg
        if proc is None:
            continue
        ret = proc.poll()
        if ret is not None:
            try:
                err = (proc.stderr.read().decode(errors="replace")[-400:]
                       if proc.stderr else "")
            except Exception:
                err = ""
            log.warning("[WATCHDOG] FFmpeg quitté (code %d) : %s", ret, err)
            if S.live:
                log.info("[WATCHDOG] Redémarrage LIVE (2s)")
                await asyncio.sleep(2)   # ← délai avant retry
                if S.live:               # vérifier que OBS est toujours là
                    start_live()

            else:
                log.info("[WATCHDOG] Redémarrage FALLBACK")
                start_fallback()


# =============================================================================
# POINT D'ENTRÉE
# =============================================================================

async def main():
    log.info("=== Superviseur Web Radio démarré (Python %s) ===",
             sys.version.split()[0])

    log.info("[INIT] Démarrage en FALLBACK (attente d'OBS)")
    start_fallback()

    app = web.Application()
    app.router.add_post("/on_publish",       hook_publish)
    app.router.add_post("/on_unpublish",     hook_unpublish)
    app.router.add_get( "/status",           hook_status)
    app.router.add_put( "/set_track",        hook_set_track)
    app.router.add_post("/reload_schedule",  hook_reload_schedule)
    app.router.add_get( "/schedule",         hook_schedule_preview)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", CFG["webhook_port"]).start()
    log.info("[HTTP] Webhooks sur http://127.0.0.1:%d", CFG["webhook_port"])

    asyncio.create_task(ffmpeg_watchdog())
    asyncio.create_task(flag_watcher())
    asyncio.create_task(stat_poller())
    asyncio.create_task(midnight_reloader())

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(runner)))

    log.info("[READY] OBS → rtmp://VOTRE_IP/live  clé: stream")
    log.info("[READY] Monitoring → curl http://127.0.0.1:%d/status", CFG["webhook_port"])
    await asyncio.Event().wait()


async def _shutdown(runner):
    log.info("[SHUTDOWN] Arrêt...")
    stop_ffmpeg()
    await runner.cleanup()
    asyncio.get_event_loop().stop()


if __name__ == "__main__":
    asyncio.run(main())

# =============================================================================
# NOUVELLES ROUTES API
# =============================================================================
#
# GET  /status            État complet (mode, titre, artiste, source playlist)
# PUT  /set_track         Forcer titre/artiste en mode LIVE
# POST /reload_schedule   Recharger la programmation DB sans redémarrer
# GET  /schedule          Voir la file restante pour aujourd'hui
#
# Exemples :
#   curl http://127.0.0.1:8089/status
#   curl http://127.0.0.1:8089/schedule
#   curl -X POST http://127.0.0.1:8089/reload_schedule
#   curl -X PUT http://127.0.0.1:8089/set_track \
#        -H "Content-Type: application/json" \
#        -d '{"title":"Bohemian Rhapsody","artist":"Queen"}'
#
# =============================================================================
# INSTALLATION
# =============================================================================
#
# pip install aiohttp PyMySQL
#
# Créer l'utilisateur MariaDB :
#   CREATE USER 'webradio_user'@'localhost' IDENTIFIED BY 'ChangeMe!';
#   GRANT SELECT ON webradio.* TO 'webradio_user'@'localhost';
#   FLUSH PRIVILEGES;
#
# Insérer une programmation de test :
#   INSERT INTO programmation (date_prog, ordre, id_titre)
#   SELECT CURDATE(), ROW_NUMBER() OVER (ORDER BY RAND()), id_titre
#   FROM titre LIMIT 10;
#
# Lancer :
#   nginx -c /opt/webradio/nginx-rtmp.conf
#   python3 supervisor.py
#
# =============================================================================
