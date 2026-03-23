FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY assistant.py /app/assistant.py
COPY assistant_app /app/assistant_app

CMD ["python", "assistant.py", "daemon"]
