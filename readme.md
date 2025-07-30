# NVDA Remote Server Relay

This is a simple server used to relay connections for [NVDA Remote](https://nvdaremote.com)

## Basic Usage

This is currently only tested on Linux.

1. [Install uv](https://docs.astral.sh/uv/getting-started/installation/)
2. Obtain your TLS certificates.
    - By default, the server looks for the certificate at `./cert`, the private key at `./privkey`, and the chain of trust at `./chain`.
    - TBD - update documentation on this/remove this feature in favour of using the web server to handle TLS
3. run the server with `uv run server.py`.

## Development

This project uses [pre-commit](https://pre-commit.com/) hooks to help ensure code quality.
These run automatically on pull requests, however it is still recommended to set them up locally.

```sh
uvx pre-commit install
```

## Docker

To run in Docker, use `docker compose`:

```sh
docker compose up
```

This will expose the server on port 6837, the default.

The project is pre-configured to support [Compose Watch](https://docs.docker.com/compose/how-tos/file-watch) to make developing in Docker easier.
To enable Watch, either press `w` when `docker compose up` has completed building and starting the container; or run `docker compose watch` to avoid mixing the application and Compose Watch logs.

* Changes will be synchronised and the server restarted whenever the code changes.
* Changes will be synchronised and the image rebuilt whenever dependencies change.
