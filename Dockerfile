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

# Compilar ft8_lib desde una revisión fijada para obtener builds reproducibles.
ARG FT8_LIB_REF=9fec6ca39886edbf96f4f5e71edc76da5074e871
RUN git init /tmp/ft8_lib \
    && git -C /tmp/ft8_lib remote add origin https://github.com/kgoba/ft8_lib.git \
    && git -C /tmp/ft8_lib fetch --depth 1 origin "$FT8_LIB_REF" \
    && git -C /tmp/ft8_lib checkout --detach FETCH_HEAD \
    && cd /tmp/ft8_lib \
    && make -j$(nproc) \
    && cp gen_ft8 decode_ft8 /usr/local/bin/ \
    && chmod +x /usr/local/bin/gen_ft8 /usr/local/bin/decode_ft8 \
    && rm -rf /tmp/ft8_lib

WORKDIR /app

# Crear directorio estático
RUN mkdir -p /app/static

# Instalar dependencias Python directas y transitivas con versiones fijadas.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copiar el backend y la interfaz web
COPY server.py /app/server.py
COPY index.html /app/static/index.html
RUN python -m compileall -q /app/server.py

EXPOSE 8085

CMD ["python", "server.py"]
