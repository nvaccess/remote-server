FROM ghcr.io/astral-sh/uv:0.8.2-python3.13-alpine

# We need to set this here even though it is default,
# because if watch is enabled, this part of the Dockerfile may be re-run as remoteUser
# which doesn't have the necessary permissions to update bind mounts.
USER root

# Increases performance, but slows down start-up time
ENV UV_COMPILE_BYTECODE=1
# Keeps Python from buffering stdout and stderr
# to avoid situations where the application crashes without emitting any logs due to buffering.
ENV PYTHONUNBUFFERED=1
# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Copy over the server
COPY . /app

# Make sure everything is synched
RUN --mount=type=cache,target=/root/.cache/uv \ 
    uv sync --locked

RUN addgroup -S remotegroup && adduser -S remoteuser -G remotegroup
USER remoteuser
EXPOSE 6837
# Run the server
CMD ["uv", "run", "server.py"]
