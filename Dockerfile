FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    ffmpeg \
    fonts-liberation \
    imagemagick \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p downloads assets/pop_sounds assets/backgrounds assets/transition_sfx

ENV PYTHONUNBUFFERED=1
ENV FONT_PATH=/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf

CMD ["python", "bot.py"]
