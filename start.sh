#!/bin/bash

# Ensure running from the script directory
cd "$(dirname "$0")"

# Force pipenv to create the virtual environment inside the project directory (.venv)
export PIPENV_VENV_IN_PROJECT=1

# Define raw python dependencies in case pipenv is not used
PACKAGES="requests bs4 tqdm async-timeout aiohttp opencc-python-reimplemented m3u8 pytz ipip-ipdb urllib3 psycopg[binary]"

# Function to install dependencies
install_dependencies() {
    echo "🔍 Checking and installing dependencies..."
    
    # 1. Try to use pipenv if possible
    if ! command -v pipenv &> /dev/null; then
        echo "📦 Pipenv not found. Trying to install pipenv..."
        if command -v python3 &> /dev/null; then
            python3 -m pip install --user pipenv 2>/dev/null || python3 -m pip install pipenv
            export PATH="$HOME/.local/bin:$PATH"
        elif command -v python &> /dev/null; then
            python -m pip install --user pipenv 2>/dev/null || python -m pip install pipenv
            export PATH="$HOME/.local/bin:$PATH"
        fi
    fi

    # 2. Install using pipenv
    if command -v pipenv &> /dev/null; then
        echo "📦 Pipenv is available. Installing packages via pipenv..."
        pipenv install --dev
        return 0
    fi

    # 3. Fallback to native python venv
    echo "⚠️ Pipenv is unavailable. Falling back to native python venv..."
    local PYTHON_CMD=""
    if command -v python3 &> /dev/null; then
        PYTHON_CMD="python3"
    elif command -v python &> /dev/null; then
        PYTHON_CMD="python"
    else
        echo "❌ Error: Python is not installed. Cannot create virtual environment."
        exit 1
    fi

    if [ ! -d ".venv" ]; then
        echo "🌐 Creating native virtual environment (.venv)..."
        $PYTHON_CMD -m venv .venv
    fi

    if [ -f ".venv/bin/pip" ]; then
        echo "📦 Installing packages into .venv..."
        .venv/bin/pip install --upgrade pip
        .venv/bin/pip install $PACKAGES
    else
        echo "❌ Error: Virtual environment pip not found."
        exit 1
    fi
    echo "✅ Dependencies installation completed."
}

# Check if the user explicitly requested installation
if [ "$1" = "install" ] || [ "$1" = "-i" ] || [ "$1" = "--install" ]; then
    install_dependencies
    exit 0
fi

# Check and run via Pipenv if available
if command -v pipenv &> /dev/null; then
    # Auto-install if virtual environment does not exist
    if ! pipenv --venv &> /dev/null; then
        echo "📦 Pipenv virtualenv not found. Auto-installing..."
        install_dependencies
    fi
    echo "⚡️ Starting IPTV-API Worker via Pipenv..."
    exec pipenv run dev
else
    # Fallback to native venv or direct python command
    # Check if native .venv exists
    if [ -d ".venv" ]; then
        # Check if dependencies are installed in .venv
        if ! .venv/bin/python -c "import requests, aiohttp" &> /dev/null; then
            echo "⚠️ Missing dependencies in .venv. Auto-installing..."
            install_dependencies
        fi
        echo "🐍 Starting IPTV-API Worker via native .venv..."
        exec .venv/bin/python main.py
    else
        # No venv folder, check if python is installed
        if command -v python3 &> /dev/null || command -v python &> /dev/null; then
            echo "📦 Virtual environment (.venv) not found. Auto-creating and installing..."
            install_dependencies
            
            # Run using the newly created .venv
            if [ -d ".venv" ]; then
                echo "🐍 Starting IPTV-API Worker via native .venv..."
                exec .venv/bin/python main.py
            fi
        else
            echo "❌ Error: Python is not installed or not in PATH."
            exit 1
        fi
    fi
fi

