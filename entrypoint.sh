#!/bin/bash

# --- 1. CONFIG FOLDER LOGIC (Preservation Mode) ---
# Goal: Create sub-folders and seed them, but don't touch user files.
CONFIG_SUBS=("backups" "data-db" "examples")

mkdir -p /app/config

for sub in "${CONFIG_SUBS[@]}"; do
    dest="/app/config/$sub"
    # Create the sub-folder if the user doesn't have it
    mkdir -p "$dest"
    
    # If the sub-folder is empty, seed it from defaults
    if [ -z "$(ls -A "$dest" 2>/dev/null)" ]; then
        echo "Seeding empty config sub-folder: $sub"
        cp -R /defaults/config/"$sub"/. "$dest/"
    fi
done

# Copy any top-level .cfg or .csv or .txt masks, but -n (no-clobber) ensures 
# we don't overwrite any files the user has customized.
cp -n /defaults/config/*.cfg /app/config/ 2>/dev/null
cp -n /defaults/config/*.csv /app/config/ 2>/dev/null
cp -n /defaults/config/*.txt /app/config/ 2>/dev/null


# --- 2. PROTOCOLS FOLDER LOGIC (Fresh/Sync Mode) ---
# Goal: Ensure the mounted folder matches the latest protocols in the image.
echo "Refreshing protocols from image..."
mkdir -p /app/protocols

# Using -u (update) only copies if the source is newer, 
# or you can just use cp -R to overwrite everything to keep it 'fresh'.
cp -R /defaults/protocols/. /app/protocols/


# --- 3. START THE APP ---
exec "$@"