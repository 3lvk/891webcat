import os
import time
import datetime
import asyncio
import threading
import json
import subprocess
import wave
import tempfile
import urllib.request
import base64
import hashlib
import hmac
import secrets
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
import serial
import numpy as np
import sounddevice as sd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response, Request, Body
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketState
import uvicorn

SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB0")
CW_KEY_PORT = os.getenv("CW_KEY_PORT", "/dev/ttyUSB1")
BAUD_RATE = int(os.getenv("BAUD_RATE", "38400"))
HTTP_PORT = int(os.getenv("HTTP_PORT", "8000"))
SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "48000"))
AUDIO_CARD_KEYWORD = os.getenv("AUDIO_CARD_KEYWORD", "USB").lower()
DEMO_FALLBACK = os.getenv("DEMO_FALLBACK", "true").lower() == "true"
STATION_USERNAME = os.getenv("STATION_USERNAME", "")
STATION_PASSWORD = os.getenv("STATION_PASSWORD", "")
STATION_SESSION_SECRET = os.getenv("STATION_SESSION_SECRET", "")
SESSION_COOKIE_NAME = "ft891_session"
SESSION_MAX_AGE = 12 * 60 * 60
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
SESSION_NONCES = {}
SESSION_LOCK = threading.Lock()
LOGIN_FAILURES = {}
LOGIN_LOCK = threading.Lock()
LOGIN_FAILURE_LIMIT = 5
LOGIN_WINDOW_SECONDS = 300
LOGIN_BLOCK_SECONDS = 300
websocket_auth_tasks = set()


class LoginCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)

def unmute_alsa_cards():
    """Configura los niveles de volumen en ALSA para operación sin saturación."""
    cmds = [
        ["amixer", "sset", "Master", "85%", "unmute"],
        ["amixer", "sset", "Speaker", "85%", "unmute"],
        ["amixer", "sset", "PCM", "85%", "unmute"],
        ["amixer", "sset", "Playback", "85%", "unmute"],
        ["amixer", "-c", "1", "sset", "Speaker", "85%", "unmute"],
        ["amixer", "-c", "1", "sset", "Playback", "85%", "unmute"],
        ["amixer", "-c", "2", "sset", "Speaker", "85%", "unmute"],
        ["amixer", "-c", "0", "sset", "Speaker", "85%", "unmute"],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, capture_output=True, timeout=1)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[AUDIO] No se pudo configurar ALSA ({' '.join(cmd)}): {exc}")

cat_clients = set()
rx_client_queues = {}
ft8_clients = set()
cw_clients = set()
ser = None
cw_ser = None
rx_stream = None
tx_stream = None

tx_pcm_buffer = bytearray()
tx_lock = threading.Lock()
tx_audio_logged_time = 0
tx_prebuffering = True
tone_phase = 0.0
cw_tone_phase = 0.0
ft8_tx_active = False
cw_tx_active = False
voice_tx_active = False
radio_power_state = None

FT8_SAMPLE_RATE = 12000
FT8_BUFFER_SAMPLES = FT8_SAMPLE_RATE * 16
ft8_rx_ring = np.zeros(FT8_BUFFER_SAMPLES, dtype=np.int16)
ft8_rx_idx = 0
ft8_rx_lock = threading.Lock()

ft8_engine_state = {
    "mode": "FT8",
    "slot_duration": 15.0,
    "my_call": "LU1AA",
    "my_grid": "FF46",
    "tx_armed": False,
    "tx_msg": "",
    "tx_freq": 1250,
    "tx_slot": "even",
    "tx_level": 50,
    "last_decoded": []
}

cw_engine_state = {
    "wpm": 20,
    "pitch": 700,
    "mode": "CW-U",
    "tx_active": False
}

def tx_audio_callback(outdata, frames, time_info, status):
    """
    Canal 0 (Left)  = Audio para DATA IN (Pin 1 Mini-DIN 6).
    Canal 1 (Right) = Tono de 1000 Hz para conmutación DAKY/PTT (Pin 3 Mini-DIN 6).
    """
    global tx_pcm_buffer, tx_audio_logged_time, tx_prebuffering, tone_phase, cw_tone_phase
    global ft8_tx_active, cw_tx_active, voice_tx_active
    bytes_needed = frames * 2

    mono = np.zeros(frames, dtype=np.int16)

    # 1. CANAL IZQUIERDO: Modulación hacia pin DATA IN
    if cw_tx_active and "DATA" in str(cw_engine_state.get("mode", "")).upper():
        # Modo DATA-U AFSK: Tono senoidal puro generado directamente
        pitch = int(cw_engine_state.get("pitch", 700))
        t_cw = (np.arange(frames) + cw_tone_phase) / actual_sample_rate
        cw_tone_phase = (cw_tone_phase + frames) % actual_sample_rate
        mono = (np.sin(2.0 * np.pi * pitch * t_cw) * 26000).astype(np.int16)
    elif cw_tx_active:
        # Modo CW-U Nativo: Silencio en DATA IN; el transmisor oscila por DAKY/Break-in
        cw_tone_phase = 0.0
        mono.fill(0)
    else:
        # FT8 digital o fonía desde WebAudio
        cw_tone_phase = 0.0
        with tx_lock:
            buffer_len = len(tx_pcm_buffer)

            if tx_prebuffering:
                if buffer_len >= 9600:
                    tx_prebuffering = False
                else:
                    buffer_len = 0

            if not tx_prebuffering and buffer_len >= bytes_needed:
                chunk = tx_pcm_buffer[:bytes_needed]
                del tx_pcm_buffer[:bytes_needed]
                mono = np.frombuffer(chunk, dtype=np.int16)
            elif not tx_prebuffering and buffer_len > 0:
                chunk = bytes(tx_pcm_buffer)
                tx_pcm_buffer.clear()
                avail = np.frombuffer(chunk, dtype=np.int16)
                mono[:len(avail)] = avail
                if not ft8_tx_active:
                    tx_prebuffering = True
            else:
                if not ft8_tx_active:
                    tx_prebuffering = True

    # 2. Asignar canal izquierdo
    stereo = np.zeros((frames, 2), dtype=np.int16)
    stereo[:, 0] = mono

    # 3. CANAL DERECHO: Tono de 1000 Hz para PTT/DAKY en Digirig
    t = (np.arange(frames) + tone_phase) / actual_sample_rate
    tone_phase = (tone_phase + frames) % actual_sample_rate

    if ft8_tx_active or voice_tx_active or cw_tx_active:
        stereo[:, 1] = (np.sin(2.0 * np.pi * 1000.0 * t) * 31500).astype(np.int16)
    else:
        stereo[:, 1] = 0

    outdata[:] = stereo

main_event_loop = None
input_device_id = None
output_device_id = None
actual_sample_rate = SAMPLE_RATE
worker_stop_event = threading.Event()
ft8_worker_thread = None
spots_worker_thread = None
serial_reader_task_handle = None

def rx_audio_callback(indata, frames, time_info, status):
    """Callback de recepción de audio desde el transceptor."""
    global main_event_loop, rx_client_queues, ft8_rx_ring, ft8_rx_idx
    if status:
        print(f"[AUDIO RX Status] {status}", file=sys.stderr)

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
    global radio_power_state
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
                        normalized_cmd = full_cmd.strip().upper()
                        if normalized_cmd == "PS1;":
                            radio_power_state = True
                        elif normalized_cmd == "PS0;":
                            radio_power_state = False
                            force_release_transmit_controls()
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
    if not cmd_str.endswith(";"):
        cmd_str += ";"
    if ser and ser.is_open:
        try:
            ser.write(cmd_str.encode("ascii"))
        except Exception as e:
            print(f"[CAT INTERNAL] Error enviando '{cmd_str}': {e}")

def set_hardware_ptt(active: bool):
    if active and radio_power_state is not True:
        return
    if ser and ser.is_open:
        try:
            ser.rts = active
        except Exception as e:
            print(f"[PTT HW] Error fijando RTS={active}: {e}")

    if cw_ser and cw_ser.is_open:
        try:
            cw_ser.rts = active
        except Exception:
            pass

    if active:
        send_cat_internal("TX2;")
    elif radio_power_state is True:
        send_cat_internal("TX0;")


def force_release_transmit_controls():
    global ft8_tx_active, cw_tx_active, voice_tx_active, tx_prebuffering
    ft8_tx_active = False
    cw_tx_active = False
    voice_tx_active = False
    for serial_device in (ser, cw_ser):
        if serial_device and serial_device.is_open:
            try:
                serial_device.rts = False
                serial_device.dtr = False
            except serial.SerialException as exc:
                print(f"[PTT HW] Error liberando líneas RTS/DTR: {exc}")
    with tx_lock:
        tx_pcm_buffer.clear()
        tx_prebuffering = True
    ft8_engine_state["tx_armed"] = False

def set_hardware_cw_key(active: bool, mode: str = "CW-U"):
    global cw_tx_active
    if active and radio_power_state is not True:
        return
    cw_tx_active = active
    mode_upper = str(mode).upper()
    is_pure_cw = ("CW" in mode_upper) and ("DATA" not in mode_upper)

    if ser and ser.is_open:
        try:
            ser.rts = active
            ser.dtr = active
        except Exception:
            pass

    if cw_ser and cw_ser.is_open:
        try:
            cw_ser.dtr = active
            cw_ser.rts = active
        except Exception:
            pass

    if is_pure_cw:
        if active:
            send_cat_internal("BI1;")
    elif radio_power_state is True:
        send_cat_internal("TX2;" if active else "TX0;")

def calibrate_ft8_snr(raw_val: float) -> int:
    val = int(round(raw_val))
    if val < 0:
        return max(-26, min(24, val))
    calibrated = val - 26
    return max(-26, min(24, calibrated))

def run_decode_ft8_file(wav_path):
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
        print(f"[FT8 DECODE] Error decodificando audio: {e}")
        return []

def synthesize_ft8_audio(message, audio_freq, out_wav_path):
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
            print(f"[FT8 GEN] Falló binario externo: {e}")

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

def ft8_cycle_worker(stop_event):
    global tx_pcm_buffer, tx_prebuffering, ft8_tx_active
    print("[FT8 WORKER] Sincronizador UTC activo.")
    last_tx_slot = -1
    last_rx_slot = -1

    while not stop_event.is_set():
        now = datetime.datetime.now(datetime.timezone.utc)
        sec = now.second + now.microsecond / 1_000_000.0
        slot_dur = ft8_engine_state["slot_duration"]
        slot_idx = int(sec // slot_dur)
        sec_in_slot = sec % slot_dur
        is_even = (slot_idx % 2 == 0)

        # 1. Disparador de Transmisión al inicio de la ranura
        if sec_in_slot < 0.75 and slot_idx != last_tx_slot:
            last_tx_slot = slot_idx
            armed = ft8_engine_state["tx_armed"]
            desired_slot = ft8_engine_state["tx_slot"]
            should_tx = False

            if armed and radio_power_state is True:
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
                            samples_48k = np.repeat(samples_12k, 4)
                            scaled_48k = (samples_48k * (ft8_engine_state["tx_level"] / 100.0)).astype(np.int16)

                        tx_duration = len(samples_48k) / actual_sample_rate
                        ft8_tx_active = True

                        set_hardware_ptt(True)

                        with tx_lock:
                            tx_pcm_buffer.clear()
                            tx_pcm_buffer.extend(scaled_48k.tobytes())
                            tx_prebuffering = False

                        stop_event.wait(tx_duration)
                        if not stop_event.is_set():
                            stop_event.wait(0.12)
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
                    print(f"[FT8 TX] No se pudo generar modulación para '{msg_to_send}'")

        # 2. Disparador de Decodificación Temprano:
        # En FT8 la modulación concluye a los 12.64s. Se dispara a los 12.8s para entregar decodes
        # al navegador antes de los 13.5s, permitiendo más de 1.5s de holgura para armar el siguiente slot.
        decode_trigger = 12.8
        capture_duration = 12.8
        if sec_in_slot >= decode_trigger and sec_in_slot < decode_trigger + 0.65 and slot_idx != last_rx_slot:
            last_rx_slot = slot_idx

            with ft8_rx_lock:
                current_idx = ft8_rx_idx
                needed_samples = int(FT8_SAMPLE_RATE * capture_duration)
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

        stop_event.wait(0.08)

def initialize_hardware():
    global ser, cw_ser, input_device_id, output_device_id, actual_sample_rate
    unmute_alsa_cards()

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
        print(f"[CAT] Puerto serie CAT conectado en {SERIAL_PORT} @ {BAUD_RATE} bps")
    except (OSError, serial.SerialException) as exc:
        ser = None
        print(f"[CAT] Advertencia en puerto {SERIAL_PORT}: {exc}")
        if not DEMO_FALLBACK:
            raise RuntimeError(f"No se pudo abrir el puerto CAT {SERIAL_PORT}") from exc

    if os.path.exists(CW_KEY_PORT) and CW_KEY_PORT != SERIAL_PORT:
        try:
            cw_ser = serial.Serial(
                port=CW_KEY_PORT,
                baudrate=BAUD_RATE,
                timeout=0,
                rtscts=False,
                dsrdtr=False
            )
            cw_ser.rts = False
            cw_ser.dtr = False
            print(f"[CW HW] Puerto serie secundario conectado en {CW_KEY_PORT}")
        except (OSError, serial.SerialException) as exc:
            cw_ser = None
            print(f"[CW HW] No se pudo abrir {CW_KEY_PORT}: {exc}")

    input_device_id = None
    output_device_id = None
    actual_sample_rate = SAMPLE_RATE
    try:
        devices = sd.query_devices()
        for idx, dev in enumerate(devices):
            name = dev["name"].lower()
            if AUDIO_CARD_KEYWORD in name:
                if dev["max_input_channels"] > 0 and input_device_id is None:
                    input_device_id = idx
                    actual_sample_rate = 48000
                    print(f"[AUDIO RX] Dispositivo #{idx} {dev['name']} @ {actual_sample_rate}Hz")
                if dev["max_output_channels"] > 0 and output_device_id is None:
                    output_device_id = idx
                    print(f"[AUDIO TX] Dispositivo #{idx} {dev['name']}")
    except Exception as exc:
        print(f"[AUDIO] Error enumerando dispositivos: {exc}")


def validate_auth_configuration():
    if not STATION_USERNAME or not STATION_PASSWORD:
        raise RuntimeError("Configure STATION_USERNAME y STATION_PASSWORD antes de iniciar.")
    if len(STATION_USERNAME) > 128 or ":" in STATION_USERNAME or STATION_USERNAME.strip() != STATION_USERNAME:
        raise RuntimeError("STATION_USERNAME admite hasta 128 caracteres, sin espacios externos ni dos puntos.")
    if len(STATION_PASSWORD) > 256:
        raise RuntimeError("STATION_PASSWORD no puede superar 256 caracteres.")
    if len(STATION_SESSION_SECRET.encode("utf-8")) < 32:
        raise RuntimeError("STATION_SESSION_SECRET debe tener al menos 32 bytes.")


def create_session_token(username):
    expires_at = int(time.time()) + SESSION_MAX_AGE
    nonce = secrets.token_urlsafe(24)
    payload = f"{username}:{expires_at}:{nonce}".encode("utf-8")
    signature = hmac.new(STATION_SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
    with SESSION_LOCK:
        now = int(time.time())
        for old_nonce, old_expiry in list(SESSION_NONCES.items()):
            if old_expiry <= now:
                SESSION_NONCES.pop(old_nonce, None)
        SESSION_NONCES[nonce] = expires_at
    return f"{base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')}.{base64.urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"


def is_valid_session(token):
    if not token:
        return False
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
        signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        expected = hmac.new(STATION_SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
        username, expires_at, nonce = payload.decode("utf-8").split(":", 2)
        is_signed_session = (
            hmac.compare_digest(signature, expected)
            and hmac.compare_digest(username.encode("utf-8"), STATION_USERNAME.encode("utf-8"))
            and int(expires_at) > int(time.time())
        )
        if not is_signed_session:
            return False
        with SESSION_LOCK:
            expiry = SESSION_NONCES.get(nonce)
            return expiry is not None and expiry > int(time.time())
    except (ValueError, UnicodeDecodeError):
        return False


def revoke_session(token):
    if not is_valid_session(token):
        return False
    try:
        encoded_payload, _ = token.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
        _, _, nonce = payload.decode("utf-8").split(":", 2)
    except (ValueError, UnicodeDecodeError):
        return False
    with SESSION_LOCK:
        return SESSION_NONCES.pop(nonce, None) is not None


def initialize_hardware_streams():
    global rx_stream, tx_stream
    if input_device_id is not None:
        try:
            rx_stream = sd.InputStream(
                device=input_device_id,
                channels=1,
                samplerate=actual_sample_rate,
                dtype="int16",
                blocksize=1920,
                callback=rx_audio_callback
            )
            rx_stream.start()
            print(f"[AUDIO RX] Entrada iniciada en #{input_device_id} @ {actual_sample_rate} Hz")
        except Exception as exc:
            rx_stream = None
            print(f"[AUDIO RX] Error iniciando InputStream: {exc}")

    if output_device_id is not None:
        try:
            tx_stream = sd.OutputStream(
                device=output_device_id,
                channels=2,
                samplerate=actual_sample_rate,
                dtype="int16",
                blocksize=480,
                callback=tx_audio_callback
            )
            tx_stream.start()
            print(f"[AUDIO TX] Modulación estéreo en #{output_device_id} @ {actual_sample_rate} Hz")
        except Exception as exc:
            tx_stream = None
            print(f"[AUDIO TX] Error iniciando OutputStream: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_event_loop, rx_stream, tx_stream, serial_reader_task_handle
    global ft8_worker_thread, spots_worker_thread, radio_power_state
    validate_auth_configuration()
    worker_stop_event.clear()
    radio_power_state = None
    initialize_hardware()
    main_event_loop = asyncio.get_running_loop()
    try:
        initialize_hardware_streams()
        serial_reader_task_handle = asyncio.create_task(serial_reader_task())
        ft8_worker_thread = threading.Thread(target=ft8_cycle_worker, args=(worker_stop_event,), daemon=True)
        spots_worker_thread = threading.Thread(target=background_spots_worker, args=(worker_stop_event,), daemon=True)
        ft8_worker_thread.start()
        spots_worker_thread.start()
        yield
    finally:
        worker_stop_event.set()
        set_hardware_ptt(False)
        if serial_reader_task_handle:
            serial_reader_task_handle.cancel()
            try:
                await serial_reader_task_handle
            except asyncio.CancelledError:
                pass
            serial_reader_task_handle = None
        for worker, timeout in ((ft8_worker_thread, 5), (spots_worker_thread, 20)):
            if worker and worker.is_alive():
                worker.join(timeout=timeout)
        ft8_worker_thread = None
        spots_worker_thread = None
        for stream in (rx_stream, tx_stream):
            if stream:
                try:
                    stream.stop()
                    stream.close()
                except Exception as exc:
                    print(f"[AUDIO] Error cerrando stream: {exc}")
        rx_stream = None
        tx_stream = None
        for serial_device in (ser, cw_ser):
            if serial_device and serial_device.is_open:
                try:
                    serial_device.close()
                except serial.SerialException as exc:
                    print(f"[SERIAL] Error cerrando puerto: {exc}")
        main_event_loop = None

app = FastAPI(title="Yaesu FT-891 WebCAT, Digital & CW Station Server", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="/app/static"), name="static")

cached_spots_data = {"spots": []}

def background_spots_worker(stop_event):
    global cached_spots_data
    while not stop_event.is_set():
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

        stop_event.wait(45)

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    svg_favicon = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect width="100" height="100" rx="20" fill="#ffb300"/>'
        '<text x="50" y="65" font-size="50" font-family="sans-serif" font-weight="bold" '
        'text-anchor="middle" fill="#0c0f12">891</text></svg>'
    )
    return Response(content=svg_favicon, media_type="image/svg+xml")

@app.post("/api/login")
async def login(request: Request, response: Response, credentials: LoginCredentials = Body(...)):
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with LOGIN_LOCK:
        failures = LOGIN_FAILURES.get(client_ip)
        if failures and failures["blocked_until"] > now:
            retry_after = max(1, int(failures["blocked_until"] - now))
            return Response(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                content="Too many login attempts"
            )
        if failures and failures["window_started"] + LOGIN_WINDOW_SECONDS <= now:
            LOGIN_FAILURES.pop(client_ip, None)

    username_matches = hmac.compare_digest(
        credentials.username.encode("utf-8"), STATION_USERNAME.encode("utf-8")
    )
    password_matches = hmac.compare_digest(
        credentials.password.encode("utf-8"), STATION_PASSWORD.encode("utf-8")
    )
    if not (username_matches and password_matches):
        with LOGIN_LOCK:
            failures = LOGIN_FAILURES.get(client_ip)
            if not failures or failures["window_started"] + LOGIN_WINDOW_SECONDS <= now:
                failures = {"count": 0, "window_started": now, "blocked_until": 0}
            failures["count"] += 1
            if failures["count"] >= LOGIN_FAILURE_LIMIT:
                failures["blocked_until"] = now + LOGIN_BLOCK_SECONDS
            LOGIN_FAILURES[client_ip] = failures
        return Response(status_code=401, content="Invalid username or password")
    with LOGIN_LOCK:
        LOGIN_FAILURES.pop(client_ip, None)

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_session_token(credentials.username),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/"
    )
    return {"authenticated": True}

@app.get("/api/session")
async def session_status(request: Request):
    return {"authenticated": is_valid_session(request.cookies.get(SESSION_COOKIE_NAME))}

@app.post("/api/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        revoke_session(token)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/"
    )
    return {"authenticated": False}

def websocket_session_is_valid(websocket: WebSocket):
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host", "").lower()
    if not origin or not host:
        return False
    parsed_origin = urlsplit(origin)
    return (
        parsed_origin.scheme in ("http", "https")
        and parsed_origin.netloc.lower() == host
        and is_valid_session(websocket.cookies.get(SESSION_COOKIE_NAME))
    )

async def authenticate_websocket(websocket: WebSocket):
    if not websocket_session_is_valid(websocket):
        await websocket.close(code=1008, reason="Authentication required")
        return False
    return True


def is_cat_command_allowed(command):
    if radio_power_state is True:
        return True
    return command in {"PS;", "PS1;", "PS0;"}


async def monitor_websocket_session(websocket: WebSocket, token: str):
    while websocket.client_state == WebSocketState.CONNECTED:
        await asyncio.sleep(1)
        if not is_valid_session(token):
            try:
                await websocket.close(code=1008, reason="Session expired or revoked")
            except RuntimeError:
                pass
            return


def track_websocket_session(websocket: WebSocket):
    token = websocket.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return
    task = asyncio.create_task(monitor_websocket_session(websocket, token))
    websocket_auth_tasks.add(task)
    task.add_done_callback(websocket_auth_tasks.discard)


@app.get("/api/spots")
async def get_spots_proxy(request: Request):
    if not is_valid_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return Response(status_code=401, content="Authentication required")
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
    global ft8_tx_active, voice_tx_active, cw_tx_active, radio_power_state
    if not await authenticate_websocket(websocket):
        return
    await websocket.accept()
    track_websocket_session(websocket)
    cat_clients.add(websocket)
    try:
        while True:
            cmd = await websocket.receive_text()
            cmd = cmd.strip()
            if not cmd.endswith(";"):
                cmd += ";"

            if not is_cat_command_allowed(cmd):
                await websocket.send_text("ERR RADIO_STANDBY;")
                continue
            if cmd == "PS0;":
                radio_power_state = False
                force_release_transmit_controls()
            elif cmd == "PS1;":
                radio_power_state = None

            if ft8_tx_active and cmd.startswith("TX0"):
                continue

            if cmd.startswith("TX1") or cmd.startswith("TX2"):
                voice_tx_active = True
                if ser and ser.is_open:
                    try:
                        ser.rts = True
                    except Exception:
                        pass
                if cw_ser and cw_ser.is_open:
                    try:
                        cw_ser.rts = True
                    except Exception:
                        pass
            elif cmd.startswith("TX0"):
                voice_tx_active = False
                if ser and ser.is_open and not (ft8_tx_active or cw_tx_active):
                    try:
                        ser.rts = False
                    except Exception:
                        pass
                if cw_ser and cw_ser.is_open and not (ft8_tx_active or cw_tx_active):
                    try:
                        cw_ser.rts = False
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
        if ser and ser.is_open and not (ft8_tx_active or cw_tx_active):
            try:
                ser.rts = False
            except Exception:
                pass
        if cw_ser and cw_ser.is_open and not (ft8_tx_active or cw_tx_active):
            try:
                cw_ser.rts = False
            except Exception:
                pass
        with tx_lock:
            tx_pcm_buffer.clear()
            tx_prebuffering = True

@app.websocket("/ws/audio/rx")
async def websocket_audio_rx_endpoint(websocket: WebSocket):
    if not await authenticate_websocket(websocket):
        return
    await websocket.accept()
    track_websocket_session(websocket)
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
    global ft8_tx_active, cw_tx_active
    if not await authenticate_websocket(websocket):
        return
    await websocket.accept()
    track_websocket_session(websocket)
    try:
        while True:
            data = await websocket.receive_bytes()
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

@app.websocket("/ws/cw")
async def websocket_cw_endpoint(websocket: WebSocket):
    global cw_tx_active
    if not await authenticate_websocket(websocket):
        return
    await websocket.accept()
    track_websocket_session(websocket)
    cw_clients.add(websocket)
    try:
        await websocket.send_text(json.dumps({
            "event": "state",
            "state": cw_engine_state
        }))
        while True:
            raw = await websocket.receive_text()
            try:
                cmd = json.loads(raw)
                action = cmd.get("action")
                if radio_power_state is not True:
                    if action == "key_up":
                        force_release_transmit_controls()
                        continue
                    await websocket.send_text(json.dumps({
                        "event": "error",
                        "message": "Encienda la radio antes de usar los controles CW."
                    }))
                    continue
                mode = cmd.get("mode", cw_engine_state.get("mode", "CW-U"))
                cw_engine_state["mode"] = mode

                if action == "key_down":
                    set_hardware_cw_key(True, mode)
                elif action == "key_up":
                    set_hardware_cw_key(False, mode)
                elif action == "enable_breakin":
                    send_cat_internal("BI1;")
                elif action == "set_speed":
                    wpm = max(5, min(60, int(cmd.get("wpm", 20))))
                    cw_engine_state["wpm"] = wpm
                    send_cat_internal(f"KS{str(wpm).zfill(3)};")
                elif action == "set_pitch":
                    pitch = max(300, min(1050, int(cmd.get("pitch", 700))))
                    cw_engine_state["pitch"] = pitch
                    send_cat_internal(f"KP{str(pitch).zfill(4)};")
                elif action == "send_ky_text":
                    text = cmd.get("text", "").upper()[:24]
                    if text:
                        send_cat_internal(f"KY {text};")
            except Exception as e:
                print(f"[WS CW] Error en comando: {e}")
    except WebSocketDisconnect:
        cw_clients.discard(websocket)
        set_hardware_cw_key(False, "CW-U")

@app.websocket("/ws/ft8")
async def websocket_ft8_endpoint(websocket: WebSocket):
    global ft8_tx_active
    if not await authenticate_websocket(websocket):
        return
    await websocket.accept()
    track_websocket_session(websocket)
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
                    if radio_power_state is not True:
                        await websocket.send_text(json.dumps({
                            "event": "error",
                            "message": "Encienda la radio antes de habilitar transmisión FT8."
                        }))
                        continue
                    ft8_engine_state["tx_armed"] = True
                    ft8_engine_state["tx_msg"] = cmd.get("msg", "")
                    ft8_engine_state["tx_freq"] = int(cmd.get("freq", 1250))
                    ft8_engine_state["tx_slot"] = cmd.get("slot", "even")
                    broadcast_ft8_event({"event": "tx_status", "state": ft8_engine_state})
                elif action == "set_tx_level":
                    try:
                        ft8_engine_state["tx_level"] = min(100, max(1, int(cmd.get("level", 50))))
                    except (TypeError, ValueError):
                        pass
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
                    if m != "FT8":
                        await websocket.send_text(json.dumps({
                            "event": "error",
                            "message": "FT4 no está soportado; el motor disponible solo transmite y decodifica FT8."
                        }))
                        continue
                    ft8_engine_state["mode"] = "FT8"
                    ft8_engine_state["slot_duration"] = 15.0
                    broadcast_ft8_event({"event": "tx_status", "state": ft8_engine_state})
            except Exception as e:
                print(f"[WS FT8] Error en comando: {e}")
    except WebSocketDisconnect:
        ft8_clients.discard(websocket)
        if not ft8_clients:
            ft8_engine_state["tx_armed"] = False
            ft8_tx_active = False
            set_hardware_ptt(False)
            with tx_lock:
                tx_pcm_buffer.clear()
                tx_prebuffering = True

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT, log_level="info")
