# Nea — agente de agendamiento para WhatsApp
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations

# Qué versión corre, para /health (app/version.py). Van después del
# `pip install` para no invalidar su caché en cada commit. El commit se
# guarda en NEA_BUILD_COMMIT y no en SOURCE_COMMIT a propósito: la
# plataforma puede poner SOURCE_COMMIT en el entorno al arrancar y lo
# pisaría, y /health ya no sabría si salió del build (verificado) o no.
#   docker build --build-arg NEA_VERSION=1.4.0 \
#     --build-arg SOURCE_COMMIT=$(git rev-parse HEAD) .
ARG NEA_VERSION=dev
ARG SOURCE_COMMIT=
ENV NEA_VERSION=${NEA_VERSION} \
    NEA_BUILD_COMMIT=${SOURCE_COMMIT}

EXPOSE 8000

# Las migraciones se aplican al arranque (lifespan de app/main.py).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4); sys.exit(0 if r.status==200 else 1)"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
