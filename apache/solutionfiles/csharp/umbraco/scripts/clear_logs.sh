#!/bin/bash
clear
systemctl stop @SERVICE_NAME@
sudo rm -rf @SITE_ROOT@/@PUBLISH_FOLDER@/umbraco/Logs
systemctl start @SERVICE_NAME@
systemctl status @SERVICE_NAME@
