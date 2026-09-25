FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends libasound2 libasound2-plugins alsa-utils libportaudio2 portaudio19-dev gcc && rm -rf /var/lib/apt/lists/*
WORKDIR /app

RUN pip install --no-cache-dir "fastapi>=0.110.0,<1.0.0" "uvicorn[standard]>=0.28.0,<1.0.0" "websockets>=12.0,<13.0" "pyserial>=3.5,<4.0" "sounddevice>=0.4.6,<1.0.0" "numpy>=1.26.0,<2.0.0"



COPY server.py /app/server.py
#COPY index.html /app/static/index.html

EXPOSE 8085

CMD ["python", "server.py"]
