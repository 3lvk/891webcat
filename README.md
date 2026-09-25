Yaesu FT-891 WebCAT Station & Remote Audio Server 📻⚡

Una estación web completa de control CAT bidireccional y transmisión/recepción de audio en tiempo real para el transceptor Yaesu FT-891, optimizada para operar desde computadoras de escritorio, tablets y dispositivos móviles (iOS y Android) a través de una interfaz de audio Digirig DR-891 en Linux/Docker.

🚀 Características Principales

1. Control CAT Completo y Respuesta en Tiempo Real

Control de Frecuencia y VFO: Sintonización fina por pasos configurables (10 Hz, 100 Hz, 1 kHz, 5 kHz, 10 kHz, 100 kHz, 1 MHz), salto rápido de dial (« 10, ‹ 5, ◀ 1, dial rotatorio táctil virtual y 1 ▶, 5 ›, 10 »), entrada directa de frecuencia en kHz e intercambio VFO-A ⇄ B (SV).

S-Meter Calibrado en dBm: Indicador analógico/digital fiel con segmentos dinámicos de señal (S-0 a S9+60 dBm).

Protección Activa de ROE / SWR: Lectura de SWR en transmisión con filtro contra picos espurios iniciales y corte automático de emergencia si la antena presenta una desadaptación crítica continua ($\ge 2.5$).

Modos de Operación: Conmutación rápida para LSB, USB, CW-U, CW-L, AM, FM, DATA-U y DATA-L.

Control de Potencia y Ganancia: Ajuste en vivo de potencia de RF (5 W a 100 W) y nivel de Squelch/RF Gain.

Detección Automática de Estado: Al iniciar, la interfaz permanece en modo Standby (pantalla apagada) hasta que la radio responde por CAT confirmando su encendido.

2. Control Total de Filtros DSP de FI (Hardware FT-891)

Acceso directo a las funciones avanzadas de procesamiento digital de señales de la radio desde el panel principal:

DNR (Digital Noise Reduction): Encendido/apagado y ajuste de algoritmo de nivel 1 a 15.

DNF (Digital Notch Filter): Supresión automática de múltiples tonos heterodinos y portadoras parásitas.

Manual Notch: Filtro de muesca manual con ajuste continuo de 100 Hz a 3.200 Hz.

Ancho de Filtro de FI (IF Width): Selector de presets (200 Hz, 500 Hz, 1.8 kHz, 2.4 kHz, 3.0 kHz, 4.0 kHz) y slider de ajuste fino por pasos de 50 Hz.

Desplazamiento de FI (IF Shift): Centrado y desplazamiento de pasabanda ($-1.2\text{ kHz}$ a $+1.2\text{ kHz}$) con botón de reinicio instantáneo.

Etapa de Entrada RF: Conmutación entre Preamplificador AMP1 / IPO (Intercept Point Optimization) y Atenuador de 12 dB (ATT).

3. Motor de Audio Web Bidireccional de Baja Latencia (48 kHz)

Recepción Ininterrumpida (RX) con PLL Adaptativo:

Implementa un Ring Buffer de memoria fija con Phase-Locked Loop (PLL) adaptativo en JavaScript.

Compensa las micro-desviaciones de frecuencia de reloj (clock drift) entre el chip de audio USB C-Media del Digirig y la tarjeta de sonido del cliente móvil o PC, eliminando cortes periódicos, micro-pausas y saturaciones del recolector de basura.

Transmisión de Voz Limpia (TX) y Enclavamiento por Hardware:

Canal Izquierdo (Left): Entrega modulación de voz pura y transparente al pin DATA IN (Pin 1 Mini-DIN) con limitador suave contra saturaciones digitales.

Canal Derecho (Right): Genera una señal continua de 1.000 Hz durante la transmisión que activa el detector de VOX/PTT por hardware del Digirig DR-891. Esto aterriza la línea física de PTT (Pin 3 Mini-DIN), obligando al FT-891 a silenciar el micrófono de palma frontal y conmutar a la entrada de audio trasera sin falsos cortes entre palabras.

Compatibilidad Móvil (iOS Safari & Android Chrome): Remuestreo lineal en vivo para dispositivos móviles que operan a 44.1 kHz, manejo de restricciones de bajo consumo de WebKit y captura de eventos de puntero (setPointerCapture) para evitar pérdidas de foco al sostener el PTT.

Modos de PTT: Selección entre modo MANTENER (Push-To-Talk tradicional o tecla Barra Espaciadora) y PULSAR ON/OFF (conmutador táctil ideal para transmisiones prolongadas en dispositivos móviles).

4. Agregador de Spots en Vivo & DX Cluster con QSY Inmediato

Panel lateral de telemetría de estaciones activas con sincronización periódica en segundo plano:

Fuentes Integradas:

POTA (Parks on the Air)

LLOTA (Lakes and Lagoons on the Air)

SOTA (Summits on the Air / SOTAwatch)

DX Cluster (DX Summit / Spothole)

Filtros Multi-Selección Interactivos: Permite seleccionar uno o varios programas, modos (SSB, CW, DATA, etc.) y bandas simultáneamente (por ejemplo, ver spots de 40m y 20m a la vez).

QSY de 1 Clic: Al pulsar sobre cualquier spot, la estación cambia automáticamente de frecuencia y modo (adaptando CW, SSB o DATA según la banda y actividad).

5. Diseño y Ergonomía Visual

Temas Claro y Oscuro: Contraste calibrado según estándares WCAG, asegurando lectura clara bajo luz solar directa o en cuarto de radio oscuro.

Diseño Responsivo: En teléfonos móviles, la navegación se divide en dos pestañas táctiles: 📻 Radio & Dial y 🎯 Spots & DX.

🏗️ Arquitectura del Sistema

┌─────────────────────────────────────────────────────────────┐
│                    Navegador Web (Cliente)                  │
│   • GUI Responsive (HTML5, TailwindCSS, Web Components)     │
│   • Web Audio API (48 kHz Resampler, PLL Ring Buffer, PTT)  │
└──────────────┬───────────────────────────────┬──────────────┘
               │ WebSocket (/ws/cat)           │ WebSockets (/ws/audio/rx & /ws/audio/tx)
               ▼                               ▼
┌─────────────────────────────────────────────────────────────┐
│              Servidor Docker (FastAPI / Python)             │
│   • Proxy CAT asíncrono serial (PySerial @ 38400 bps 8N2)   │
│   • Pipeline de audio ALSA estéreo de baja latencia         │
│   • Background Worker de Spots (POTA, LLOTA, SOTA, DX)      │
└──────────────┬───────────────────────────────┬──────────────┘
               │ Serial (/dev/ttyUSB0)         │ ALSA Audio PCM (Card "USB")
               ▼                               ▼
┌─────────────────────────────────────────────────────────────┐
│                 Módem Digirig DR-891 (Linux)                │
│   • UART CAT Bridge                                         │
│   • Chip de Sonido C-Media CM108/CM119                      │
│   • Control de PTT físico por hardware (Canal Derecho)      │
└──────────────────────────────┬──────────────────────────────┘
                               │ Cables CAT (Mini-DIN 8) & DATA (Mini-DIN 6)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                   Transceptor Yaesu FT-891                  │
└─────────────────────────────────────────────────────────────┘


🛠️ Requisitos de Hardware y Conexión

Transceptor: Yaesu FT-891.

Interfaz: Digirig DR-891 (o Digirig Mobile con cables para Yaesu).

Cableado:

Cable de Audio/PTT Mini-DIN de 6 pines (conector RTTY/DATA trasero del FT-891 al jack de audio del Digirig).

Cable CAT Mini-DIN de 8 pines (conector CAT/LINEAR trasero del FT-891 al jack serie del Digirig).

Cable USB entre el Digirig y la máquina host con Linux (Raspberry Pi, mini PC o servidor).

Configuración Recomendada en el Menú del FT-891

05-06 [CAT RATE]: 38400bps

05-07 [CAT TOT]: 100ms

05-08 [CAT RTS]: DISABLE

16-01 [SSB MIC SELECT]: REAR o MIC (gracias al tono de 1 kHz del canal derecho, el hardware conmuta automáticamente al pin DATA IN trasero al entrar en TX).

16-03 [DATA IN SELECT]: DATA

16-04 [DATA PTT SELECT]: DAKY

📦 Estructura del Proyecto

.
├── docker-compose.yml             # Orquestación de servicios y montaje de dispositivos
├── Dockerfile                     # Imagen optimizada en Python 3.11 con ALSA y PortAudio
├── FT891_WebCAT_Station.html      # Aplicación web frontend unificada
├── server.py                      # Servidor FastAPI, puente CAT y motor de audio
└── README.md                      # Documentación del proyecto


🚀 Despliegue con Docker Compose

1. Clonar el Repositorio

git clone https://github.com/tu-usuario/ft891-webcat.git
cd ft891-webcat


2. Permisos de Dispositivos en Linux

Asegúrate de que el usuario que ejecuta Docker pertenezca a los grupos dialout y audio:

sudo usermod -aG dialout,audio $USER


Verifica la asignación del puerto serie del Digirig (normalmente /dev/ttyUSB0) y de la tarjeta de sonido:

ls -l /dev/ttyUSB*
arecord -l


3. Configurar Parámetros (docker-compose.yml)

Revisa que las variables de entorno coincidan con tu sistema:

environment:
  - SERIAL_PORT=/dev/ttyUSB0
  - BAUD_RATE=38400
  - HTTP_PORT=8000
  - AUDIO_SAMPLE_RATE=48000
  - AUDIO_CARD_KEYWORD=USB
  - DEMO_FALLBACK=true


4. Construir y Levantar el Contenedor

docker compose up -d --build


5. Consultar Registros de Actividad

docker compose logs -f ft891_webcat


🌐 Acceso a la Interfaz Web

Abre tu navegador y entra en:

http://:8000


Nota para uso en Smartphones (iOS Safari / Android Chrome):
Para que los navegadores móviles otorguen acceso al micrófono, la conexión debe establecerse a través de un contexto seguro (HTTPS) o mediante localhost. Si accedes a través de una red local o dominio propio, te recomendamos usar un túnel seguro con certificado SSL (como Cloudflare Tunnel, Caddy o Nginx Reverse Proxy con Let's Encrypt).

⌨️ Atajos de Teclado (Escritorio)

Tecla / Acción

Función

Barra Espaciadora (Mantener)

Transmitir en vivo (PTT activo). Soltar para volver a RX.

Rueda del Ratón sobre el Dial

Sintonizar frecuencia hacia arriba o abajo según el paso activo.

Clic en Botón VFO-A ⇄ B

Alternar entre VFO activo y auxiliar.

Clic en Spot

Sintonizar inmediatamente la estación (QSY).

🛡️ Licencia y Créditos

Desarrollado para la comunidad de radioaficionados. Distribuido bajo la licencia MIT.

73 de la estación y ¡buenos comunicados DX! 📡✨
