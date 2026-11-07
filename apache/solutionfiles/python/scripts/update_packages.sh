#!/bin/bash
cd "$(dirname "$0")"
cd ..
clear
sudo -u www-data ./venv/bin/pip install --upgrade -r app/requirements.txt
sudo systemctl restart @SERVICE_NAME@
sudo systemctl status @SERVICE_NAME@
