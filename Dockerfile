FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py balance_worker.py service.py initial_catalog.json ./
COPY templates ./templates
COPY static ./static
RUN useradd --create-home appuser && mkdir -p /data/uploads && chown -R appuser:appuser /data
USER appuser
EXPOSE 5000
CMD ["python", "service.py"]
