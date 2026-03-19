"""
Token counting utilities for Seny.

Provides functions to estimate and track token usage for Claude API calls.
"""


class TokenCounter:
    """Utility class for estimating token counts."""

    # Token estimation constants
    # Anthropic models use ~4 characters per token on average for English text
    CHARS_PER_TOKEN = 4

    @staticmethod
    def estimate_tokens(text):
        """
        Estimate the number of tokens in a text string.

        This uses a simple heuristic: ~4 characters per token.
        While not perfectly accurate, it's good enough for context management.

        Args:
            text (str): Text to estimate tokens for

        Returns:
            int: Estimated token count
        """
        if not text:
            return 0
        return len(text) // TokenCounter.CHARS_PER_TOKEN

    @staticmethod
    def estimate_messages(messages):
        """
        Estimate total tokens in a list of messages.

        Includes overhead for message structure (role, formatting, etc.)

        Args:
            messages (list): List of message dicts with 'role' and 'content'

        Returns:
            int: Estimated total token count
        """
        if not messages:
            return 0

        total_tokens = 0

        for message in messages:
            # Count content tokens
            content = message.get('content', '')
            total_tokens += TokenCounter.estimate_tokens(content)

            # Add overhead for message structure (~10 tokens per message)
            # This accounts for role labels, JSON formatting, etc.
            total_tokens += 10

        return total_tokens

    @staticmethod
    def format_token_count(token_count):
        """
        Format token count for display.

        Args:
            token_count (int): Number of tokens

        Returns:
            str: Formatted string (e.g., "1.2K tokens" or "450 tokens")
        """
        if token_count >= 1000:
            return f"{token_count / 1000:.1f}K tokens"
        return f"{token_count} tokens"


class ConversationContext:
    """Manages conversation context within token limits."""

    def __init__(self, max_context_tokens=100000):
        """
        Initialize context manager.

        Args:
            max_context_tokens (int): Maximum tokens to use for conversation history.
                                      Default is 100K (Claude's context window is 200K,
                                      but we reserve space for system prompts and responses)
        """
        self.max_context_tokens = max_context_tokens
        self.token_counter = TokenCounter()

    def trim_history(self, messages, target_tokens=None):
        """
        Trim conversation history to fit within token limits.

        Keeps the most recent messages and removes older ones to stay under limit.
        Always preserves at least the last user-assistant exchange.

        Args:
            messages (list): List of message dicts
            target_tokens (int, optional): Target token count. Defaults to max_context_tokens.

        Returns:
            tuple: (trimmed_messages, removed_count, total_tokens)
        """
        if target_tokens is None:
            target_tokens = self.max_context_tokens

        if not messages:
            return [], 0, 0

        # Calculate current total tokens
        total_tokens = self.token_counter.estimate_messages(messages)

        # If we're under the limit, no trimming needed
        if total_tokens <= target_tokens:
            return messages, 0, total_tokens

        # We need to trim - start from the oldest and work forward
        # Always keep at least the last 2 messages (one exchange)
        min_messages_to_keep = 2

        if len(messages) <= min_messages_to_keep:
            # Can't trim if we're at minimum
            return messages, 0, total_tokens

        # Binary search approach: find the cutoff point
        # Start by keeping only recent messages
        for keep_count in range(len(messages), min_messages_to_keep - 1, -1):
            recent_messages = messages[-keep_count:]
            recent_tokens = self.token_counter.estimate_messages(recent_messages)

            if recent_tokens <= target_tokens:
                # Found a good cutoff point
                removed_count = len(messages) - keep_count
                return recent_messages, removed_count, recent_tokens

        # If we get here, even minimum messages exceed limit
        # Return minimum anyway (let API handle the error if it's truly too large)
        recent_messages = messages[-min_messages_to_keep:]
        recent_tokens = self.token_counter.estimate_messages(recent_messages)
        removed_count = len(messages) - min_messages_to_keep

        return recent_messages, removed_count, recent_tokens

    def get_context_stats(self, messages):
        """
        Get statistics about current conversation context.

        Args:
            messages (list): List of message dicts

        Returns:
            dict: Statistics including token count, percentage used, etc.
        """
        total_tokens = self.token_counter.estimate_messages(messages)
        percentage_used = (total_tokens / self.max_context_tokens) * 100

        return {
            'total_tokens': total_tokens,
            'max_tokens': self.max_context_tokens,
            'percentage_used': percentage_used,
            'message_count': len(messages),
            'formatted_tokens': TokenCounter.format_token_count(total_tokens),
            'formatted_max': TokenCounter.format_token_count(self.max_context_tokens)
        }
