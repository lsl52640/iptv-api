#!/bin/sh
set -e

for file in /iptv-api-config/*; do
  filename=$(basename "$file")
  target_file="$APP_WORKDIR/config/$filename"
  if [ ! -e "$target_file" ]; then
    cp -r "$file" "$target_file"
  fi
done

. $APP_WORKDIR/.venv/bin/activate

exec python -u $APP_WORKDIR/main.py
