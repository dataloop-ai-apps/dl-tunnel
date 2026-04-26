FROM hub.dataloop.ai/dtlpy-runner-images/cpu:python3.12_opencv

RUN pip install --no-cache-dir \
    "websockets>=12.0" \
    "PyJWT>=2.8.0"


ENV DL_TUNNEL_PORT=3000

# Build & push:
# docker build -t gcr.io/viewo-g/piper/agent/dl-tunnel:0.1.0 .
# docker push gcr.io/viewo-g/piper/agent/dl-tunnel:0.1.0
