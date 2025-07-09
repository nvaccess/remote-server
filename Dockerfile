# Copyright 2020 Christopher Toth
# 
# This file is part of NVDA Remote  Access Relay Server.
# 
# NVDA Remote Access Relay Server is free software: you can redistribute it and/or modify it under the terms
# of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.
# 
# NVDA Remote Access Relay Server is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero
# General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License along with NVDA Remote Access Relay
# Server. If not, see <https://www.gnu.org/licenses/>.

FROM ubuntu:focal

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get -y update && apt-get -y upgrade

RUN apt-get install -y -q tini curl python3 python3-pip

ENV PYTHONDONTWRITEBYTECODE 1

ENV PYTHONUNBUFFERED 1

WORKDIR /usr/src/app

ADD requirements.txt /usr/src/app

RUN pip3 install --no-cache-dir -r requirements.txt

ADD . /usr/src/app

RUN useradd remote

USER remote

EXPOSE 6837

ENTRYPOINT ["tini", "--", "python3", "server.py", "--certificate=certificate/cert", "--privkey=certificate/key", "--chain=certificate/chain"]
