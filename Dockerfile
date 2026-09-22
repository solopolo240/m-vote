FROM python:3.11-alpine

WORKDIR /app

RUN apk add --no-cache ffmpeg

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY with_out_filter.py config.py ./

CMD ["python", "with_out_filter.py"]
