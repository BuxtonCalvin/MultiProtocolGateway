FROM python:3.13-alpine AS base
FROM base AS builder
RUN mkdir /install
WORKDIR /install
COPY requirements.txt /requirements.txt
RUN pip install --prefix=/install -r /requirements.txt

FROM base
COPY --from=builder /install /usr/local
COPY protocol_gateway.py /app/
COPY config.cfg /app/
COPY defs/ /app/defs/
COPY classes /app/classes/
COPY protocols /app/protocols/
WORKDIR /app
CMD ["python3", "protocol_gateway.py"] 
