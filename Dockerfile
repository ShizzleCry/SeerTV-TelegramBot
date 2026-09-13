FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# No EXPOSE needed - the bot only makes outbound connections (polling)
CMD ["python", "-u", "bot.py"]
