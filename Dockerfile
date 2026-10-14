FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    READLATER_DATA=/data

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py parser.py ./
COPY static/ ./static/

VOLUME ["/data"]
EXPOSE 8765

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8765"]
