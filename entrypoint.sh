#!/bin/bash

echo "🚀 Initializing volumes from defaults..."

# Sync all config files and subfolders
mkdir -p /app/config
cp -rn /defaults/config/. /app/config/

# Sync ALL protocol folders (eg4, growatt, victron, etc.)
# This ensures every folder in your image gets moved to the volume
mkdir -p /app/protocols
cp -rn /defaults/protocols/. /app/protocols/

# Ensure log and backlog folders exist for the app
mkdir -p /app/logs
mkdir -p /app/timescaledb_backlog

echo "✅ Sync complete. Launching gateway..."
exec "$@"