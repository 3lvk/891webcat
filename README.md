# Yaesu FT-891 WebCAT Station & Remote Digital Station 📻⚡

Una plataforma web integral para control CAT bidireccional, streaming de audio dúplex de baja latencia (48 kHz) y **cliente nativo de modos digitales FT8 y FT4** para el transceptor **Yaesu FT-891**, optimizada para operar desde navegadores modernos en PC, tablets y smartphones (iOS / Android) mediante una interfaz **Digirig DR-891** sobre Linux y Docker.

## 🌟 Características Principales

### 1. Control CAT Bidireccional Completo

* **Control de Frecuencia y VFO:** Sintonización por pasos configurables (100 Hz, 1 kHz, 5 kHz, 10 kHz), mando rotatorio virtual (knob interactivo para ratón y pantalla táctil), salto rápido (`«` $\times 10$, `▶` $+1$, `‹` $-1$, `»` $\times 10$) y entrada numérica directa en kHz.

* **Gestión de VFOs:** Intercambio rápido VFO-A ⇄ B (`SV;`), sincronización instantánea y seguimiento de split.

* **S-Meter Calibrado:** Escala dinámica precisa en unidades S y dBm (desde S-0 / $-127\text{ dBm}$ hasta S9+60 dB / $-13\text{ dBm}$).

* **Lectura de Potencia y ROE / SWR:** Monitor en vivo de potencia de salida y medidor de ondas estacionarias con alerta visual inmediata ante desadaptaciones críticas ($\ge 2.5$).

* **Filtros DSP de FI (Hardware del FT-891):**

  * **DNR (Digital Noise Reduction):** Encendido/apagado y ajuste de nivel 1 a 15 (`NR0;`, `RL0;`).

  * **DNF (Digital Notch Filter):** Supresión automática de heterodinos (`BC0;`).

  * **Manual Notch:** Filtro de muesca manual con ajuste continuo de 100 Hz a 3.200 Hz (`BP00;`, `BP01;`).

  * **Ancho de FI (IF Width):** Presets de 500 Hz, 1.8 kHz, 2.4 kHz y 3.0 kHz (`SH0;`).

  * **Etapa de Entrada RF:** Selector AMP1 / IPO (Intercept Point Optimization) y atenuador de 12 dB (`RA0;`).

* **Detección Automática de Estado / Standby:** La interfaz detecta si la radio está apagada o encendida, mostrando el panel LCD en standby y energizándolo al responder los sondeos CAT.

### 2. Cliente de Modos Digitales FT8 & FT4 Integrado 📟

* **Demodulación y Modulación Nativa en Linux:** Motor en C `ft8_lib` compilado dentro del contenedor Docker (`decode_ft8` y `gen_ft8`), sin depender de programas de escritorio pesados como WSJT-X.

* **Sincronización UTC Precisa:**

  * Control de ventanas de 15 segundos (**FT8**) y 7.5 segundos (**FT4**) con reloj UTC en vivo y barra de progreso de slot.

  * Determinación automática de ranura: **EVEN (:00 / :30)** y **ODD (:15 / :45)**.

* **Cálculo de SNR Calibrado a Estándar WSJT-X (2.500 Hz):**

  * Reportes de relación señal/ruido realistas (desde $-24\text{ dB}$ hasta $+15\text{ dB}$) con código de color dinámico y formateo obligatorio de dos dígitos con signo (`-16`, `-08`, `+02`).

* **Máquina de Estados de QSO Automatizada (FSM):**

  * Secuencia estándar completa:

    * **Tx 1:** `CQ  `

    * **Tx 2:** `  `

    * **Tx 3:** `  `

    * **Tx 4:** `  R`

    * **Tx 5:** `  RR73`

    * **Tx 6:** `  73`

  * **Auto-Secuencia:** Avanza automáticamente de fase al recibir las respuestas del corresponsal.

  * **Auto-CQ Continuo:** Al completar un QSO y enviar el `73` final, registra el contacto en el log, limpia los datos del corresponsal y vuelve a llamar CQ de forma ininterrumpida.

  * **Protección contra bucles:** Detiene el transmisor y desactiva el ciclo al terminar el intercambio.

* **Confirmación On-Air & Monitoreo de ALC:**

  * Visualización simultánea del Dial, offset de audio en Hz y frecuencia de emisión en antena.

  * Indicador de nivel de ALC en tiempo real para prevenir saturación e intermodulación en la banda.

* **Historial de QSOs y Exportación ADIF:** Registro local de contactos completados con exportación a archivo estándar `.adi` compatible con LoTW, QRZ y Cloudlog.

### 3. Motor de Audio Web Dúplex (48 kHz)

* **Recepción (RX) con Ring Buffer y PLL Adaptativo:**

  * Búfer circular elástico de memoria fija con bucle de enganche de fase (PLL) en JavaScript.

  * Compensa el desfase de reloj (*clock drift*) entre el chip de audio USB del Digirig y la placa de sonido del navegador web (PC o móvil), eliminando saltos, clics y latencia acumulada.

* **Transmisión de Fonía y Modulación Digital (TX):**

  * **PTT por Hardware:** Conmutación mediante la línea **RTS** del puerto serie y comando CAT `TX2;` (`DATA PTT`), garantizando que el audio ingrese por la entrada trasera `DATA IN` (Mini-DIN 6) y no por el micrófono frontal.

  * **Tono de Enclavamiento de 1.000 Hz:** Generado en el canal derecho estéreo para activar de forma sólida el detector de hardware del Digirig DR-891 sin tableteo de relés.

  * **Control de PTT Flexible:** Soporta pulsador en pantalla (táctil o ratón), modo alternado (toggle ON/OFF) y acceso directo con la **barra espaciadora**.

### 4. Agregador de Spots en Vivo & QSY Inteligente

* **Fuentes Integradas:** Descarga en segundo plano spots de **POTA**, **LLOTA**, **SOTA** y **DX Cluster**.

* **Filtros Multi-Criterio:** Filtrado instantáneo por programa, banda y modo (`SSB`, `CW`, `FT8`, `FT4`, `DATA`).

* **QSY Inteligente a Modos Digitales:** Al hacer clic sobre cualquier spot digital:

  1. El transceptor sintoniza la frecuencia de la banda y se conmuta a `DATA-U`.

  2. La interfaz cambia automáticamente a la pestaña de **FT8/FT4**.

  3. El indicativo y cuadrícula del activador quedan precargados en la secuencia de transmisión.

## 🏗️ Arquitectura del Sistema

```
┌─────────────────────────────────────────────────────────────┐
│                    Navegador Web (Cliente)                  │
│   • Interfaz Responsive (HTML5, TailwindCSS, CSS Variables) │
│   • Web Audio API (48 kHz PLL Ring Buffer & Mic Capture)   │
│   • FSM de FT8/FT4 (Ciclos UTC, auto-secuencia y log ADIF)  │
└──────────────┬───────────────────────────────┬──────────────┘
               │ WebSocket (/ws/cat)           │ WebSockets (/ws/audio/* & /ws/ft8)
               ▼                               ▼
┌─────────────────────────────────────────────────────────────┐
│            Servidor de Aplicación (FastAPI / Python)        │
│   • Proxy CAT asíncrono serial (PySerial @ 38.400 bps 8N2)  │
│   • Pipeline de audio ALSA estéreo dúplex (sounddevice)     │
│   • Sincronizador de ciclos UTC de 15s y 7.5s               │
│   • Motor nativo C: decode_ft8 (RX) y gen_ft8 (TX)          │
│   • Worker de spots (POTA, LLOTA, SOTA, DX Cluster)         │
└──────────────┬───────────────────────────────┬──────────────┘
               │ Serial (/dev/ttyUSB0)         │ ALSA Audio PCM (Card "USB")
               ▼                               ▼
┌─────────────────────────────────────────────────────────────┐
│                 Interfaz Módem Digirig DR-891               │
│   • UART CAT Bridge (Línea RTS para enclavamiento PTT)      │
│   • C-Media Audio Codec (Pin 1 DATA IN, Pin 2 GND, Pin 5 AF)│
│   • Detección de tono auxiliar en canal derecho             │
└──────────────────────────────┬──────────────────────────────┘
                               │ Cables CAT (Mini-DIN 8) & DATA (Mini-DIN 6)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                   Transceptor Yaesu FT-891                  │
└─────────────────────────────────────────────────────────────┘

```

## 🔌 Requisitos de Hardware y Conexión

1. **Transceptor:** Yaesu FT-891 (HF / 50 MHz).

2. **Interfaz:** Digirig DR-891 (o Digirig Mobile con jumpers y cables para Yaesu).

3. **Cableado:**

   * **Cable de Datos/Audio:** Conector Mini-DIN de 6 pines conectado al puerto **RTTY/DATA** trasero del FT-891 y al jack de audio del Digirig.

   * **Cable CAT:** Conector Mini-DIN de 8 pines conectado al puerto **CAT/LINEAR** trasero del FT-891 y al jack serie del Digirig.

   * **Cable USB:** Conexión desde el puerto USB del Digirig al equipo host (Raspberry Pi 4/5, mini PC o servidor Linux).

### Configuración Mandatoria en el Menú del FT-891

Presiona la tecla `[F]` de forma prolongada para ingresar a la configuración del menú del transceptor:

| Parámetro | Valor Requerido | Función | 
 | ----- | ----- | ----- | 
| **05-06 \[CAT RATE\]** | `38400bps` | Velocidad de comunicación serie CAT de alta tasa | 
| **05-07 \[CAT TOT\]** | `100ms` | Tiempo límite de transmisión CAT | 
| **05-08 \[CAT RTS\]** | `DISABLE` | Control de línea RTS | 
| **16-01 \[SSB MIC SELECT\]** | `REAR` o `MIC` | Selección de entrada para SSB | 
| **16-03 \[DATA IN SELECT\]** | `DATA` | Enruta la modulación digital por el puerto trasero Mini-DIN | 
| **16-04 \[DATA PTT SELECT\]** | `DAKY` | Permite la activación de PTT por hardware mediante el pin de datos | 
| **16-02 \[DATA GAIN\]** | `40` a `60` | Nivel de ganancia de audio digital (ajustar para evitar ALC excesivo) | 

## 🚀 Despliegue con Docker Compose

### 1. Clonar el Repositorio

```
git clone https://github.com/tu-usuario/ft891-webcat-station.git
cd ft891-webcat-station

```

### 2. Estructura de Archivos

Asegúrate de contar con la siguiente organización en el directorio del proyecto:

```
ft891-webcat-station/
├── docker-compose.yml
├── Dockerfile
├── server.py
├── FT891_WebCAT_Station.html
└── README.md

```

### 3. Iniciar el Servicio

Construye la imagen Docker (la cual compilará automáticamente `ft8_lib` de forma nativa) e inicia los contenedores:

```
docker compose up -d --build

```

### 4. Verificar Registros de Ejecución

```
docker compose logs -f ft891_webcat

```

## 🌐 Configuración de Proxy Inverso Nginx (HTTPS y WebSockets)

Para acceder de forma segura a través de Internet y permitir el uso del micrófono del navegador en dispositivos móviles, se debe emplear un proxy inverso con certificado SSL (Let's Encrypt / Certbot).

### Configuración con Solución al Bucle de Autenticación en Safari iOS

En navegadores WebKit (iPhone / iPad), el uso de `auth_basic` (htaccess) en la raíz del dominio provoca solicitudes continuas de usuario y contraseña porque Safari no envía cabeceras de autorización en el handshake de WebSockets.

La solución consiste en proteger la entrada web principal y deshabilitar explícitamente `auth_basic off;` en el bloque `/ws/`:

```
server {
    listen 80;
    server_name tu-dominio.com;
    return 301 https://\(host\)request_uri;
}

server {
    listen 443 ssl http2;
    server_name tu-dominio.com;

    ssl_certificate /etc/letsencrypt/live/tu-dominio.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/tu-dominio.com/privkey.pem;

    # Autenticación de acceso a la estación
    auth_basic "Estación Yaesu FT-891";
    auth_basic_user_file /etc/nginx/.htpasswd;

    # 1. Aplicación Web y Archivos Estáticos
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 2. WebSockets (CAT, Audio RX/TX y FT8)
    # CRÍTICO: Desactivar auth_basic aquí para evitar el bucle infinito en Safari iOS
    location /ws/ {
        auth_basic off;

        proxy_pass http://127.0.0.1:8000/ws/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Tiempos de espera extendidos para conexiones persistentes
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }

    location = /favicon.ico {
        auth_basic off;
        proxy_pass http://127.0.0.1:8000/favicon.ico;
    }
}

```

Recarga la configuración tras aplicar el archivo:

```
sudo nginx -t && sudo systemctl reload nginx

```

## 🛠️ Variables de Entorno

Puedes personalizar el comportamiento del sistema mediante variables en `docker-compose.yml`:

| Variable | Valor por Defecto | Descripción | 
 | ----- | ----- | ----- | 
| `SERIAL_PORT` | `/dev/ttyUSB0` | Ruta del dispositivo serie conectado al CAT del Digirig | 
| `BAUD_RATE` | `38400` | Velocidad de baudios serie (debe coincidir con el menú `05-06`) | 
| `HTTP_PORT` | `8000` | Puerto HTTP interno del servidor FastAPI | 
| `AUDIO_SAMPLE_RATE` | `48000` | Frecuencia de muestreo estándar de audio ALSA | 
| `AUDIO_CARD_KEYWORD` | `USB` | Subcadena para identificar el chip de sonido C-Media del Digirig | 
| `DEMO_FALLBACK` | `true` | Si es `true`, permite que la web abra en modo demo si la radio no responde | 

## 📻 Guía Rápida de Operación

### Operación en Fonía (SSB / AM / FM)

1. Conéctate a la URL de tu estación vía HTTPS.

2. Pulsa el botón **"Audio"** en la barra superior (se iluminará en verde con el icono 🔊).

3. Selecciona la banda deseada (`40m`, `20m`, etc.) y el modo (`LSB` o `USB`).

4. Para transmitir:

   * **Modo Mantener:** Presiona y mantén presionado el botón central **TRANSMITIR (PTT)** o la **barra espaciadora** de tu teclado.

   * **Modo Pulsar ON/OFF:** Pulsa el botón "MODO" debajo del PTT para alternar la transmisión con cada clic.

5. El vúmetro de modulación mostrará el nivel de audio capturado por tu micrófono.

### Operación en FT8 / FT4

1. Dirígete a la pestaña **📟 FT8/FT4** (o pulsa sobre cualquier spot digital de la lista).

2. Verifica que tu indicativo (**MI CALL**) y cuadrícula Maidenhead (**GRID**) estén configurados.

3. Observa la barra de ciclo UTC. En cada final de periodo (:14/:29/:44/:59s), aparecerán los mensajes decodificados.

4. **Para responder a una estación:** Haz clic sobre cualquier mensaje o llamado `CQ` de la lista. El sistema seleccionará `Tx 2`, calculará el turno opuesto (**EVEN** u **ODD**) y armará la transmisión para el segundo :00 o :15 correspondiente.

5. **Para llamar CQ de forma continua:** Marca la casilla **Auto-CQ Continuo** y pulsa **HABILITAR TX**. El sistema emitirá CQ, responderá automáticamente si alguien atiende el llamado y reanudará el CQ al concluir el QSO.

6. Al finalizar tus comunicados, haz clic en **Exportar ADIF** para descargar el log en formato compatible con tu libro de guardia preferido.

## 📄 Licencia y Reconocimientos

* Motor de codificación y decodificación FT8 basado en `ft8_lib` desarrollado por Karlis Goba (YL3JG).

* Distribuido bajo la licencia [MIT](LICENSE).

73 de la estación y ¡buena radio en las bandas! 📡⚡
