# Imagen del entrenamiento offline (perfil "entrenamiento" de docker compose). No es un servicio.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /repo
COPY ml/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt
COPY ml/pyproject.toml /opt/ml/pyproject.toml
COPY ml/credimarket_ml /opt/ml/credimarket_ml
RUN pip install --no-deps /opt/ml
CMD ["python", "ml/entrenar.py"]
