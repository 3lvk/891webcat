import os
import sys
import time
import asyncio
import threading
import json
import urllib.request
from contextlib import asynccontextmanager
import serial
import numpy as np
import sounddevice as sd
import subprocess
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB0")
BAUD_RATE = int(os.getenv("BAUD_RATE", "38400"))
HTTP_PORT = int(os.getenv("HTTP_PORT", "8000"))
SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "48000"))  # 48kHz nativo para Digirig CM108/119
AUDIO_CARD_KEYWORD = os.getenv("AUDIO_CARD_KEYWORD", "USB").lower()
DEMO_FALLBACK = os.getenv("DEMO_FALLBACK", "true").lower() == "true"

def unmute_alsa_cards():
    """Desmutea y ajusta los mezcladores ALSA del Digirig a nivel nominal de línea (45%) para no saturar DATA IN."""
    print("[ALSA INIT] Verificando y ajustando mezcladores de audio a nivel nominal (45%)...")
    cmds = [
        ["amixer", "sset", "Master", "45%", "unmute"],
        ["amixer", "sset", "Speaker", "45%", "unmute"],
        ["amixer", "sset", "PCM", "45%", "unmute"],
        ["amixer", "sset", "Playback", "45%", "unmute"],
        ["amixer", "-c", "1", "sset", "Speaker", "45%", "unmute"],
        ["amixer", "-c", "1", "sset", "Playback", "45%", "unmute"],
        ["amixer", "-c", "2", "sset", "Speaker", "45%", "unmute"],
        ["amixer", "-c", "0", "sset", "Speaker", "45%", "unmute"],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, capture_output=True, timeout=1)
        except Exception:
            pass

unmute_alsa_cards()

cat_clients = set()
rx_client_queues = {}
ser = None
rx_stream = None
tx_stream = None

tx_pcm_buffer = bytearray()
tx_lock = threading.Lock()
tx_audio_logged_time = 0
tx_prebuffering = True
tone_phase = 0.0

def tx_audio_callback(outdata, frames, time_info, status):
    """
    Callback de modulación ALSA (TX hacia Digirig DR-891).
    Canal 0 (Left) = Audio de voz hacia FT-891 DATA IN (Pin 1).
    Canal 1 (Right) = Tono continuo de 1000 Hz mientras haya audio TX.
    Esto activa el VOX por hardware del Digirig DR-891 de forma continua y sólida,
    enclavando la línea PTT del conector Mini-DIN (DATA jack) sin temblores ni cortes.
    Al aterrizar la línea PTT trasera, el FT-891 conmuta a DATA IN y silencia el micrófono frontal.
    """
    global tx_pcm_buffer, tx_audio_logged_time, tx_prebuffering, tone_phase
    bytes_needed = frames * 2  # 16-bit mono = 2 bytes por muestra

    with tx_lock:
        buffer_len = len(tx_pcm_buffer)

        # Pre-buffering: acumula al menos 120ms (11.520 bytes a 48kHz) antes de arrancar
        if tx_prebuffering:
            if buffer_len >= 11520:
                tx_prebuffering = False
            else:
                outdata.fill(0)
                return

        if buffer_len >= bytes_needed:
            chunk = tx_pcm_buffer[:bytes_needed]
            del tx_pcm_buffer[:bytes_needed]
            mono = np.frombuffer(chunk, dtype=np.int16)
        elif buffer_len > 0:
            chunk = bytes(tx_pcm_buffer)
            tx_pcm_buffer.clear()
            avail = np.frombuffer(chunk, dtype=np.int16)
            mono = np.zeros(frames, dtype=np.int16)
            mono[:len(avail)] = avail
            tx_prebuffering = True
        else:
            outdata.fill(0)
            tx_prebuffering = True
            return

        stereo = np.zeros((frames, 2), dtype=np.int16)
        stereo[:, 0] = mono  # Canal 0 (Left): DATA IN puro

        # Canal 1 (Right): Tono PTT continuo de 1 kHz para enclavar el PTT hardware del Digirig
        t = (np.arange(frames) + tone_phase) / actual_sample_rate
        tone_phase = (tone_phase + frames) % actual_sample_rate
        stereo[:, 1] = (np.sin(2.0 * np.pi * 1000.0 * t) * 26000).astype(np.int16)

        outdata[:] = stereo

        now = time.time()
        if now - tx_audio_logged_time > 1.5:
            tx_audio_logged_time = now
            peak = int(np.max(np.abs(mono)))
            print(f"[AUDIO TX -> Digirig] Pico={peak}/32767 | Buffer Jitter: {len(tx_pcm_buffer)} bytes")

main_event_loop = None

try:
    ser = serial.Serial(
        port=SERIAL_PORT,
        baudrate=BAUD_RATE,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_TWO,
        timeout=0,
        rtscts=False,
        dsrdtr=False
    )
    print(f"[CAT] Puerto serie abierto en {SERIAL_PORT} @ {BAUD_RATE} bps (8N2)")
except Exception as e:
    print(f"[CAT] Advertencia: No se pudo abrir {SERIAL_PORT}: {e}")
    if not DEMO_FALLBACK:
        sys.exit(1)

input_device_id = None
output_device_id = None
actual_sample_rate = SAMPLE_RATE

try:
    devices = sd.query_devices()
    for idx, dev in enumerate(devices):
        name = dev['name'].lower()
        if AUDIO_CARD_KEYWORD in name:
            if dev['max_input_channels'] > 0 and input_device_id is None:
                input_device_id = idx
                actual_sample_rate = 48000  # Forzado a 48kHz nativo de Digirig CM108/CM119
                print(f"[AUDIO RX] Entrada Digirig detectada: #{idx} {dev['name']} @ {actual_sample_rate}Hz")
            if dev['max_output_channels'] > 0 and output_device_id is None:
                output_device_id = idx
                print(f"[AUDIO TX] Salida Digirig detectada: #{idx} {dev['name']}")
except Exception as e:
    print(f"[AUDIO] Error enumerando dispositivos ALSA: {e}")

def rx_audio_callback(indata, frames, time_info, status):
    """Callback exclusivo de captura ALSA (RX) desde Digirig/FT-891."""
    global main_event_loop, rx_client_queues
    if status:
        print(f"[AUDIO RX ALSA Status] {status}", file=sys.stderr)
    if rx_client_queues and main_event_loop and not main_event_loop.is_closed():
        pcm_bytes = indata.tobytes()
        def dispatch():
            for q in list(rx_client_queues.values()):
                try:
                    q.put_nowait(pcm_bytes)
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()
                        q.put_nowait(pcm_bytes)
                    except Exception:
                        pass
        main_event_loop.call_soon_threadsafe(dispatch)

async def serial_reader_task():
    """Lee respuestas CAT del FT-891 y las difunde a los navegadores."""
    buffer = b""
    while True:
        if ser and ser.is_open:
            try:
                waiting = ser.in_waiting
                if waiting:
                    data = ser.read(waiting)
                    buffer += data
                    while b";" in buffer:
                        cmd, buffer = buffer.split(b";", 1)
                        full_cmd = (cmd + b";").decode("ascii", errors="ignore")
                        disconnected = set()
                        for client in list(cat_clients):
                            try:
                                await client.send_text(full_cmd)
                            except Exception:
                                disconnected.add(client)
                        cat_clients.difference_update(disconnected)
            except Exception:
                await asyncio.sleep(0.05)
        await asyncio.sleep(0.01)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_event_loop, rx_stream, tx_stream
    main_event_loop = asyncio.get_running_loop()

    asyncio.create_task(serial_reader_task())

    if input_device_id is not None:
        try:
            rx_stream = sd.InputStream(
                device=input_device_id,
                channels=1,
                samplerate=actual_sample_rate,
                dtype='int16',
                blocksize=1920,
                callback=rx_audio_callback
            )
            rx_stream.start()
            print(f"[AUDIO RX] Captura iniciada en #{input_device_id} @ {actual_sample_rate} Hz")
        except Exception as e:
            print(f"[AUDIO RX] Error iniciando InputStream: {e}")

    if output_device_id is not None:
        try:
            tx_stream = sd.OutputStream(
                device=output_device_id,
                channels=2,
                samplerate=actual_sample_rate,
                dtype='int16',
                blocksize=1920,
                callback=tx_audio_callback
            )
            tx_stream.start()
            print(f"[AUDIO TX] Modulación estéreo iniciada en #{output_device_id} @ {actual_sample_rate} Hz")
        except Exception as e:
            print(f"[AUDIO TX] Error iniciando OutputStream: {e}")

    yield

    if rx_stream:
        try:
            rx_stream.stop()
            rx_stream.close()
        except Exception:
            pass
    if tx_stream:
        try:
            tx_stream.stop()
            tx_stream.close()
        except Exception:
            pass

app = FastAPI(title="Yaesu FT-891 WebCAT & Audio Server", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="/app/static"), name="static")

cached_spots_data = {"spots": []}

def background_spots_worker():
    """Descarga spots en un hilo independiente cada 45s (POTA, LLOTA, SOTA y DX Cluster)."""
    global cached_spots_data
    while True:
        combined = []

        # 1. POTA (Parks on the Air)
        try:
            req_pota = urllib.request.Request(
                "https://api.pota.app/spot/activator",
                headers={"User-Agent": "FT891WebCAT/2.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req_pota, timeout=4) as response:
                if response.status == 200:
                    pota_data = json.loads(response.read().decode("utf-8"))
                    for s in pota_data[:35]:
                        try:
                            freq_num = float(s.get("frequency", 0))
                            combined.append({
                                "id": f"POTA-{s.get('spotId', '')}",
                                "activator": str(s.get("activator", "")).upper(),
                                "program": "POTA",
                                "ref": str(s.get("reference", "")).upper(),
                                "parkName": s.get("name") or "Parque Activo POTA",
                                "freq": freq_num,
                                "mode": (s.get("mode") or "SSB").upper(),
                                "time": s.get("spotTime", "")[11:16] + "z" if s.get("spotTime") else "Ahora",
                                "spotter": s.get("spotter") or "POTA",
                                "comments": s.get("comments") or "POTA Activator"
                            })
                        except Exception:
                            continue
        except Exception as e:
            print(f"[BG SPOTS] Error POTA: {e}")

        # 2. LLOTA (Lakes and Lagoons on the Air)
        try:
            req_llota = urllib.request.Request(
                "https://llota.app/api/public/spots",
                headers={"User-Agent": "FT891WebCAT/2.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req_llota, timeout=4) as response:
                if response.status == 200:
                    llota_raw = json.loads(response.read().decode("utf-8"))
                    llota_list = llota_raw if isinstance(llota_raw, list) else (llota_raw.get("spots") or llota_raw.get("data") or [])
                    for s in llota_list:
                        try:
                            freq_num = float(s.get("frequency") or s.get("freq") or 14250)
                            if freq_num > 100000:
                                freq_num /= 1000.0
                            elif freq_num < 100:
                                freq_num *= 1000.0

                            combined.append({
                                "id": f"LLOTA-{s.get('id') or s.get('spotId') or ''}",
                                "activator": str(s.get("activator") or s.get("callsign") or s.get("call") or "OPERADOR").upper(),
                                "program": "LLOTA",
                                "ref": str(s.get("reference") or s.get("ref") or s.get("lake_id") or "LAKE").upper(),
                                "parkName": s.get("lakeName") or s.get("name") or s.get("lake") or "Lago / Laguna (LLOTA)",
                                "freq": freq_num,
                                "mode": (s.get("mode") or "SSB").upper(),
                                "time": s.get("time") or (s.get("created_at", "")[11:16] + "z" if s.get("created_at") else "Ahora"),
                                "spotter": s.get("spotter") or "llota.app",
                                "comments": s.get("comments") or "Lakes & Lagoons On The Air (llota.app)"
                            })
                        except Exception:
                            continue
        except Exception as e:
            print(f"[BG SPOTS] Error LLOTA: {e}")

        # 3. SOTA (Summits on the Air)
        try:
            req_sota = urllib.request.Request(
                "https://api2.sota.org.uk/api/spots/25",
                headers={"User-Agent": "FT891WebCAT/2.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req_sota, timeout=4) as response:
                if response.status == 200:
                    sota_data = json.loads(response.read().decode("utf-8"))
                    for s in sota_data:
                        try:
                            freq_num = float(s.get("frequency") or 14.285)
                            if freq_num < 100:
                                freq_num *= 1000.0  # de MHz a kHz
                            combined.append({
                                "id": f"SOTA-{s.get('id', '')}",
                                "activator": str(s.get("activatorCallsign", "")).upper(),
                                "program": "SOTA",
                                "ref": str(s.get("associationCode", "") + "/" + s.get("summitCode", "")).upper(),
                                "parkName": s.get("summitName") or "Cumbre SOTA",
                                "freq": freq_num,
                                "mode": (s.get("mode") or "SSB").upper(),
                                "time": str(s.get("timeStamp", ""))[11:16] + "z" if s.get("timeStamp") else "Ahora",
                                "spotter": s.get("spotter") or "SOTAwatch",
                                "comments": s.get("comments") or "SOTA Summit"
                            })
                        except Exception:
                            continue
        except Exception as e:
            print(f"[BG SPOTS] Error SOTA: {e}")

        # 4. DX Cluster (DX Summit / Spothole)
        try:
            req_dx = urllib.request.Request(
                "http://www.dxsummit.fi/api/v1/spots?limit=40",
                headers={"User-Agent": "FT891WebCAT/2.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req_dx, timeout=4) as response:
                if response.status == 200:
                    dx_data = json.loads(response.read().decode("utf-8"))
                    for s in dx_data:
                        try:
                            freq_num = float(s.get("frequency") or 0)
                            if freq_num > 100000:
                                freq_num /= 1000.0
                            elif freq_num < 100:
                                freq_num *= 1000.0

                            dx_call = str(s.get("dx_call") or s.get("call") or "").upper()
                            if not dx_call:
                                continue

                            # Detección heurística de modo si no viene explícito
                            info_txt = str(s.get("info") or s.get("comment") or "")
                            mode_dx = "SSB"
                            info_upper = info_txt.upper()
                            if "CW" in info_upper:
                                mode_dx = "CW"
                            elif "FT8" in info_upper or "FT4" in info_upper or "RTTY" in info_upper or "DATA" in info_upper:
                                mode_dx = "DATA"
                            elif freq_num % 1 == 0 and (int(freq_num) in [7074, 14074, 21074, 28074, 3573, 10136, 18100]):
                                mode_dx = "DATA"

                            time_raw = str(s.get("time") or "")
                            time_str = time_raw[11:16] + "z" if len(time_raw) >= 16 else "Ahora"

                            combined.append({
                                "id": f"DX-{dx_call}-{freq_num}",
                                "activator": dx_call,
                                "program": "DX",
                                "ref": "DX CLUSTER",
                                "parkName": f"DX de {s.get('de_call') or 'Cluster'}",
                                "freq": freq_num,
                                "mode": mode_dx,
                                "time": time_str,
                                "spotter": s.get("de_call") or "DXCluster",
                                "comments": info_txt or "DX Cluster Spot"
                            })
                        except Exception:
                            continue
        except Exception as e:
            # Fallback a Spothole si DXSummit tiene alguna interrupción
            try:
                req_spothole = urllib.request.Request(
                    "https://spothole.app/api/v1/spots?limit=40",
                    headers={"User-Agent": "FT891WebCAT/2.0", "Accept": "application/json"}
                )
                with urllib.request.urlopen(req_spothole, timeout=4) as response:
                    if response.status == 200:
                        spothole_data = json.loads(response.read().decode("utf-8"))
                        for s in spothole_data:
                            try:
                                freq_num = float(s.get("frequency") or 0)
                                if freq_num > 100000:
                                    freq_num /= 1000.0
                                elif freq_num < 100:
                                    freq_num *= 1000.0
                                dx_call = str(s.get("dx_call") or "").upper()
                                if not dx_call:
                                    continue
                                combined.append({
                                    "id": f"DX-{dx_call}-{freq_num}",
                                    "activator": dx_call,
                                    "program": "DX",
                                    "ref": "DX CLUSTER",
                                    "parkName": f"DX vía {s.get('source') or 'Cluster'}",
                                    "freq": freq_num,
                                    "mode": str(s.get("mode") or "SSB").upper(),
                                    "time": str(s.get("time") or "")[11:16] + "z" if s.get("time") else "Ahora",
                                    "spotter": s.get("spotter_call") or "DX",
                                    "comments": s.get("comment") or "DX Spot"
                                })
                            except Exception:
                                continue
            except Exception:
                pass

        if combined:
            cached_spots_data = {"spots": combined}

        time.sleep(45)

threading.Thread(target=background_spots_worker, daemon=True).start()

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    svg_favicon = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect width="100" height="100" rx="20" fill="#ffb300"/>'
        '<text x="50" y="65" font-size="50" font-family="sans-serif" font-weight="bold" '
        'text-anchor="middle" fill="#0c0f12">891</text></svg>'
    )
    return Response(content=svg_favicon, media_type="image/svg+xml")

@app.get("/api/spots")
async def get_spots_proxy():
    return cached_spots_data

@app.get("/")
async def get_index():
    response = FileResponse("/app/static/index.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.websocket("/ws/cat")
async def websocket_cat_endpoint(websocket: WebSocket):
    await websocket.accept()
    cat_clients.add(websocket)
    try:
        while True:
            cmd = await websocket.receive_text()
            cmd = cmd.strip()
            if not cmd.endswith(";"):
                cmd += ";"

            if ser and ser.is_open:
                if cmd.startswith("PS1"):
                    ser.write(b"    ;")
                    await asyncio.sleep(0.02)
                ser.write(cmd.encode("ascii"))
            elif DEMO_FALLBACK:
                await websocket.send_text(cmd)
    except WebSocketDisconnect:
        cat_clients.discard(websocket)

@app.websocket("/ws/audio/rx")
async def websocket_audio_rx_endpoint(websocket: WebSocket):
    await websocket.accept()
    q = asyncio.Queue(maxsize=50)
    rx_client_queues[websocket] = q
    try:
        while True:
            pcm_bytes = await q.get()
            await websocket.send_bytes(pcm_bytes)
    except WebSocketDisconnect:
        pass
    finally:
        rx_client_queues.pop(websocket, None)

@app.websocket("/ws/audio/tx")
async def websocket_audio_tx_endpoint(websocket: WebSocket):
    global tx_pcm_buffer, tx_prebuffering
    await websocket.accept()
    print("[AUDIO TX WS] Cliente móvil/PC conectado a transmisión de micrófono.")
    with tx_lock:
        tx_pcm_buffer.clear()
        tx_prebuffering = True

    try:
        while True:
            data = await websocket.receive_bytes()
            with tx_lock:
                if len(tx_pcm_buffer) > 96000:
                    del tx_pcm_buffer[:38400]
                tx_pcm_buffer.extend(data)
    except WebSocketDisconnect:
        print("[AUDIO TX WS] Cliente móvil/PC desconectó micrófono.")
        with tx_lock:
            tx_pcm_buffer.clear()
            tx_prebuffering = True

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT, log_level="info")
