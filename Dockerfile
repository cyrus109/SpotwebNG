FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV SPOTWEBNEXT_SEED_DEMO_DATA=false
EXPOSE 7979
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7979"]
