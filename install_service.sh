#!/bin/bash

# Define the service name and file path
SERVICE_NAME="beatport-rinser"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
WORKING_DIR="/root/soulseek-similar-rinser"
EXEC_START="${WORKING_DIR}/rinse_all.sh"

echo "Creating systemd service file at ${SERVICE_FILE}..."

# Create the service file
cat <<EOF > $SERVICE_FILE
[Unit]
Description=Beatport Local Rinser Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$WORKING_DIR
ExecStart=$EXEC_START
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "Setting necessary permissions..."
chmod 644 $SERVICE_FILE
chmod +x $EXEC_START

echo "Reloading systemd daemon..."
systemctl daemon-reload

echo "Enabling the service to start on boot..."
systemctl enable ${SERVICE_NAME}.service

echo "Starting the service..."
systemctl start ${SERVICE_NAME}.service

echo "Installation complete!"
echo "--------------------------------------------------------"
echo "You can check the status of the service using:"
echo "  systemctl status ${SERVICE_NAME}.service"
echo "You can view the logs for the service using:"
echo "  journalctl -u ${SERVICE_NAME}.service -f"
echo "--------------------------------------------------------"
