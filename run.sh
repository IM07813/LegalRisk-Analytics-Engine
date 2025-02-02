#!/bin/bash
# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install requirements if needed
pip install -r requirements.txt

# Run the legal automation system
python3 legal_automation_system.py
