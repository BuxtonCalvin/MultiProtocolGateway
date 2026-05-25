# Description: Builds the container image for the MultiProtocolGateway application.
# File: Dockerfile
#
# Copyright 2026 Kevin Burke
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://apache.org
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

FROM python:3.13-alpine AS base

FROM base AS builder
RUN mkdir /install
WORKDIR /install
COPY requirements.txt /requirements.txt
RUN pip install --prefix=/install -r /requirements.txt

FROM base
RUN apk add --no-cache bash
COPY --from=builder /install /usr/local
WORKDIR /app

# 1. Create the default "seed" directories
RUN mkdir -p /defaults/config /defaults/protocols

# 2. Copy local files into the "seed" directories
COPY ./config/ /defaults/config/
COPY ./protocols/ /defaults/protocols/

# 3. Copy app logic
COPY protocol_gateway.py /app/
COPY defs/ /app/defs/
COPY classes /app/classes/

# 4. Set up entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "protocol_gateway.py"]