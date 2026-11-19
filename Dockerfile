FROM python:3.13-slim
WORKDIR /app
COPY *.py LICENSE ./
COPY static ./static
ENV MUSIC_HUB_CONFIG=/config/music-hub.cfg MUSIC_HUB_STATE=/state PYTHONUNBUFFERED=1
USER 65534:65534
ENTRYPOINT ["python3", "/app/music_hub.py"]
CMD ["serve"]
