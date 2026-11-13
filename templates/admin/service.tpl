[Unit]
# Author: Martin Schläger (VNS)
Description=Running admin site [@SITE@]
After=network.target

[Service]
# Runs as root on purpose: the site creates, changes and deletes sites and opens shells. It is not sandboxed.
User=root
# The admin is the Harbor release in use; what it writes (its login, logs, libraries) stays in the site's app folder
WorkingDirectory=@HARBOR@/current/admin
Environment=HARBOR_ADMIN_DATA=@SITE_ROOT@/app
ExecStart=@HARBOR@/venv/bin/gunicorn --worker-class gthread --workers 1 --threads 32 --timeout 120 --bind 127.0.0.1:@PORT_HTTP@ app:app

RestartSec=10
Restart=always
# Keep running shells and scripts alive if only this service is restarted
KillMode=process

StandardOutput=journal
StandardError=journal
SyslogIdentifier=@SITE@

[Install]
WantedBy=multi-user.target
