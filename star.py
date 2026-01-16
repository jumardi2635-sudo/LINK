# Start service
sudo systemctl daemon-reload
sudo systemctl enable telegram-bot.service
sudo systemctl start telegram-bot.service

# Monitor logs
sudo journalctl -u telegram-bot.service -f

# Check status
sudo systemctl status telegram-bot.service

# Restart service
sudo systemctl restart telegram-bot.service