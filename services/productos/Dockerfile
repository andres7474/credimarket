# Contexto de construcción: raíz del repositorio (ver docker-compose.yml)
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
COPY services/productos/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt
COPY comun /opt/comun
RUN pip install --no-deps /opt/comun

COPY services/productos/app ./app
RUN useradd --system --uid 10001 app
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
