#!/usr/bin/env python3
"""
Seny Local Agent - Syncs local data to Seny cloud.

This agent runs on your local machine and syncs data like browser history
to your Seny personal assistant in the cloud.

Usage:
    python seny_agent.py --setup     # First-time setup
    python seny_agent.py --sync      # One-time sync
    python seny_agent.py --daemon    # Run continuously (every 15 minutes)
    python seny_agent.py --status    # Check sync status

Requirements:
    pip install requests

Configuration is stored in ~/.seny/config.json
"""

import argparse
import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print("Error: 'requests' package is required.")
    print("Install with: pip install requests")
    sys.exit(1)


# ============================================================================
# Configuration
# ============================================================================

CONFIG_DIR = Path.home() / ".seny"
CONFIG_FILE = CONFIG_DIR / "config.json"
MACHINE_ID_FILE = CONFIG_DIR / "machine_id"

DEFAULT_CONFIG = {
    "seny_url": "http://localhost:8000",
    "api_token": "",
    "sync_interval_minutes": 15,
    "browser_history": {
        "enabled": True,
        "exclude_domains": ["localhost", "127.0.0.1", "0.0.0.0"]
    }
}


# ============================================================================
# Chrome History Paths
# ============================================================================

def get_chrome_history_path() -> Optional[Path]:
    """Get Chrome history database path for current OS."""
    system = platform.system()

    if system == "Darwin":  # macOS
        path = Path.home() / "Library/Application Support/Google/Chrome/Default/History"
    elif system == "Windows":
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            path = Path(local_appdata) / "Google/Chrome/User Data/Default/History"
        else:
            return None
    elif system == "Linux":
        path = Path.home() / ".config/google-chrome/Default/History"
    else:
        return None

    return path if path.exists() else None


# ============================================================================
# WebKit Timestamp Conversion
# ============================================================================

# WebKit timestamps are microseconds since January 1, 1601
WEBKIT_EPOCH = datetime(1601, 1, 1)


def webkit_to_datetime(webkit_timestamp: int) -> datetime:
    """Convert WebKit timestamp to Python datetime."""
    return WEBKIT_EPOCH + timedelta(microseconds=webkit_timestamp)


def webkit_to_iso(webkit_timestamp: int) -> str:
    """Convert WebKit timestamp to ISO format string."""
    dt = webkit_to_datetime(webkit_timestamp)
    return dt.isoformat()


# ============================================================================
# Agent Class
# ============================================================================

class SenyAgent:
    """
    Seny Local Agent - syncs local data to Seny cloud.
    """

    def __init__(self):
        self.config = self._load_config()
        self.machine_id = self._get_machine_id()

    def _load_config(self) -> dict:
        """Load configuration from file or create default."""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    config = json.load(f)
                # Merge with defaults for any missing keys
                merged = DEFAULT_CONFIG.copy()
                merged.update(config)
                return merged
            except Exception as e:
                print(f"Warning: Could not load config: {e}")

        return DEFAULT_CONFIG.copy()

    def _save_config(self) -> None:
        """Save current configuration to file."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(self.config, f, indent=2)
        # Set restrictive permissions
        os.chmod(CONFIG_FILE, 0o600)

    def _get_machine_id(self) -> str:
        """Get or generate unique machine identifier."""
        if MACHINE_ID_FILE.exists():
            return MACHINE_ID_FILE.read_text().strip()

        # Generate new machine ID
        system = platform.system()
        node = platform.node()
        machine_id = f"{system}-{node}-{uuid.uuid4().hex[:8]}"

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        MACHINE_ID_FILE.write_text(machine_id)

        return machine_id

    def _get_last_sync_time(self) -> Optional[datetime]:
        """Get the last sync time from config."""
        last_sync = self.config.get("last_sync_time")
        if last_sync:
            try:
                return datetime.fromisoformat(last_sync)
            except Exception:
                pass
        return None

    def _update_last_sync_time(self) -> None:
        """Update the last sync time in config."""
        self.config["last_sync_time"] = datetime.now().isoformat()
        self._save_config()

    def _extract_domain(self, url: str) -> Optional[str]:
        """Extract domain from URL."""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc
            if ":" in domain:
                domain = domain.split(":")[0]
            if domain.startswith("www."):
                domain = domain[4:]
            return domain if domain else None
        except Exception:
            return None

    def _should_exclude_url(self, url: str) -> bool:
        """Check if URL should be excluded from sync."""
        domain = self._extract_domain(url)
        if not domain:
            return True

        exclude_domains = self.config.get("browser_history", {}).get(
            "exclude_domains", []
        )

        return domain in exclude_domains

    # =========================================================================
    # Setup
    # =========================================================================

    def setup(self) -> bool:
        """Interactive setup wizard."""
        print("\n" + "=" * 60)
        print("Seny Local Agent Setup")
        print("=" * 60 + "\n")

        # Check for existing config
        if self.config.get("api_token"):
            print("Existing configuration found.")
            response = input("Do you want to reconfigure? (y/N): ").strip().lower()
            if response != "y":
                print("Setup cancelled.")
                return False

        # Get Seny URL
        default_url = self.config.get("seny_url", DEFAULT_CONFIG["seny_url"])
        print(f"\n1. Seny Server URL")
        print(f"   Default: {default_url}")
        url_input = input(f"   Enter URL (or press Enter for default): ").strip()
        seny_url = url_input if url_input else default_url

        # Verify connection
        print(f"\n   Checking connection to {seny_url}...")
        try:
            response = requests.get(f"{seny_url}/health", timeout=10)
            if response.status_code == 200:
                print("   Connection successful!")
            else:
                print(f"   Warning: Server returned status {response.status_code}")
        except requests.RequestException as e:
            print(f"   Warning: Could not connect to server: {e}")
            cont = input("   Continue anyway? (y/N): ").strip().lower()
            if cont != "y":
                return False

        # Get API token
        print(f"\n2. API Token")
        print(f"   To get your API token:")
        print(f"   1. Go to {seny_url}")
        print(f"   2. Log in to your account")
        print(f"   3. Go to Settings > API Token")
        print(f"   4. Copy your token")
        print()
        api_token = input("   Paste your API token: ").strip()

        if not api_token:
            print("   Error: API token is required.")
            return False

        # Verify token
        print("\n   Verifying token...")
        try:
            response = requests.get(
                f"{seny_url}/api/sync/status",
                headers={"Authorization": f"Bearer {api_token}"},
                timeout=10
            )
            if response.status_code == 200:
                print("   Token verified successfully!")
            elif response.status_code == 401:
                print("   Error: Invalid token. Please check and try again.")
                return False
            else:
                print(f"   Warning: Unexpected status {response.status_code}")
        except requests.RequestException as e:
            print(f"   Warning: Could not verify token: {e}")

        # Check Chrome history access
        print(f"\n3. Browser History")
        chrome_path = get_chrome_history_path()
        if chrome_path:
            print(f"   Chrome history found at: {chrome_path}")
        else:
            print("   Warning: Chrome history not found.")
            print("   Browser history sync will not work until Chrome is installed.")

        # Save configuration
        self.config["seny_url"] = seny_url
        self.config["api_token"] = api_token
        self._save_config()

        print(f"\n" + "=" * 60)
        print("Setup Complete!")
        print("=" * 60)
        print(f"\nConfiguration saved to: {CONFIG_FILE}")
        print(f"Machine ID: {self.machine_id}")
        print()
        print("Next steps:")
        print("  - Run: python seny_agent.py --sync    (one-time sync)")
        print("  - Run: python seny_agent.py --daemon  (continuous sync)")
        print()

        # Offer to do initial sync
        do_sync = input("Do you want to do an initial sync now? (Y/n): ").strip().lower()
        if do_sync != "n":
            return self.sync()

        return True

    # =========================================================================
    # Browser History Sync
    # =========================================================================

    def _copy_chrome_history(self) -> Optional[Path]:
        """Copy Chrome history to temp location (Chrome locks the file)."""
        source = get_chrome_history_path()
        if not source or not source.exists():
            print("Chrome history file not found.")
            return None

        # Copy to temp file
        temp_dir = tempfile.mkdtemp()
        dest = Path(temp_dir) / "History"

        try:
            shutil.copy2(source, dest)
            return dest
        except Exception as e:
            print(f"Could not copy Chrome history: {e}")
            print("Tip: Try closing Chrome and running again.")
            return None

    def _query_chrome_history(
        self,
        db_path: Path,
        since: Optional[datetime] = None,
        limit: int = 5000
    ) -> list[dict]:
        """Query Chrome history database."""
        entries = []

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cursor = conn.cursor()

            # Build query
            # Join urls and visits tables to get full history
            query = """
                SELECT
                    u.url,
                    u.title,
                    v.visit_time,
                    u.visit_count
                FROM visits v
                JOIN urls u ON v.url = u.id
                WHERE u.url NOT LIKE 'chrome://%'
                  AND u.url NOT LIKE 'chrome-extension://%'
                  AND u.url NOT LIKE 'file://%'
            """
            params = []

            if since:
                # Convert datetime to WebKit timestamp
                webkit_since = int((since - WEBKIT_EPOCH).total_seconds() * 1_000_000)
                query += " AND v.visit_time > ?"
                params.append(webkit_since)

            query += " ORDER BY v.visit_time DESC LIMIT ?"
            params.append(limit)

            cursor.execute(query, params)

            for row in cursor.fetchall():
                url, title, visit_time, visit_count = row

                # Skip excluded domains
                if self._should_exclude_url(url):
                    continue

                entries.append({
                    "url": url,
                    "title": title or "",
                    "visit_time": webkit_to_iso(visit_time),
                    "visit_count": visit_count
                })

            conn.close()

        except Exception as e:
            print(f"Error reading Chrome history: {e}")

        return entries

    def sync_browser_history(self) -> dict:
        """Sync browser history to Seny."""
        if not self.config.get("browser_history", {}).get("enabled", True):
            return {"status": "disabled", "message": "Browser history sync is disabled"}

        print("Syncing browser history...")

        # Copy history file
        db_path = self._copy_chrome_history()
        if not db_path:
            return {"status": "error", "message": "Could not access Chrome history"}

        try:
            # Get entries since last sync
            since = self._get_last_sync_time()
            entries = self._query_chrome_history(db_path, since=since)

            if not entries:
                print("No new history entries to sync.")
                return {"status": "success", "synced": 0}

            print(f"Found {len(entries)} entries to sync...")

            # Send to Seny
            result = self._send_to_seny(entries)

            # Update last sync time
            self._update_last_sync_time()

            return result

        finally:
            # Cleanup temp file
            try:
                db_path.parent.rmdir()
                db_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _send_to_seny(self, entries: list[dict]) -> dict:
        """Send history entries to Seny API."""
        seny_url = self.config.get("seny_url")
        api_token = self.config.get("api_token")

        if not api_token:
            print("Error: API token not configured. Run --setup first.")
            return {"status": "error", "message": "Not configured"}

        try:
            response = requests.post(
                f"{seny_url}/api/sync/browser-history",
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json"
                },
                json={
                    "machine_id": self.machine_id,
                    "entries": entries
                },
                timeout=60
            )

            if response.status_code == 200:
                result = response.json()
                print(f"Sync complete: {result.get('inserted_count', 0)} new entries")
                return {
                    "status": "success",
                    "synced": result.get("inserted_count", 0),
                    "skipped": result.get("skipped_count", 0)
                }
            elif response.status_code == 401:
                print("Error: Invalid or expired API token.")
                print("Run --setup to reconfigure.")
                return {"status": "error", "message": "Authentication failed"}
            else:
                print(f"Error: Server returned {response.status_code}")
                return {"status": "error", "message": f"HTTP {response.status_code}"}

        except requests.RequestException as e:
            print(f"Error: Could not connect to Seny: {e}")
            return {"status": "error", "message": str(e)}

    # =========================================================================
    # Main Operations
    # =========================================================================

    def sync(self) -> bool:
        """Perform one-time sync of all enabled data types."""
        print(f"\nSeny Agent - Sync")
        print(f"Machine: {self.machine_id}")
        print(f"Server: {self.config.get('seny_url')}")
        print("-" * 40)

        # Sync browser history
        result = self.sync_browser_history()

        print("-" * 40)
        print(f"Sync completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        return result.get("status") == "success"

    def daemon(self) -> None:
        """Run continuous sync loop."""
        interval = self.config.get("sync_interval_minutes", 15)

        print(f"\nSeny Agent - Daemon Mode")
        print(f"Machine: {self.machine_id}")
        print(f"Server: {self.config.get('seny_url')}")
        print(f"Interval: {interval} minutes")
        print("-" * 40)
        print("Press Ctrl+C to stop\n")

        try:
            while True:
                self.sync()
                print(f"\nNext sync in {interval} minutes...")
                time.sleep(interval * 60)
        except KeyboardInterrupt:
            print("\n\nDaemon stopped.")

    def status(self) -> None:
        """Show current sync status."""
        print(f"\nSeny Agent - Status")
        print("-" * 40)
        print(f"Machine ID: {self.machine_id}")
        print(f"Config file: {CONFIG_FILE}")
        print(f"Server: {self.config.get('seny_url', 'Not configured')}")
        print(f"API token: {'Configured' if self.config.get('api_token') else 'Not configured'}")

        last_sync = self._get_last_sync_time()
        if last_sync:
            print(f"Last sync: {last_sync.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            print("Last sync: Never")

        # Check Chrome history
        chrome_path = get_chrome_history_path()
        print(f"\nBrowser History:")
        print(f"  Chrome path: {chrome_path or 'Not found'}")
        print(f"  Enabled: {self.config.get('browser_history', {}).get('enabled', True)}")

        # Get status from server
        if self.config.get("api_token"):
            print("\nServer Status:")
            try:
                response = requests.get(
                    f"{self.config['seny_url']}/api/sync/status/{self.machine_id}",
                    headers={"Authorization": f"Bearer {self.config['api_token']}"},
                    timeout=10
                )
                if response.status_code == 200:
                    data = response.json()
                    for status in data.get("statuses", []):
                        print(f"  {status['sync_type']}: {status['status']}")
                        print(f"    Last: {status['last_sync_time']}")
                        print(f"    Count: {status['last_sync_count']}")
                elif response.status_code == 404:
                    print("  No sync records found on server")
                else:
                    print(f"  Could not fetch status (HTTP {response.status_code})")
            except requests.RequestException as e:
                print(f"  Could not connect to server: {e}")


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Seny Local Agent - Syncs local data to Seny cloud",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python seny_agent.py --setup     # First-time setup
    python seny_agent.py --sync      # One-time sync
    python seny_agent.py --daemon    # Run continuously
    python seny_agent.py --status    # Check sync status
        """
    )

    parser.add_argument(
        "--setup",
        action="store_true",
        help="Run interactive setup wizard"
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Perform one-time sync"
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuous sync (every 15 minutes)"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current sync status"
    )

    args = parser.parse_args()

    # If no arguments, show help
    if not any([args.setup, args.sync, args.daemon, args.status]):
        parser.print_help()
        return

    agent = SenyAgent()

    if args.setup:
        agent.setup()
    elif args.sync:
        agent.sync()
    elif args.daemon:
        agent.daemon()
    elif args.status:
        agent.status()


if __name__ == "__main__":
    main()
