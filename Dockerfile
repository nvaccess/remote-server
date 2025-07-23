FROM ghcr.io/astral-sh/uv:0.8.2-python3.13-alpine

RUN addgroup -S remotegroup && adduser -S remoteuser -G remotegroup
USER remoteuser

# Increases performance, but slows down start-up time
ENV UV_COMPILE_BYTECODE=1
# Keeps Python from buffering stdout and stderr
# to avoid situations where the application crashes without emitting any logs due to buffering.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

# Copy over the server
COPY . /app

# Make sure everything is synched
RUN --mount=type=cache,target=/root/.cache/uv \ 
    uv sync --locked

# Run the server
CMD ["uv", "run", "server.py"]
