FROM python:3.11-slim

# Instalar dependencias del sistema para ALSA, PortAudio, compilador y clonado de ft8_lib
RUN apt-get update && apt-get install -y --no-install-recommends \
    libasound2 \
    libasound2-plugins \
    alsa-utils \
    libportaudio2 \
    portaudio19-dev \
    gcc \
    g++ \
    make \
    git \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Compilar e instalar herramientas nativas de modulación y demodulación FT8 (ft8_lib de Karlis Goba)
RUN git clone --depth 1 https://github.com/kgoba/ft8_lib.git /tmp/ft8_lib \
    && cd /tmp/ft8_lib \
    && make -j$(nproc) \
    && cp gen_ft8 decode_ft8 /usr/local/bin/ \
    && chmod +x /usr/local/bin/gen_ft8 /usr/local/bin/decode_ft8 \
    && rm -rf /tmp/ft8_lib

WORKDIR /app

# Crear directorio estático
RUN mkdir -p /app/static

# Instalar dependencias de Python
RUN pip install --no-cache-dir \
    "fastapi>=0.110.0,<1.0.0" \
    "uvicorn[standard]>=0.28.0,<1.0.0" \
    "websockets>=12.0,<13.0" \
    "pyserial>=3.5,<4.0" \
    "sounddevice>=0.4.6,<1.0.0" \
    "numpy>=1.26.0,<2.0.0" \
    "scipy>=1.12.0,<2.0.0"

# Copiar el backend y la interfaz web
COPY server.py /app/server.py
COPY index.html /app/static/index.html

EXPOSE 8085

CMD ["python", "server.py"]
