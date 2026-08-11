# InfluxDB3 How to

Quick Start: Running InfluxDB 3 Core & Explorer UI.  This stack runs InfluxDB 3 Core (the time-series storage engine) alongside InfluxDB 3 Explorer (the administrative web UI).  There are two keys you must generate:  a Session Secret Key for the Explorer and an admin_token key for the Data Base.

## Deployment Instructions

### 1. Configure the Session Secret Key

 The Explorer UI requires a secure session secret to start its backend server. Generate a 32-byte hex string and save it to an .env file in the same directory as the compose file: bash# Generate the secret string on your terminal

```ini
openssl rand -hex 32
```

### 2. Save it to your environment file

echo "SESSION_SECRET_KEY=your_generated_hex_here" > .env

### 3. Generate the Admin Token File

InfluxDB 3 Core requires an initial administrator token to handle authentication across both HTTP write paths and Apache Arrow Flight gRPC query engines.  Run the stack once to initialize the storage volumes.

```ini
docker compose up -d
```

### 4. Force-generate the token asset directly onto the persistent storage path in the container

Generate the token file directly to the shared disk catalog, and restart the stack to apply it.

```ini
docker compose exec influxdb_v3-core influxdb3 create token --admin --name "admin_token" --offline --output-file /var/lib/influxdb3/data/admin_token.json

docker compose restart influxdb_v3-core
```

### 5. View and copy your generated token string for client configurations

docker compose exec influxdb_v3-core cat /var/lib/influxdb3/data/admin_token.json

### 6. Restart the core container to force it to read the new token file from disk

```ini
docker compose restart influxdb_v3-core
```

### 7.  Connect to the Web UI

Navigate to: <http://localhost:8886>

#### Server URL

Place the server URL in the input box:  <http://influxdb_v3-core:8181> (Uses internal Docker network routing so keep the original port as it does not conflict with anything)

#### API Token

Paste the alphanumeric token string copied from the admin_token.json file.

### 8. Start MPG

docker compose restart mpg

## Docker Compose Stack

```ini
 influxdb3:
   image: influxdb:3-core
   container_name: influxdb_v3-core
   restart: always
   user: root
   environment:
    # This env tells the engine process which token string matches the operational layer. Generate the admin_token per above.
     - INFLUXDB3_AUTH_TOKEN=apiv3_CiLCKXAAbiCBxSzqvjT09Ev1ZK5DBDFo1I-Z11ALvJm-oh9XqoFQmZ_99im0D4r2xkm6toTX3XzSspaKj1XB4A
   ports:
     - "8100:8181" # Native v3 API port
   volumes:
     - /dockercontainers/influxdb3/data:/var/lib/influxdb3/data:z
     - /dockercontainers/influxdb3/influxdb3_plugins:/var/lib/influxdb3/plugins
     #- /dockercontainers/influxdb3/certs:/etc/ssl/influxdb:ro    # Mounts your host certs folder into the container
   command:
     - influxdb3
     - serve
     - --node-id=node0
     - --object-store=file
     - --data-dir=/var/lib/influxdb3/data
     - --plugin-dir=/var/lib/influxdb3/plugins
     - --disable-authz=health,ping
     - --admin-token-file=/var/lib/influxdb3/data/admin_token.json
     #- --tls-cert=/etc/ssl/influxdb/server-cert.pem
     #- --tls-key=/etc/ssl/influxdb/server-key.key

 influxdb3-explorer:
   image: influxdata/influxdb3-ui:latest
   container_name: influxdb3-explorer
   user: root
   ports:
     - "8889:80"
     - "8887:443"
     - "8886:8080"
   environment:
     - SESSION_SECRET_KEY=${SESSION_SECRET_KEY:-}
   command: ["--mode=admin"]
   restart: unless-stopped
```
