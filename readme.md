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

```sh
docker-compose up --build
```


This will expose the server on port 6837, the default.
You must create a folder called certificate along-side the docker-compose.yml which contains the certificate, private key, and root chain named as cert, key, and chain respectively.
