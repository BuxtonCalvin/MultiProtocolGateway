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

# 1. Create the safety/backup directory
RUN mkdir -p /defaults/config /defaults/protocols

# 2. Copy EVERYTHING from your local folders into the safety folders
COPY config/ /defaults/config/
COPY protocols/ /defaults/protocols/

# 3. Copy app files
COPY protocol_gateway.py /app/
COPY defs/ /app/defs/
COPY classes /app/classes/

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "protocol_gateway.py"]