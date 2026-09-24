"""Local doorway visit monitor. Run with uvicorn app:app --host 127.0.0.1 --port 8000."""
import os
import json
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from ultralytics import YOLO

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv('MONITOR_DATA', ROOT / 'data'))
DATA.mkdir(parents=True, exist_ok=True)
PHOTOS = DATA / 'photos'
PHOTOS.mkdir(exist_ok=True)
DB = DATA / 'visits.sqlite3'
CONFIG = DATA / 'camera.json'
lock = threading.RLock()
app = FastAPI(title='CamSecMon')


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def connection():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connection() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS visits (
          id TEXT PRIMARY KEY, entered_at TEXT NOT NULL, exited_at TEXT,
          photo TEXT NOT NULL, features BLOB, status TEXT NOT NULL DEFAULT 'open',
          confidence REAL, note TEXT);
          CREATE TABLE IF NOT EXISTS unmatched_exits (
          id TEXT PRIMARY KEY, detected_at TEXT NOT NULL, photo TEXT NOT NULL,
          features BLOB, status TEXT NOT NULL DEFAULT 'pending', visit_id TEXT);
          CREATE INDEX IF NOT EXISTS visits_entered ON visits(entered_at);
        ''')

init_db()


def photograph(crop):
    name = uuid.uuid4().hex + '.jpg'
    if crop.size == 0 or not cv2.imwrite(str(PHOTOS / name), crop, [cv2.IMWRITE_JPEG_QUALITY, 83]):
        raise ValueError('Could not save crop')
    return name


def clothing_features(crop):
    h, w = crop.shape[:2]
    torso = crop[int(h*.22):int(h*.72), int(w*.18):int(w*.82)]
    if torso.size == 0:
        return None
    hsv = cv2.cvtColor(torso, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist.astype(np.float32).tobytes()


def score(a, b):
    if not a or not b:
        return 0.0
    return float(cv2.compareHist(np.frombuffer(a, np.float32), np.frombuffer(b, np.float32), cv2.HISTCMP_CORREL))


def register_entry(crop):
    with lock:
        name = photograph(crop)
        identity = uuid.uuid4().hex[:12]
        with connection() as db:
            db.execute('INSERT INTO visits (id,entered_at,photo,features) VALUES (?,?,?,?)',
                       (identity, now(), name, clothing_features(crop)))
        return identity


def register_exit(crop):
    """Auto-close only for a strong, uniquely best clothing match; queue everything else."""
    with lock:
        name = photograph(crop)
        feat = clothing_features(crop)
        with connection() as db:
            candidates = db.execute("SELECT id,features FROM visits WHERE status='open'").fetchall()
            ranked = sorted(((score(feat, r['features']), r['id']) for r in candidates), reverse=True)
            best = ranked[0][0] if ranked else 0
            second = ranked[1][0] if len(ranked) > 1 else -1
            if ranked and best >= .88 and best - second >= .12:
                db.execute("UPDATE visits SET exited_at=?, status='closed', confidence=? WHERE id=? AND status='open'",
                           (now(), best, ranked[0][1]))
                return {'matched': ranked[0][1], 'confidence': best}
            identity = uuid.uuid4().hex[:12]
            db.execute('INSERT INTO unmatched_exits(id,detected_at,photo,features) VALUES (?,?,?,?)',
                       (identity, now(), name, feat))
            return {'pending': identity}


class CameraSettings(BaseModel):
    host: str
    port: int = 554
    username: str
    password: str = ''
    stream_path: str = '/cam/realmonitor?channel=1&subtype=0'
    line_x: float = 0.5
    entry_direction: str = 'left_to_right'


def saved_settings():
    if CONFIG.is_file():
        return json.loads(CONFIG.read_text(encoding='utf-8'))
    return None


def validated_settings(settings, old=None):
    settings = settings.model_dump()
    host = settings['host'].strip()
    if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', host) or host.startswith('-') or '..' in host:
        raise HTTPException(400, 'Informe um IP ou nome de host válido.')
    if not 1 <= settings['port'] <= 65535 or not .1 <= settings['line_x'] <= .9:
        raise HTTPException(400, 'Porta ou posição da linha inválida.')
    if settings['entry_direction'] not in ('left_to_right', 'right_to_left'):
        raise HTTPException(400, 'Sentido de entrada inválido.')
    if not settings['username'] or not settings['username'].isascii() or not settings['username'].isprintable():
        raise HTTPException(400, 'Usuário da câmera inválido.')
    if not settings['password'] and old and old.get('host') == host and old.get('username') == settings['username']:
        settings['password'] = old['password']
    if not settings['password']:
        raise HTTPException(400, 'Informe a senha da câmera.')
    path = settings['stream_path'].strip()
    if not path.startswith('/') or '#' in path or '@' in path or ' ' in path or any(ord(c) < 32 for c in path):
        raise HTTPException(400, 'O caminho RTSP deve começar com / e não conter espaços, @ ou #.')
    settings['host'], settings['stream_path'] = host, path
    return settings


def stream_url(settings):
    return (f"rtsp://{quote(settings['username'], safe='')}:{quote(settings['password'], safe='')}"
            f"@{settings['host']}:{settings['port']}{settings['stream_path']}")


def open_capture(source):
    # Supported OpenCV builds apply these timeouts to FFmpeg and GStreamer capture backends.
    return cv2.VideoCapture(source, cv2.CAP_FFMPEG,
                            [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])


@app.get('/api/camera')
def camera_settings():
    current = saved_settings()
    if not current:
        return {'configured': False, 'host': '', 'port': 554, 'username': '',
                'stream_path': '/cam/realmonitor?channel=1&subtype=0', 'line_x': .5, 'entry_direction': 'left_to_right'}
    return {key:value for key,value in current.items() if key != 'password'} | {'configured': True}


@app.post('/api/camera/test')
def test_camera(settings: CameraSettings):
    current = validated_settings(settings, saved_settings())
    cap = open_capture(stream_url(current))
    try:
        if not cap.isOpened():
            raise HTTPException(400, 'Não foi possível conectar ao RTSP. Verifique rede, IP, senha e caminho.')
        ok, frame = cap.read()
        if not ok or frame is None:
            raise HTTPException(400, 'Conectou, mas não recebeu imagem do stream.')
        height, width = frame.shape[:2]
        return {'ok': True, 'resolution': f'{width} × {height}'}
    finally:
        cap.release()


@app.post('/api/camera')
def save_camera(settings: CameraSettings):
    current = validated_settings(settings, saved_settings())
    with lock:
        monitor.stop_flag.set()
        if monitor.thread and monitor.thread.is_alive():
            monitor.thread.join(timeout=10)
        if monitor.thread and monitor.thread.is_alive():
            raise HTTPException(409, 'A captura anterior ainda está encerrando; tente novamente em alguns segundos.')
        temporary = CONFIG.with_suffix('.tmp')
        temporary.write_text(json.dumps(current, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, CONFIG)
        try:
            os.chmod(CONFIG, 0o600)
        except OSError:
            pass
        monitor.start(stream_url(current), current['line_x'], current['entry_direction'])
    return {'ok': True}


@app.get('/api/camera/frame')
def camera_frame():
    with monitor.frame_lock:
        if monitor.frame is None:
            raise HTTPException(404, 'Aguardando primeira imagem da câmera.')
        frame = monitor.frame.copy()
    ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
    if not ok:
        raise HTTPException(500, 'Erro ao preparar a imagem.')
    from fastapi.responses import Response
    return Response(content=encoded.tobytes(), media_type='image/jpeg', headers={'Cache-Control': 'no-store'})


class Resolve(BaseModel):
    visit_id: str


@app.get('/')
def homepage():
    return HTMLResponse((ROOT / 'index.html').read_text(encoding='utf-8'))


@app.get('/api/status')
def status():
    return {'running': monitor.running, 'error': monitor.error, 'last_frame': monitor.last_frame,
            'camera_configured': bool(saved_settings() or os.getenv('CAMERA_RTSP') or os.getenv('VIDEO_FILE'))}


@app.get('/api/visits')
def visits(day: str | None = None):
    day = day or datetime.now().astimezone().date().isoformat()
    with connection() as db:
        rows = db.execute('SELECT id,entered_at,exited_at,photo,status,confidence,note FROM visits WHERE substr(entered_at,1,10)=? ORDER BY entered_at DESC', (day,)).fetchall()
        pending = db.execute("SELECT id,detected_at,photo FROM unmatched_exits WHERE status='pending' ORDER BY detected_at DESC").fetchall()
    result = []
    for r in rows:
        item = dict(r)
        item['duration_seconds'] = max(0, int((datetime.fromisoformat(r['exited_at']) - datetime.fromisoformat(r['entered_at'])).total_seconds())) if r['exited_at'] else None
        result.append(item)
    return {'day': day, 'visits': result, 'pending_exits': [dict(r) for r in pending]}


@app.post('/api/exits/{exit_id}/resolve')
def resolve(exit_id: str, body: Resolve):
    with lock, connection() as db:
        ex = db.execute("SELECT * FROM unmatched_exits WHERE id=? AND status='pending'", (exit_id,)).fetchone()
        visit = db.execute("SELECT * FROM visits WHERE id=? AND status='open'", (body.visit_id,)).fetchone()
        if not ex or not visit:
            raise HTTPException(404, 'Saída pendente ou visita aberta não encontrada')
        if datetime.fromisoformat(ex['detected_at']) < datetime.fromisoformat(visit['entered_at']):
            raise HTTPException(400, 'A saída ocorreu antes da entrada')
        db.execute("UPDATE visits SET exited_at=?, status='closed', note='Confirmado manualmente' WHERE id=?", (ex['detected_at'], body.visit_id))
        db.execute("UPDATE unmatched_exits SET status='resolved', visit_id=? WHERE id=?", (body.visit_id, exit_id))
    return {'ok': True}


@app.post('/api/exits/{exit_id}/dismiss')
def dismiss(exit_id: str):
    with lock, connection() as db:
        changed = db.execute("UPDATE unmatched_exits SET status='dismissed' WHERE id=? AND status='pending'", (exit_id,)).rowcount
        if not changed:
            raise HTTPException(404, 'Saída pendente não encontrada')
    return {'ok': True}


@app.get('/photos/{name}')
def photo(name: str):
    if not name.endswith('.jpg') or '/' in name or '\\' in name or not (PHOTOS / name).is_file():
        raise HTTPException(404)
    return FileResponse(PHOTOS / name, media_type='image/jpeg')


class Monitor:
    def __init__(self):
        self.running = False
        self.error = None
        self.last_frame = None
        self.frame = None
        self.frame_lock = threading.Lock()
        self.thread = None
        self.stop_flag = threading.Event()

    def start(self, source, line_x=.5, direction='left_to_right'):
        self.stop_flag = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.loop, args=(source, line_x, direction, self.stop_flag), daemon=True)
        self.thread.start()

    def loop(self, source, line_x, direction, stop_flag):
        self.running = True
        tracks = {}
        try:
            model = YOLO(os.getenv('YOLO_MODEL', 'yolo11n.pt'))
            while not stop_flag.is_set():
                cap = open_capture(source)
                if not cap.isOpened():
                    self.error = 'Não foi possível abrir o vídeo; tentando novamente.'
                    cap.release()
                    stop_flag.wait(5)
                    continue
                self.error = None
                tracks.clear()
                while not stop_flag.is_set():
                    ok, frame = cap.read()
                    if not ok:
                        break
                    self.last_frame = now()
                    height, width = frame.shape[:2]
                    line = int(width * line_x)
                    preview = frame.copy()
                    cv2.line(preview, (line, 0), (line, height), (0, 220, 170), 3)
                    with self.frame_lock:
                        self.frame = preview
                    results = model.track(frame, persist=True, classes=[0], conf=.35, tracker='bytetrack.yaml', verbose=False)
                    boxes = results[0].boxes
                    if boxes is None or boxes.id is None:
                        continue
                    for xyxy, ident in zip(boxes.xyxy.cpu().numpy(), boxes.id.int().cpu().numpy()):
                        x1, y1, x2, y2 = [int(v) for v in xyxy]
                        center = (x1 + x2) // 2
                        side = -1 if center < line else 1
                        ident = int(ident)
                        prior = tracks.get(ident)
                        tracks[ident] = (side, time.monotonic())
                        if prior is None or prior[0] == side or abs(center-line) > width * .12:
                            continue
                        x1, y1, x2, y2 = max(0,x1), max(0,y1), min(width,x2), min(height,y2)
                        crop = frame[y1:y2, x1:x2].copy()
                        if crop.size == 0 or crop.shape[0] < 60 or crop.shape[1] < 25:
                            continue
                        enters = prior[0] == (-1 if direction == 'left_to_right' else 1)
                        if enters:
                            register_entry(crop)
                        else:
                            register_exit(crop)
                    cutoff = time.monotonic() - 40
                    tracks = {key:value for key,value in tracks.items() if value[1] > cutoff}
                cap.release()
                if isinstance(source, str) and not source.startswith(('rtsp://','rtsps://')):
                    break
                stop_flag.wait(3)
        except Exception:
            self.error = 'Falha no processamento de vídeo. Consulte o terminal local.'
        finally:
            with self.frame_lock:
                self.frame = None
            self.running = False


monitor = Monitor()


@app.on_event('startup')
def begin():
    settings = saved_settings()
    if settings:
        monitor.start(stream_url(settings), settings['line_x'], settings['entry_direction'])
    else:
        source = os.getenv('CAMERA_RTSP') or os.getenv('VIDEO_FILE')
        if source:
            monitor.start(source, float(os.getenv('LINE_X', '.5')), os.getenv('ENTRY_DIRECTION', 'left_to_right'))


@app.on_event('shutdown')
def end():
    monitor.stop_flag.set()
