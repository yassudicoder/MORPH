# One container: builds the React console, then runs the Python brain that also serves it.
# Works on Render, Koyeb, Railway, Fly.io, a VM, or your own laptop (docker build -t tower . && docker run -p 8000:8000 tower)

# --- stage 1: build the operator console -----------------------------------
FROM node:22-slim AS console
WORKDIR /app/console
COPY console/package.json console/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY console/ ./
RUN npm run build

# --- stage 2: Python runtime ------------------------------------------------
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TOWER_HOST=0.0.0.0 \
    TOWER_PORT=8000
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY tower/ tower/
COPY scenarios/ scenarios/
COPY --from=console /app/console/dist console/dist
EXPOSE 8000
# Hosts that assign their own port set PORT; config.py reads it when TOWER_PORT is not overridden.
CMD ["sh", "-c", "TOWER_PORT=${PORT:-$TOWER_PORT} exec python -m tower.server"]
