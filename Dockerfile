FROM ghcr.io/astral-sh/uv:0.8.2-python3.13-bookworm

ADD . /app
WORKDIR /app
RUN uv sync --locked
CMD ["uv", "run", "server.py"]
