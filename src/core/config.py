"""
Configuration management for Seny.

Handles environment variables and application settings.
"""

import os
from pathlib import Path
from dotenv import load_dotenv


# Load environment variables from .env file
env_path = Path(__file__).parent.parent.parent / '.env'
load_dotenv(dotenv_path=env_path)


class Config:
    """Application configuration."""

    # API Configuration
    ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')

    # JWT Authentication
    SECRET_KEY = os.getenv('SECRET_KEY')
    ALGORITHM = "HS256"  # HMAC with SHA-256
    ACCESS_TOKEN_EXPIRE_MINUTES = 60  # Default token expiry

    # Model Configuration
    # Set CLAUDE_MODEL env var to override, or defaults to Sonnet 4.5
    MODEL_NAME = os.getenv('CLAUDE_MODEL', 'claude-sonnet-4-5-20250929')
    MAX_TOKENS = 8192  # Maximum tokens for Claude's response

    # Available models (set CLAUDE_MODEL to one of these):
    # - "claude-sonnet-4-5-20250929" (Sonnet 4.5 - balanced, default)
    # - "claude-opus-4-5-20251101" (Opus 4.5 - most capable, ~5x cost)
    # - "claude-3-5-haiku-20241022" (Haiku 3.5 - fastest, cheapest)

    # Context Window Management
    # Claude Sonnet 4.5 has a 200K token context window
    # We reserve space for system prompts and responses
    MAX_CONTEXT_TOKENS = 100000  # Max tokens to use for conversation history

    # When context approaches this percentage, we'll show a warning
    CONTEXT_WARNING_THRESHOLD = 80  # Show warning at 80% usage

    # Data Storage
    DATA_DIR = Path(__file__).parent.parent.parent / 'data'
    HISTORY_FILE = DATA_DIR / 'conversation_history.json'

    @classmethod
    def validate(cls):
        """Validate that required configuration is present."""
        if not cls.ANTHROPIC_API_KEY or cls.ANTHROPIC_API_KEY == 'your-api-key-here':
            raise ValueError(
                "❌ ANTHROPIC_API_KEY not found or not set!\n"
                "\n"
                "Please add your API key to the .env file:\n"
                "1. Open the .env file in the project root\n"
                "2. Replace 'your-api-key-here' with your actual API key\n"
                "3. Get your key from: https://console.anthropic.com/\n"
            )

        if not cls.SECRET_KEY:
            raise ValueError(
                "❌ SECRET_KEY not found!\n"
                "\n"
                "Please add a SECRET_KEY to the .env file:\n"
                "1. Open the .env file in the project root\n"
                "2. Add: SECRET_KEY=<random-hex-string>\n"
                "3. Generate a key with: python3 -c 'import secrets; print(secrets.token_hex(32))'\n"
            )

        # Ensure data directory exists
        cls.DATA_DIR.mkdir(exist_ok=True)

        return True


# Validate configuration on import
Config.validate()
