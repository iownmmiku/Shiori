FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 SHIORI_DATA_DIR=/data
WORKDIR /app
COPY requirements-server.txt /app/
RUN pip install --no-cache-dir -r requirements-server.txt \
    && groupadd --gid 10001 shiori \
    && useradd --uid 10001 --gid 10001 --no-create-home shiori \
    && mkdir /data && chown 10001:10001 /data
COPY shiori /app/shiori
COPY qunyu /app/qunyu
COPY mikubot /app/mikubot
USER 10001:10001
VOLUME /data
EXPOSE 8765
ENTRYPOINT ["python", "-m", "shiori"]
CMD ["--no-browser", "--no-tray", "--host", "0.0.0.0", "--port", "8765", "--strict-port"]
