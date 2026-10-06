# Yaesu FT-891 WebCAT Station, CW Workstation & FT8 📻⚡📟

Plataforma web integral para el control CAT bidireccional, streaming de audio dúplex (48 kHz), **telegrafía CW nativa** y **modo digital FT8** para el transceptor **Yaesu FT-891**, optimizada para navegadores modernos mediante una interfaz Digirig DR-891 sobre Linux y Docker. **FT4 no está implementado y no se puede transmitir ni decodificar con el motor actual.**

---

## 📑 Tabla de Contenidos

1. [Novedades y Últimas Actualizaciones](#-novedades-y-últimas-actualizaciones)
2. [Arquitectura del Sistema](#-arquitectura-del-sistema)
3. [Módulo de Telegrafía CW (Morse)](#-módulo-de-telegrafía-cw-morse)
   - [Doble Modo: CW-U Nativo (DAKY) vs. DATA-U (AFSK)](#doble-modo-cw-u-nativo-daky-vs-data-u-afsk)
   - [Decodificador DSP Biquad en Tiempo Real](#decodificador-dsp-biquad-en-tiempo-real)
   - [Keyer de Teclado, Macros y Paddle Virtual](#keyer-de-teclado-macros-y-paddle-virtual)
4. [Módulo Digital FT8](#-módulo-digital-ft8)
   - [Decodificación Temprana (Early Decoding)](#decodificación-temprana-early-decoding)
   - [Máquina de Estados de QSO (FSM) y Auto-CQ](#máquina-de-estados-de-qso-fsm-y-auto-cq)
   - [Calibración de SNR según Estándar WSJT-X](#calibración-de-snr-según-estándar-wsjt-x)
5. [Agregador de Spots y QSY Inteligente](#-agregador-de-spots-y-qsy-inteligente)
6. [Consola Radio & Mandos CAT](#-consola-radio--mandos-cat)
7. [Configuración Crítica en el Yaesu FT-891](#-configuración-crítica-en-el-yaesu-ft-891)
8. [Despliegue con Docker y Docker Compose](#-despliegue-con-docker-y-docker-compose)
9. [Licencia](#-licencia)

---

## 🌟 Novedades y Últimas Actualizaciones

- **Decodificación FT8 Temprana (*Early Decoding*):** La captura y decodificación de audio FT8 se dispara a los **12.8 segundos** de su ranura de 15 segundos.
- **Resolución de Bloqueos en Secuencia FT8 (`Tx3`):** Se rediseñó la precedencia de la máquina de estados. Al recibir el reporte de retorno de un corresponsal activo (`-10` o `R-10`), el sistema transiciona directamente a `Tx5` (`RR73`), sincroniza la ranura opuesta (`even`/`odd`) y mantiene la estación transmitiendo sin detenerse.
- **QSY Digital Inteligente para Spots FT8:** Al pulsar sobre un spot de FT8, el sistema:
  1. Sintoniza el VFO en el dial estándar de la banda correspondiente (`14.074.000 Hz`, `7.074.000 Hz`, etc.).
  2. Ajusta el offset de audio (`txFreq`) a la posición exacta anunciada por el spot.
  3. Fija el modo en `DATA-U` y actualiza el selector de banda digital.
  4. Aplica un bloqueo de sondeo serie (*CAT Polling Lock* de 850 ms) para evitar que las lecturas intermedias de `FA;` reviertan la frecuencia.
  5. Activa el modo de escucha pasiva: no transmite a ciegas, sino que aguarda a decodificar el `CQ` del activador para responderle en el slot inverso.
- **Transmisión de CW Nativo (`CW-U` / `CW-L`) por Hardware DAKY:** Conmutación sin latencia a través de las líneas físicas RTS/DTR en el pin 3 Mini-DIN (DAKY) y activación automática de Break-In (`BI1;`). Elimina el comando manual de MOX (`TX1;`) que bloqueaba el oscilador telegráfico interno del FT-891.
- **Decodificador CW DSP Biquad IIR:** Sustitución del detector Goertzel básico por un filtro paso banda IIR de orden 2 con ancho de ~140 Hz, histéresis Schmitt y temporizador de vaciado automático de 25 ms para evitar caracteres retenidos.

---

## 🏗 Arquitectura del Sistema

```
                        ┌───────────────────────────────────────────┐
                        │          Navegador Web (Cliente)          │
                        │  Tailwind CSS + HTML5 + Web Audio API    │
                        └──────────┬────────────────────▲───────────┘
                                   │                    │
              HTTP / WebSocket JSON│                    │WebSocket PCM 48kHz
             (/ws/cat, /ws/cw, etc)│                    │(Ring Buffer + PLL)
                                   ▼                    │
                        ┌───────────────────────────────┴───────────┐
                        │      Servidor Python FastAPI + Uvicorn     │
                        │       (Contenedor Docker en Linux)        │
                        ├─────────────────────┬─────────────────────┤
                        │   Worker UTC FT8    │ Worker Spots POTA/  │
                        │ decode_ft8 / gen_ft8│ SOTA/LLOTA/Cluster  │
                        └──────────┬──────────┴─────────▲───────────┘
                                   │                    │
                          /dev/ttyUSB0 (CAT)            │ ALSA Stream (48 kHz)
                          /dev/ttyUSB1 (CW Key)         │ In/Out Estéreo
                                   │                    │
                        ┌──────────▼────────────────────┴───────────┐
                        │       Interfaz Digirig DR-891 (USB)       │
                        │   CP2105 (Dual COM) + Tarjeta C-Media     │
                        └──────────┬────────────────────▲───────────┘
               Mini-DIN 8 (CAT)    │                    │ Mini-DIN 6 (DATA)
               Líneas RTS/DTR      │                    │ Audio In/Out + DAKY Pin 3
                                   ▼                    │
                        ┌───────────────────────────────┴───────────┐
                        │       Transceptor Yaesu FT-891 (HF/50MHz) │
                        └───────────────────────────────────────────┘
```

---

## ⚡ Módulo de Telegrafía CW (Morse)

### Doble Modo: CW-U Nativo (DAKY) vs. DATA-U (AFSK)

El transceptor Yaesu FT-891 maneja la telegrafía a través de dos caminos circuitales completamente separados:

| Modo en Interfaz | Modo Transceptor | Canal de Modulación | Línea de Disparo | Relé de Transmisión |
| :--- | :--- | :--- | :--- | :--- |
| **CW-U (Nativo Radio)** | `CW-U` (`MD03;`) | Oscilador interno de RF de la radio | Pin 3 Mini-DIN 6 (**DAKY**) a masa mediante transistor Digirig + tono 1000 Hz en canal derecho | **Break-In (`BI1;`)** automático. Sin comandos de MOX (`TX1;`) |
| **DATA-U (AFSK Audio)** | `DATA-U` (`MD0C;`) | Tono senoidal puro de 700 Hz por salida analógica *DATA IN* (Pin 1) | Línea PTT de datos CAT (`TX2;`) | Enclavamiento continuo por comando de datos CAT |

- **Ventaja de `CW-U (DAKY)`:** Ancho de banda de emisión mínimo, manipulado directo por el modulador nativo del equipo y posibilidad de escuchar entre signos (Full/Semi Break-In).
- **Ventaja de `DATA-U (AFSK)`:** Inmune a problemas de cableado físico de manipulación o configuraciones erróneas de llave en el menú del transceptor.

### Decodificador DSP Biquad en Tiempo Real

El flujo de audio PCM recibido a 48 kHz pasa por un procesador digital en JavaScript:
1. **Filtro Pasa-Banda Biquad IIR:** Calculado dinámicamente con factor $Q = 5.0$ y centrado en la frecuencia de Pitch seleccionada (500 a 800 Hz).
2. **Detector de Envolvente y Suelo de Ruido Adaptativo:** Mide la energía en bloques de 240 muestras (5 ms) y calibra de forma continua el umbral de silencio respecto a las señales pico.
3. **Disparador Schmitt con Histéresis:** Margen del $\pm 15\%$ para evitar que el ruido atmosférico genere falsos dits.
4. **Temporizador de Vaciado (*Symbol Flush* a 25 ms):**
   - Si la pausa supera $2.2 \times T_{\text{dit}}$, decodifica inmediatamente el símbolo Morse en la letra correspondiente.
   - Si la pausa supera $5.5 \times T_{\text{dit}}$, inserta un espacio de separación entre palabras.
5. **Seguimiento Automático de Cadencia (*Auto-WPM Tracking*):** Adapta la referencia temporal a la velocidad real de transmisión de la estación distante (8 a 45 WPM).

### Keyer de Teclado, Macros y Paddle Virtual

- **Manipulador de Texto Anticipado (*Type-Ahead*):** Escribe el mensaje completo y presiona `Enter`; el sistema generará los elementos con cadencia estándar PARIS y sidetone local de retorno suave mediante Web Audio API (rampas de 5 ms para prevenir clics acústicos).
- **Parada de Emergencia:** Presiona `Escape` o el botón **DETENER (ESC)** para cortar la emisión instantáneamente.
- **Botonera de Macros Rápidos:**
  - `CQ CQ`: `CQ CQ DE [MYCALL] [MYCALL] K`
  - `MI CALL`: `DE [MYCALL] K`
  - `5NN TU`: `[DXCALL] 5NN TU`
  - `73 GL`: `73 ES GL SK`
  - `AGN?` / `QRZ?`
- **Manipulador Manual Táctil / Ratón (*Paddle*):** Botón táctil ergonómico con enclavamiento sin retardo para emitir manualmente desde pantallas táctiles o ratón.

---

## 📟 Módulo Digital FT8

El motor digital implementado utiliza `ft8_lib` para FT8. FT4 puede aparecer en los spots externos, pero el cliente lo marca como no disponible y no permite sintonizarlo desde ese spot. No se debe transmitir ni asumir decodificación FT4.

### Decodificación Temprana (Early Decoding)

En la operación clásica de FT8, la transmisión ocupa 12.64 segundos dentro de la ranura de 15 segundos. El worker de `server.py` implementa sincronización temprana:
- **Disparo de decodificación a los 12.8 s:** Al concluir la emisión, captura el bloque acumulado en el ring buffer de 12.000 Hz y ejecuta `decode_ft8`.
- **Entrega de decodes a los ~13.3 s:** El navegador recibe las respuestas antes del segundo 13.5 s, permitiendo a la máquina de estados evaluar el mensaje y enviar la orden `arm_tx` con **más de 1.5 segundos de antelación** al inicio del siguiente ciclo.

### Máquina de Estados de QSO (FSM) y Auto-CQ

La máquina de estados finita gestiona el intercambio completo estándar:

$$\text{Tx1 (CQ)} \longrightarrow \text{Tx2 (Grid)} \longrightarrow \text{Tx3 (Reporte)} \longrightarrow \text{Tx4 (R+Reporte)} \longrightarrow \text{Tx5 (RR73)} \longrightarrow \text{Tx6 (73)}$$

1. **Prioridad 1 (QSO Activo):** Analiza expresiones regulares para tokens de reporte (`[+-]\d{2}`, `R[+-]\d{2}`) y confirmaciones (`RR73`, `73`). Si tu estación envió `Tx3` y recibe el reporte del corresponsal, **salta de inmediato a `Tx5` (`RR73`)**, sincroniza la paridad de la ranura contraria y deja la radio armada.
2. **Prioridad 2 (Spot en Espera):** Al sintonizar un activador desde los spots, precarga sus datos pero mantiene el transmisor en espera. En cuanto el activador emite un `CQ`, responde de inmediato en `Tx2`.
3. **Prioridad 3 (Respuesta a Nuestro CQ):** Detecta a un nuevo corresponsal que contesta a nuestra llamada, configura su indicativo/grid y avanza a `Tx3`.
4. **Auto-CQ Continuo:** Al emitir `RR73` o `73` y concluir el QSO, registra el contacto en el log ADIF, reinicia el estado y vuelve a llamar `CQ` de forma automática.

### Calibración de SNR según Estándar WSJT-X

El sistema convierte las puntuaciones internas de detección de `ft8_lib` a la escala internacional WSJT-X referenciada a un ancho de banda de 2.500 Hz:

$$\text{SNR}_{\text{WSJT-X}} = \text{SNR}_{\text{bin}} - 10 \log_{10}\left(\frac{2500}{6.25}\right) \approx \text{SNR}_{\text{bin}} - 26\text{ dB}$$

Los reportes se visualizan en rangos realistas (de $-24\text{ dB}$ a $+15\text{ dB}$) con formato estricto de signo y dos dígitos (`-08`, `+03`, `-14`).

---

## 🎯 Agregador de Spots y QSY Inteligente

El servidor sondea en segundo plano las APIs de **POTA**, **SOTA**, **LLOTA** y **DX Cluster**, unificando la lista en `/api/spots`.

### Lógica de Sintonización Automática por Modo

- **Si el spot es FT8:**
  - Sintoniza la frecuencia base de la banda en el VFO (`14.074.000 Hz`, `7.074.000 Hz`, etc.).
  - Configura el modo en `DATA-U` (`MD0C;`).
  - Asigna el delta de frecuencia como audio offset (`Ft8State.txFreq`).
  - Actualiza el selector de banda digital y cambia la vista a la consola digital.
  - Bloquea el sondeo CAT (`qsyLockUntil`) durante 850 ms para evitar que lecturas intermedias de `FA;` desfasadas restauren la frecuencia anterior.
- **Si el spot es CW:**
  - Sintoniza la frecuencia exacta de RF en el VFO.
  - Configura el modo en `CW-U` (`MD03;`) y activa el Break-In (`BI1;`).
  - Cambia la vista a la consola CW y precarga el indicativo en el manipulador.
- **Si el spot es Fonía (SSB):**
  - Sintoniza la frecuencia y conmuta a `LSB` (en bandas inferiores a 10 MHz) o `USB` (en bandas superiores a 10 MHz).

---

## 📻 Consola Radio & Mandos CAT

La cabecera fija comparte los controles de banda y modo entre las vistas Radio, CW y Digital. Al cambiar banda, Radio aplica su frecuencia/modo predeterminados; Digital sintoniza el dial estándar FT8 en DATA-U; CW conserva CW-U, CW-L o DATA-U. Al entrar en Digital se selecciona DATA-U y al entrar en CW se conserva un modo compatible o se selecciona CW-U; al volver a Radio desde un modo digital/CW se aplica el modo predeterminado de la banda. La cabecera también contiene potencia RF y ganancia de micrófono del audio del navegador (no es un ajuste CAT de ganancia MIC del transceptor).

Con CAT desconectado o la radio en standby, los controles de operación quedan deshabilitados. En ese estado solo se permite consultar el estado (`PS;`) o encender/apagar (`PS1;` / `PS0;`); la interfaz habilita los demás controles al recibir el estado de encendido desde la radio.

- **Dial Rotatorio Virtual:** Knob interactivo con soporte de rueda de ratón, gestos táctiles e inercia.
- **Selector de Paso de Sintonía:** Pasos rápidos de 100 Hz, 1 kHz, 5 kHz y 10 kHz, más entrada directa de frecuencia numérica en kHz.
- **S-Meter Dinámico Calibrado:** Escala continua desde S-0 ($-127\text{ dBm}$) hasta S9+60 dB ($-13\text{ dBm}$).
- **Lectura de Potencia RF y SWR:** Detección de ROE con alarma visual ante valores críticos ($\ge 2.5:1$).
- **Filtros DSP de FI del FT-891:**
  - `DNR` (Reducción Digital de Ruido): On/Off y nivel de 1 a 15 (`NR0;`, `RL0;`).
  - `DNF` (Filtro Notch Automático): Supresión de heterodinos (`BC0;`).
  - `NOTCH` (Filtro Notch Manual): Ajuste continuo de 100 Hz a 3.200 Hz (`BP00;`, `BP01;`).
  - `WIDTH`: Ancho de banda de FI seleccionable (500 Hz, 1.8 kHz, 2.4 kHz, 3.0 kHz).
  - `IPO` / `ATT`: Preamplificador AMP1, entrada directa IPO y atenuador de 12 dB.

---

## ⚙️ Configuración Crítica en el Yaesu FT-891

Para que el transceptor responda a la manipulación por hardware y conmutación de audio con el Digirig DR-891, verifica los siguientes menús en el equipo:

| Menú | Parámetro | Valor Requerido | Descripción |
| :--- | :--- | :--- | :--- |
| **05-06** | `CAT RATE` | **`38400bps`** | Velocidad de comunicación serie CAT. |
| **05-07** | `CAT TOT` | `100ms` | Timeout de comandos CAT. |
| **05-08** | `CAT RTS` | **`ENABLE`** | Habilita el control de flujo por hardware. |
| **07-08** | `CW BK-IN TYPE` | **`SEMI`** o **`FULL`** | Permite que el transmisor emita al recibir pulsos de llave. |
| **07-12** | `PC KEYING` | **`DAKY`** | Asigna la línea de manipulación de PC al Pin 3 del puerto Mini-DIN. |
| **07-13** | `QSK DELAY TIME` | `25ms` - `30ms` | Retardo de recuperación de relé entre signos. |
| **08-01** | `DATA MODE` | `OTHERS` | Configuración para modos digitales en banda base. |
| **08-09** | `DATA IN SELECT` | **`REAR`** | Selecciona la entrada de audio del conector Mini-DIN trasero. |
| **08-10** | `DATA PTT SELECT` | **`DAKY`** | Permite que la línea DAKY active la transmisión de datos. |
| **08-12** | `DATA BFO` | `USB` | Mantiene la banda lateral superior en modos de datos. |
| **Panel** | `BK-IN` (Botón F) | **`ON`** | **Obligatorio para CW-U.** Si está en `OFF`, solo sonará el sidetone interno y no saldrá RF. |

---

## 🐳 Despliegue con Docker y Docker Compose

### 1. Requisitos de Hardware y Sistema
- Transceptor Yaesu FT-891.
- Interfaz Digirig DR-891 conectada con su cable Mini-DIN 8 (CAT) y Mini-DIN 6 (DATA).
- Servidor Linux (PC, Raspberry Pi 4/5 o similar) con Docker y Docker Compose instalados.

### 2. Archivo `docker-compose.yml`

El archivo `docker-compose.yml` versionado es la configuración activa y requiere credenciales. No publiques el servicio directamente en Internet. Para acceso remoto, utiliza una VPN o un proxy HTTPS de confianza.

El `Dockerfile` instala las versiones fijadas en `requirements.txt` y compila `ft8_lib` desde el commit fijado en `FT8_LIB_REF`.

Prepara un archivo local de entorno (no se versiona) y sustituye todos los valores de ejemplo:

```bash
cp .env.example .env
chmod 600 .env
openssl rand -hex 32
```

Usa el resultado de `openssl rand -hex 32` como `STATION_SESSION_SECRET` en `.env` y configura una contraseña larga y única. `COOKIE_SECURE=false` solo es adecuado para acceso HTTP dentro de una red local/VPN confiable. Si sirves la aplicación por HTTPS/WSS, cambia a `COOKIE_SECURE=true`. Las credenciales se pasan al contenedor mediante variables de entorno; limita también quién tiene acceso al daemon Docker.

### 3. Comandos de Puesta en Marcha

```bash
# Desde el directorio del proyecto, después de crear .env
docker compose config --quiet

# Construir y levantar el contenedor en segundo plano
docker compose up -d --build

# Monitorear registros de ejecución (CAT, Audio y FT8)
docker compose logs -f
```

Una vez levantado, ingresa desde cualquier navegador web en la red local:
```
http://<IP-DE-TU-SERVIDOR>:8085
```

La aplicación solicitará usuario y contraseña antes de conectar CAT, audio, CW o FT8. La sesión expira a las 12 horas; los inicios fallidos se limitan temporalmente. Para apagarla, usa `docker compose down`.

### Validaciones

Ejecuta las pruebas dentro de una imagen Docker ya construida (no en el host):

```bash
docker run --rm --env-file .env \
  -v "$PWD/tests:/tests:ro" \
  --entrypoint python 891webcat:local \
  -m unittest discover -s /tests
```

---

## 📄 Licencia

Este proyecto está distribuido bajo la **Licencia MIT**. Consulta el archivo `LICENSE`.
