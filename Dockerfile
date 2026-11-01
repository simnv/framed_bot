FROM ghcr.io/astral-sh/uv:0.13.5 AS uv
FROM python:3.14.9-slim-trixie AS clip
# Keep these values equal to answers/clip_model.py; tests/test_deployment.py checks it.
ARG CLIP_URL=https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/6b7c523703ae26e39be4b03774966f8571c7f523/onnx/vision_model_fp16.onnx
ARG CLIP_SHA256=35c4e0fb0aeee527dcde1693520b214a34424a786babd530f35366bad5844efd
ARG CLIP_BYTES=176080659
COPY deploy/fetch-model.py /fetch-model.py
RUN python /fetch-model.py "$CLIP_URL" "$CLIP_SHA256" "$CLIP_BYTES" /opt/clip/vision_model.onnx

FROM python:3.14.9-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=0 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN groupadd --gid 10001 bot && useradd --uid 10001 --gid 10001 --no-create-home bot && mkdir /data && chown 10001:10001 /data
COPY --from=uv /uv /bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY --from=clip /opt/clip/ /opt/clip/
COPY config.py insight.py main.py reaction_delivery.py rich_tables.py safe_logging.py stats.py top_type.py ./
COPY models/ ./models/
COPY answers/ ./answers/
COPY deploy/healthcheck.py deploy/start-bot.py deploy/role-policy.sql ./
USER 10001:10001
CMD ["python", "start-bot.py"]
