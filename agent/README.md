# Seny Local Agent

The Seny Local Agent syncs data from your local machine (like browser history) to your Seny personal assistant in the cloud.

## Features

- **Browser History Sync**: Syncs your Chrome browsing history to Seny
- **Privacy-First**: You control what gets synced; all data is encrypted in transit
- **Cross-Platform**: Works on macOS, Windows, and Linux

## Requirements

- Python 3.8 or later
- Chrome browser (for browser history sync)
- A Seny account

## Installation

### 1. Download the Agent

Download `seny_agent.py` and `requirements.txt` to your computer:

```bash
# Create a directory for the agent
mkdir ~/seny-agent
cd ~/seny-agent

# Download the files (or copy from this directory)
# The files should be: seny_agent.py and requirements.txt
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

Or simply:

```bash
pip install requests
```

### 3. Run Setup

```bash
python seny_agent.py --setup
```

This will:
1. Ask for your Seny server URL (use the default)
2. Ask for your API token (get this from Seny Settings)
3. Verify your connection
4. Offer to do an initial sync

## Usage

### One-Time Sync

Sync your data once:

```bash
python seny_agent.py --sync
```

### Continuous Sync (Daemon Mode)

Keep the agent running to sync automatically every 15 minutes:

```bash
python seny_agent.py --daemon
```

Press `Ctrl+C` to stop.

### Check Status

See your sync status and configuration:

```bash
python seny_agent.py --status
```

## Configuration

Configuration is stored in `~/.seny/config.json`:

```json
{
  "seny_url": "http://localhost:8000",
  "api_token": "your_jwt_token_here",
  "sync_interval_minutes": 15,
  "browser_history": {
    "enabled": true,
    "exclude_domains": ["localhost", "127.0.0.1", "0.0.0.0"]
  }
}
```

### Configuration Options

| Option | Description | Default |
|--------|-------------|---------|
| `seny_url` | Your Seny server URL | Production URL |
| `api_token` | Your JWT authentication token | (required) |
| `sync_interval_minutes` | How often to sync in daemon mode | 15 |
| `browser_history.enabled` | Enable/disable browser history sync | true |
| `browser_history.exclude_domains` | Domains to exclude from sync | localhost, etc. |

### Adding Exclusions

To exclude certain domains from syncing, edit the config file:

```json
{
  "browser_history": {
    "exclude_domains": [
      "localhost",
      "127.0.0.1",
      "private-site.com",
      "banking-site.com"
    ]
  }
}
```

## Running on Startup

### macOS (launchd)

1. Create a launch agent file:

```bash
mkdir -p ~/Library/LaunchAgents
```

2. Create `~/Library/LaunchAgents/com.seny.agent.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.seny.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/YOUR_USERNAME/seny-agent/seny_agent.py</string>
        <string>--daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/seny-agent.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/seny-agent.err</string>
</dict>
</plist>
```

3. Replace `YOUR_USERNAME` with your actual username.

4. Load the agent:

```bash
launchctl load ~/Library/LaunchAgents/com.seny.agent.plist
```

5. To stop:

```bash
launchctl unload ~/Library/LaunchAgents/com.seny.agent.plist
```

### Windows (Task Scheduler)

1. Open Task Scheduler
2. Click "Create Basic Task"
3. Name: "Seny Agent"
4. Trigger: "When the computer starts"
5. Action: "Start a program"
6. Program: `python` (or full path to python.exe)
7. Arguments: `C:\path\to\seny_agent.py --daemon`
8. Finish

### Linux (systemd)

1. Create `/etc/systemd/user/seny-agent.service`:

```ini
[Unit]
Description=Seny Local Agent
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/YOUR_USERNAME/seny-agent/seny_agent.py --daemon
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

2. Replace `YOUR_USERNAME` with your actual username.

3. Enable and start:

```bash
systemctl --user enable seny-agent
systemctl --user start seny-agent
```

## Getting Your API Token

1. Go to your Seny instance (e.g., http://localhost:8000)
2. Log in to your account
3. Go to **Settings**
4. Under **API Token**, copy your token

**Note:** Keep your API token secret. It provides full access to your Seny account.

## Troubleshooting

### "Could not copy Chrome history"

Chrome locks its history database while running. Solutions:
- Close Chrome completely, then run the sync
- The agent uses file copying to work around this, but it may fail occasionally

### "Invalid or expired API token"

Your token may have expired. Get a new token from Seny Settings and run setup again:

```bash
python seny_agent.py --setup
```

### "Chrome history not found"

The agent looks for Chrome in the default location. If you're using:
- A different browser profile (not "Default")
- Chromium instead of Chrome
- A portable installation

You may need to modify the `get_chrome_history_path()` function in the script.

### Checking Logs

When running as a daemon/service:
- macOS: Check `/tmp/seny-agent.log`
- Windows: Check Task Scheduler history
- Linux: Run `journalctl --user -u seny-agent`

## Privacy

- **What's synced**: URLs, page titles, visit times
- **What's NOT synced**: Page content, cookies, passwords, form data
- **Exclusions**: localhost, private IPs, and any domains you configure
- **Security**: All data is transmitted over HTTPS
- **Control**: You can delete synced history anytime from Seny

## Data Location

| Platform | Config Location |
|----------|-----------------|
| All | `~/.seny/config.json` |
| All | `~/.seny/machine_id` |

## Uninstalling

1. Stop any running agent (Ctrl+C or stop the service)
2. Remove the launch agent/service (if configured)
3. Delete the config directory:

```bash
rm -rf ~/.seny
```

4. Delete the agent files:

```bash
rm -rf ~/seny-agent
```

## License

Part of the Seny Personal Assistant project.
