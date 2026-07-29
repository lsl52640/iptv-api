#!/bin/bash

# Ensure running from the script directory
cd "$(dirname "$0")"

echo "⚡️ Starting IPTV-API Worker..."

# Check and run via Pipenv if available
if command -v pipenv &> /dev/null; then
    echo "📦 Running via Pipenv..."
    exec pipenv run dev
else
    # Fallback to direct Python command
    if command -v python3 &> /dev/null; then
        echo "🐍 Running via python3..."
        exec python3 main.py
    elif command -v python &> /dev/null; then
        echo "🐍 Running via python..."
        exec python main.py
    else
        echo "❌ Error: Python is not installed or not in PATH."
        exit 1
    fi
fi
