FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py storage.py sonarr.py .

# Port 5091 is only for Overseerr (on your LAN) to call the webhook - it is
# NOT published to the internet unless you explicitly forward it on your router.
EXPOSE 5091

CMD ["python", "-u", "bot.py"]
