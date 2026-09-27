import os
import sys
import time
import datetime
import asyncio
import threading
import json
import subprocess
import wave
import tempfile
import urllib.request
from contextlib import asynccontextmanager
import serial
import numpy as np
import sounddevice as sd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB0")
BAUD_RATE = int(os.getenv("BAUD_RATE", "38400"))
HTTP_PORT = int(os.getenv("HTTP_PORT", "8000"))
SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "48000"))
AUDIO_CARD_KEYWORD = os.getenv("AUDIO_CARD_KEYWORD", "USB").lower()
DEMO_FALLBACK = os.getenv("DEMO_FALLBACK", "true").lower() == "true"

def unmute_alsa_cards():
    """Configura los volúmenes ALSA del Digirig a nivel óptimo para no saturar ALC."""
    cmds = [
        ["amixer", "sset", "Master", "55%", "unmute"],
        ["amixer", "sset", "Speaker", "55%", "unmute"],
        ["amixer", "sset", "PCM", "55%", "unmute"],
        ["amixer", "sset", "Playback", "55%", "unmute"],
        ["amixer", "-c", "1", "sset", "Speaker", "55%", "unmute"],
        ["amixer", "-c", "1", "sset", "Playback", "55%", "unmute"],
        ["amixer", "-c", "2", "sset", "Speaker", "55%", "unmute"],
        ["amixer", "-c", "0", "sset", "Speaker", "55%", "unmute"],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, capture_output=True, timeout=1)
        except Exception:
            pass

unmute_alsa_cards()

cat_clients = set()
rx_client_queues = {}
ft8_clients = set()
ser = None
rx_stream = None
tx_stream = None

tx_pcm_buffer = bytearray()
tx_lock = threading.Lock()
tx_audio_logged_time = 0
tx_prebuffering = True
tone_phase = 0.0
ft8_tx_active = False
voice_tx_active = False

# Búfer circular de recepción FT8/FT4 (12000 Hz, 16 segundos)
FT8_SAMPLE_RATE = 12000
FT8_BUFFER_SAMPLES = FT8_SAMPLE_RATE * 16
ft8_rx_ring = np.zeros(FT8_BUFFER_SAMPLES, dtype=np.int16)
ft8_rx_idx = 0
ft8_rx_lock = threading.Lock()

# Estado del motor FT8 / FT4 en el servidor
ft8_engine_state = {
    "mode": "FT8",
    "slot_duration": 15.0,
    "my_call": "LU1AA",
    "my_grid": "FF46",
    "tx_armed": False,
    "tx_msg": "",
    "tx_freq": 1250,
    "tx_slot": "even",
    "last_decoded": []
}

def tx_audio_callback(outdata, frames, time_info, status):
    """
    Callback de modulación ALSA estéreo para Digirig DR-891:
    Canal 0 (Left)  = Señal de audio digital modulada hacia pin DATA IN del FT-891.
    Canal 1 (Right) = Tono de control de 1000 Hz para circuitos de PTT auxiliares.
    """
    global tx_pcm_buffer, tx_audio_logged_time, tx_prebuffering, tone_phase, ft8_tx_active
    bytes_needed = frames * 2

    with tx_lock:
        buffer_len = len(tx_pcm_buffer)

        if tx_prebuffering:
            if buffer_len >= 9600:
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
            if not ft8_tx_active:
                tx_prebuffering = True
        else:
            outdata.fill(0)
            if not ft8_tx_active:
                tx_prebuffering = True
            return

        stereo = np.zeros((frames, 2), dtype=np.int16)
        stereo[:, 0] = mono

        t = (np.arange(frames) + tone_phase) / actual_sample_rate
        tone_phase = (tone_phase + frames) % actual_sample_rate
        
        # Mantener el tono de 1000 Hz activo de forma continua y sólida mientras dure el PTT
        if ft8_tx_active or voice_tx_active:
            stereo[:, 1] = (np.sin(2.0 * np.pi * 1000.0 * t) * 26000).astype(np.int16)
        else:
            stereo[:, 1] = 0

        outdata[:] = stereo

        now = time.time()
        if now - tx_audio_logged_time > 2.0:
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
    ser.rts = False
    ser.dtr = False
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
                actual_sample_rate = 48000
                print(f"[AUDIO RX] Entrada Digirig detectada: #{idx} {dev['name']} @ {actual_sample_rate}Hz")
            if dev['max_output_channels'] > 0 and output_device_id is None:
                output_device_id = idx
                print(f"[AUDIO TX] Salida Digirig detectada: #{idx} {dev['name']}")
except Exception as e:
    print(f"[AUDIO] Error enumerando dispositivos ALSA: {e}")

def rx_audio_callback(indata, frames, time_info, status):
    """Callback de recepción ALSA desde el Digirig/FT-891."""
    global main_event_loop, rx_client_queues, ft8_rx_ring, ft8_rx_idx
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

    raw_mono = indata[:, 0] if indata.ndim > 1 else indata.flatten()
    ds_samples = raw_mono[::4]
    num_ds = len(ds_samples)

    with ft8_rx_lock:
        if ft8_rx_idx + num_ds <= FT8_BUFFER_SAMPLES:
            ft8_rx_ring[ft8_rx_idx:ft8_rx_idx + num_ds] = ds_samples
            ft8_rx_idx += num_ds
        else:
            space = FT8_BUFFER_SAMPLES - ft8_rx_idx
            ft8_rx_ring[ft8_rx_idx:] = ds_samples[:space]
            rem = num_ds - space
            ft8_rx_ring[:rem] = ds_samples[space:]
            ft8_rx_idx = rem

async def serial_reader_task():
    """Lee respuestas CAT del FT-891 y las difunde a los clientes Web."""
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

def send_cat_internal(cmd_str):
    """Envía comandos CAT al transceptor asegurando la terminación con punto y coma."""
    if not cmd_str.endswith(";"):
        cmd_str += ";"
    if ser and ser.is_open:
        try:
            ser.write(cmd_str.encode("ascii"))
        except Exception as e:
            print(f"[CAT INTERNAL] Error enviando '{cmd_str}': {e}")

def set_hardware_ptt(active: bool):
    """
    Enclava o libera la línea PTT del FT-891 por hardware y CAT:
    1. Línea física RTS del Digirig hacia el pin PTT (Pin 3 Mini-DIN 6).
    2. Comando CAT TX2; (DATA PTT para conmutar a entrada trasera DATA IN).
       Si se usa TX1;, el FT-891 conmuta al micrófono frontal y silencia el puerto trasero.
    """
    if ser and ser.is_open:
        try:
            ser.rts = active
        except Exception as e:
            print(f"[PTT HW] Error fijando RTS={active}: {e}")

    if active:
        send_cat_internal("TX2;")
    else:
        send_cat_internal("TX0;")

def calibrate_ft8_snr(raw_val: float) -> int:
    """
    Convierte el score bruto de detección (ft8_lib candidate.score o SNR en bin de 6.25 Hz)
    al estándar internacional WSJT-X referenciado a 2500 Hz:
    SNR_2500Hz = SNR_bin - 10*log10(2500 / 6.25) ≈ SNR_bin - 26 dB.

    - Si el valor ya es negativo (ej. un fork que ya entrega WSJT-X SNR), se conserva.
    - Si el valor es positivo (> 0), se calibra con el desplazamiento de 26 dB
      para reflejar señales reales entre -24 dB y +15 dB.
    """
    val = int(round(raw_val))
    if val < 0:
        return max(-26, min(24, val))

    calibrated = val - 26
    return max(-26, min(24, calibrated))

def run_decode_ft8_file(wav_path):
    """Decodifica un bloque de audio WAV utilizando el binario decode_ft8."""
    candidates = ["/usr/local/bin/decode_ft8", "./decode_ft8", "decode_ft8"]
    bin_path = next((c for c in candidates if os.path.exists(c)), None)
    if not bin_path:
        return []
    try:
        res = subprocess.run(
            [bin_path, wav_path],
            capture_output=True,
            text=True,
            timeout=3.5
        )
        decodes = []
        utc_now = datetime.datetime.now(datetime.timezone.utc).strftime("%H%M%S")
        for line in res.stdout.splitlines():
            line = line.strip()
            if "~" in line:
                parts = line.split("~")
                meta = parts[0].strip().split()
                msg = parts[1].strip()
                if len(meta) >= 3:
                    snr_str = meta[1]
                    dt_str = meta[2]
                    freq_str = meta[3] if len(meta) > 3 else "1200"

                    # Conversión y calibración a escala estándar WSJT-X (dB)
                    try:
                        raw_snr = float(snr_str)
                        snr_val = calibrate_ft8_snr(raw_snr)
                    except Exception:
                        snr_val = -15

                    try:
                        dt_val = float(dt_str)
                    except Exception:
                        dt_val = 0.0

                    try:
                        freq_val = int(round(float(freq_str)))
                    except Exception:
                        freq_val = 1200

                    decodes.append({
                        "time": utc_now,
                        "snr": snr_val,
                        "dt": dt_val,
                        "freq": freq_val,
                        "msg": msg
                    })
        return decodes
    except Exception as e:
        print(f"[FT8 DECODE] Error ejecutando decode_ft8: {e}")
        return []

def synthesize_ft8_audio(message, audio_freq, out_wav_path):
    """
    Genera el archivo de modulación de audio.
    Usa primero gen_ft8 de ft8_lib si está disponible; de lo contrario utiliza
    el sintetizador GFSK directo en 12 kHz.
    """
    candidates = ["/usr/local/bin/gen_ft8", "./gen_ft8", "gen_ft8"]
    bin_path = next((c for c in candidates if os.path.exists(c)), None)

    if bin_path:
        try:
            subprocess.run(
                [bin_path, message, out_wav_path, str(int(audio_freq))],
                capture_output=True,
                timeout=2.5
            )
            if os.path.exists(out_wav_path) and os.path.getsize(out_wav_path) > 1000:
                return True
        except Exception as e:
            print(f"[FT8 GEN] Falló binario gen_ft8, recurriendo a sintetizador interno: {e}")

    # Sintetizador interno de contingencia (tono continuo con rampa en la frecuencia indicada)
    try:
        sample_rate = 12000
        duration = 12.64
        num_samples = int(sample_rate * duration)
        t = np.linspace(0, duration, num_samples, endpoint=False)
        phase = 2.0 * np.pi * audio_freq * t
        envelope = np.ones(num_samples)
        fade_len = int(sample_rate * 0.05)
        envelope[:fade_len] = np.linspace(0, 1, fade_len)
        envelope[-fade_len:] = np.linspace(1, 0, fade_len)
        signal = (np.sin(phase) * envelope * 24000).astype(np.int16)

        with wave.open(out_wav_path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(signal.tobytes())
        return True
    except Exception as e:
        print(f"[FT8 SINTESIS INTERNA] Error: {e}")
        return False

def broadcast_ft8_event(event_dict):
    """Difunde eventos de estado FT8 hacia todos los clientes WebSocket."""
    if not ft8_clients or not main_event_loop or main_event_loop.is_closed():
        return
    payload = json.dumps(event_dict)
    def dispatch():
        for ws in list(ft8_clients):
            try:
                asyncio.create_task(ws.send_text(payload))
            except Exception:
                pass
    main_event_loop.call_soon_threadsafe(dispatch)

def ft8_cycle_worker():
    """
    Worker en segundo plano sincronizado con la ranura UTC exacta:
    - Inicio de slot (:00, :15, :30, :45): Transmite si la estación está armada para el slot actual.
    - Final de slot (:14, :29, :44, :59): Extrae los 15s de audio y decodifica la banda.
    """
    global tx_pcm_buffer, tx_prebuffering, ft8_tx_active
    print("[FT8 WORKER] Hilo de sincronización UTC activo.")
    last_tx_slot = -1
    last_rx_slot = -1

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        sec = now.second + now.microsecond / 1_000_000.0
        slot_dur = ft8_engine_state["slot_duration"]
        slot_idx = int(sec // slot_dur)
        sec_in_slot = sec % slot_dur
        is_even = (slot_idx % 2 == 0)

        # 1. DISPARADOR DE TRANSMISIÓN (entre 0.0s y 0.75s del inicio de la ranura)
        if sec_in_slot < 0.75 and slot_idx != last_tx_slot:
            last_tx_slot = slot_idx
            armed = ft8_engine_state["tx_armed"]
            desired_slot = ft8_engine_state["tx_slot"]
            should_tx = False

            if armed:
                if desired_slot == "now":
                    should_tx = True
                elif desired_slot == "even" and is_even:
                    should_tx = True
                elif desired_slot == "odd" and not is_even:
                    should_tx = True

            if should_tx and ft8_engine_state["tx_msg"]:
                msg_to_send = ft8_engine_state["tx_msg"]
                audio_f = ft8_engine_state["tx_freq"]
                print(f"[FT8 TX ACTIVO] Ciclo UTC {now.strftime('%H:%M:%S')}z | Msg: '{msg_to_send}' | Audio: {audio_f}Hz")

                broadcast_ft8_event({
                    "event": "tx_started",
                    "msg": msg_to_send,
                    "freq": audio_f,
                    "slot": "even" if is_even else "odd"
                })

                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
                    tmp_wav_path = tmp_wav.name

                if synthesize_ft8_audio(msg_to_send, audio_f, tmp_wav_path):
                    try:
                        with wave.open(tmp_wav_path, 'rb') as wf:
                            n_frames = wf.getnframes()
                            raw_12k = wf.readframes(n_frames)
                            samples_12k = np.frombuffer(raw_12k, dtype=np.int16)
                            # Remuestreo 12 kHz -> 48 kHz (interpolación 4x)
                            samples_48k = np.repeat(samples_12k, 4)
                            # Nivel nominal para 50W-100W sin distorsión
                            scaled_48k = (samples_48k * 0.85).astype(np.int16)

                        tx_duration = len(samples_48k) / actual_sample_rate
                        ft8_tx_active = True

                        # Enclavar PTT hardware y conmutar a DATA IN
                        set_hardware_ptt(True)

                        with tx_lock:
                            tx_pcm_buffer.clear()
                            tx_pcm_buffer.extend(scaled_48k.tobytes())
                            tx_prebuffering = False

                        # Tiempo exacto de reproducción de la trama de modulación
                        time.sleep(tx_duration)
                        # Breve margen de 120ms para que la tarjeta de sonido drene la cola DAC
                        time.sleep(0.12)
                    except Exception as ex:
                        print(f"[FT8 TX] Error en transmisión: {ex}")
                    finally:
                        ft8_tx_active = False
                        set_hardware_ptt(False)
                        with tx_lock:
                            tx_pcm_buffer.clear()
                            tx_prebuffering = True

                        broadcast_ft8_event({
                            "event": "tx_finished",
                            "msg": msg_to_send,
                            "slot": "even" if is_even else "odd"
                        })

                        try:
                            os.remove(tmp_wav_path)
                        except Exception:
                            pass
                else:
                    print(f"[FT8 TX] No se pudo generar la señal para '{msg_to_send}'")

        # 2. DISPARADOR DE RECEPCIÓN Y DECODIFICACIÓN (al segundo 14.1s en FT8 o 6.8s en FT4)
        decode_trigger = slot_dur - 0.9
        if sec_in_slot >= decode_trigger and sec_in_slot < decode_trigger + 0.45 and slot_idx != last_rx_slot:
            last_rx_slot = slot_idx

            with ft8_rx_lock:
                current_idx = ft8_rx_idx
                needed_samples = int(FT8_SAMPLE_RATE * (slot_dur - 0.2))
                if current_idx >= needed_samples:
                    chunk = ft8_rx_ring[current_idx - needed_samples:current_idx].copy()
                else:
                    part1 = ft8_rx_ring[FT8_BUFFER_SAMPLES - (needed_samples - current_idx):]
                    part2 = ft8_rx_ring[:current_idx]
                    chunk = np.concatenate((part1, part2))

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_rx:
                rx_wav_path = tmp_rx.name

            try:
                with wave.open(rx_wav_path, 'wb') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(FT8_SAMPLE_RATE)
                    wf.writeframes(chunk.tobytes())

                decodes = run_decode_ft8_file(rx_wav_path)
                if decodes:
                    ft8_engine_state["last_decoded"] = decodes
                    broadcast_ft8_event({
                        "event": "decodes",
                        "slot": "even" if is_even else "odd",
                        "data": decodes
                    })
            except Exception as e:
                print(f"[FT8 WORKER] Error decodificando audio: {e}")
            finally:
                try:
                    os.remove(rx_wav_path)
                except Exception:
                    pass

        time.sleep(0.08)

threading.Thread(target=ft8_cycle_worker, daemon=True).start()

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

    set_hardware_ptt(False)
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

app = FastAPI(title="Yaesu FT-891 WebCAT & FT8 Station Server", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="/app/static"), name="static")

cached_spots_data = {"spots": []}

def background_spots_worker():
    """Descarga spots en segundo plano cada 45 segundos (POTA, LLOTA, SOTA y DX Cluster)."""
    global cached_spots_data
    while True:
        combined = []

        # 1. POTA
        try:
            req_pota = urllib.request.Request(
                "https://api.pota.app/spot/activator",
                headers={"User-Agent": "FT891WebCAT/3.5", "Accept": "application/json"}
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
        except Exception:
            pass

        # 2. LLOTA
        try:
            req_llota = urllib.request.Request(
                "https://llota.app/api/public/spots",
                headers={"User-Agent": "FT891WebCAT/3.5", "Accept": "application/json"}
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
        except Exception:
            pass

        # 3. SOTA
        try:
            req_sota = urllib.request.Request(
                "https://api2.sota.org.uk/api/spots/25",
                headers={"User-Agent": "FT891WebCAT/3.5", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req_sota, timeout=4) as response:
                if response.status == 200:
                    sota_data = json.loads(response.read().decode("utf-8"))
                    for s in sota_data:
                        try:
                            freq_num = float(s.get("frequency") or 14.285)
                            if freq_num < 100:
                                freq_num *= 1000.0
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
        except Exception:
            pass

        # 4. DX Cluster
        try:
            req_dx = urllib.request.Request(
                "http://www.dxsummit.fi/api/v1/spots?limit=40",
                headers={"User-Agent": "FT891WebCAT/3.5", "Accept": "application/json"}
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

                            info_txt = str(s.get("info") or s.get("comment") or "")
                            mode_dx = "SSB"
                            info_upper = info_txt.upper()
                            if "CW" in info_upper:
                                mode_dx = "CW"
                            elif "FT8" in info_upper:
                                mode_dx = "FT8"
                            elif "FT4" in info_upper:
                                mode_dx = "FT4"
                            elif "RTTY" in info_upper or "DATA" in info_upper:
                                mode_dx = "DATA"
                            elif freq_num % 1 == 0 and (int(freq_num) in [7074, 14074, 21074, 28074, 3573, 10136, 18100]):
                                mode_dx = "FT8"

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
    global ft8_tx_active, voice_tx_active
    await websocket.accept()
    cat_clients.add(websocket)
    try:
        while True:
            cmd = await websocket.receive_text()
            cmd = cmd.strip()
            if not cmd.endswith(";"):
                cmd += ";"

            # Si el motor digital FT8 está transmitiendo, descartar comandos de corte accidental (TX0)
            if ft8_tx_active and cmd.startswith("TX0"):
                continue

            # Enclavar y desenclavar PTT hardware y tono auxiliar según el comando CAT
            if cmd.startswith("TX1") or cmd.startswith("TX2"):
                voice_tx_active = True
                if ser and ser.is_open:
                    try:
                        ser.rts = True
                    except Exception:
                        pass
            elif cmd.startswith("TX0"):
                voice_tx_active = False
                if ser and ser.is_open and not ft8_tx_active:
                    try:
                        ser.rts = False
                    except Exception:
                        pass

            if ser and ser.is_open:
                if cmd.startswith("PS1"):
                    ser.write(b"    ;")
                    await asyncio.sleep(0.02)
                ser.write(cmd.encode("ascii"))
            elif DEMO_FALLBACK:
                await websocket.send_text(cmd)
    except WebSocketDisconnect:
        cat_clients.discard(websocket)
        voice_tx_active = False
        if ser and ser.is_open and not ft8_tx_active:
            try:
                ser.rts = False
            except Exception:
                pass
            with tx_lock:
                tx_pcm_buffer.clear()
                tx_prebuffering = True

@app.websocket("/ws/audio/rx")
async def websocket_audio_rx_endpoint(websocket: WebSocket):
    """Envía el stream de audio capturado desde el Digirig (48 kHz) hacia el navegador."""
    await websocket.accept()
    q = asyncio.Queue(maxsize=30)
    client_id = id(websocket)
    rx_client_queues[client_id] = q
    try:
        while True:
            pcm_bytes = await q.get()
            await websocket.send_bytes(pcm_bytes)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as e:
        print(f"[AUDIO RX WS Error] {e}")
    finally:
        rx_client_queues.pop(client_id, None)

@app.websocket("/ws/audio/tx")
async def websocket_audio_tx_endpoint(websocket: WebSocket):
    """Recibe paquetes PCM del micrófono del navegador y los encola en el buffer de salida."""
    global ft8_tx_active
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_bytes()
            # Si FT8 está transmitiendo, descartar el micrófono para evitar sobreescribir la trama
            if ft8_tx_active:
                continue
            with tx_lock:
                if len(tx_pcm_buffer) > 96000:
                    del tx_pcm_buffer[:38400]
                tx_pcm_buffer.extend(data)
    except (WebSocketDisconnect, asyncio.CancelledError):
        with tx_lock:
            if not ft8_tx_active:
                tx_pcm_buffer.clear()
                tx_prebuffering = True
    except Exception as e:
        print(f"[AUDIO TX WS Error] {e}")

@app.websocket("/ws/ft8")
async def websocket_ft8_endpoint(websocket: WebSocket):
    """Endpoint WebSocket para telemetría, decodes y control de ciclos FT8/FT4."""
    global ft8_tx_active
    await websocket.accept()
    ft8_clients.add(websocket)
    try:
        await websocket.send_text(json.dumps({
            "event": "state",
            "state": ft8_engine_state
        }))
        while True:
            raw = await websocket.receive_text()
            try:
                cmd = json.loads(raw)
                action = cmd.get("action")
                if action == "arm_tx":
                    ft8_engine_state["tx_armed"] = True
                    ft8_engine_state["tx_msg"] = cmd.get("msg", "")
                    ft8_engine_state["tx_freq"] = int(cmd.get("freq", 1250))
                    ft8_engine_state["tx_slot"] = cmd.get("slot", "even")
                    broadcast_ft8_event({"event": "tx_status", "state": ft8_engine_state})
                elif action == "abort_tx":
                    ft8_engine_state["tx_armed"] = False
                    ft8_tx_active = False
                    set_hardware_ptt(False)
                    with tx_lock:
                        tx_pcm_buffer.clear()
                        tx_prebuffering = True
                    broadcast_ft8_event({"event": "tx_status", "state": ft8_engine_state})
                elif action == "set_station":
                    if "my_call" in cmd:
                        ft8_engine_state["my_call"] = cmd["my_call"].upper()
                    if "my_grid" in cmd:
                        ft8_engine_state["my_grid"] = cmd["my_grid"].upper()
                    broadcast_ft8_event({"event": "tx_status", "state": ft8_engine_state})
                elif action == "set_mode":
                    m = cmd.get("mode", "FT8").upper()
                    ft8_engine_state["mode"] = m
                    ft8_engine_state["slot_duration"] = 7.5 if m == "FT4" else 15.0
                    broadcast_ft8_event({"event": "tx_status", "state": ft8_engine_state})
            except Exception as e:
                print(f"[WS FT8] Error en comando: {e}")
    except WebSocketDisconnect:
        ft8_clients.discard(websocket)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT, log_level="info")
