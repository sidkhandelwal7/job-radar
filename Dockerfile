# Job Radar — bare-metal is the documented path (README); this image exists so `docker compose up`
# works on a Linux box or NAS. It is NOT tested on the author's machine (no Docker there) — see
# DECISIONS.md D46. LLM enrichment needs the headless LLM CLI logged in to a subscription, which
# this image does not include: set RADAR_LLM_ENABLED=0 or mount the CLI's logged-in config directory.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 RADAR_ROOT=/app
WORKDIR /app
COPY pyproject.toml README.md ./
COPY radar ./radar
COPY config ./config
COPY web/dist ./web/dist
# ^ build the dashboard first (cd web && npm ci && npm run build); dist is not committed
RUN pip install -e ".[serve,lca]"
VOLUME ["/app/data"]
EXPOSE 8787
CMD ["radar", "serve", "--host", "0.0.0.0", "--port", "8787"]
