import { useState, useEffect, useCallback } from "react";
import { api } from "@/lib/api";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { toast } from "sonner";
import { Send, MessageSquare, ExternalLink, Link2 } from "lucide-react";

interface ChatSettings {
  telegram_chat_enabled: boolean;
  slack_chat_enabled: boolean;
  telegram_bot_linked: boolean;
  slack_bot_linked: boolean;
  telegram_bot_configured: boolean;
}

export function ChatTab() {
  const [settings, setSettings] = useState<ChatSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [linkCode, setLinkCode] = useState("");
  const [linking, setLinking] = useState(false);

  useEffect(() => {
    loadSettings();
  }, []);

  async function loadSettings() {
    try {
      const data = await api.get<ChatSettings>("/api/settings/multichannel-chat");
      setSettings(data);
    } catch {
      toast.error("Failed to load chat settings");
    } finally {
      setLoading(false);
    }
  }

  const updateSetting = useCallback(
    async (key: "telegram_chat_enabled" | "slack_chat_enabled", value: boolean) => {
      if (!settings) return;

      // Optimistic update
      setSettings({ ...settings, [key]: value });
      setSaving(true);

      try {
        await api.patch("/api/settings/multichannel-chat", { [key]: value });
        toast.success("Chat settings saved");
      } catch {
        // Revert on error
        setSettings({ ...settings, [key]: !value });
        toast.error("Failed to save chat settings");
      } finally {
        setSaving(false);
      }
    },
    [settings]
  );

  async function linkTelegram() {
    if (!linkCode.trim()) {
      toast.error("Please enter the code from Telegram");
      return;
    }

    setLinking(true);
    try {
      const result = await api.post<{ success: boolean; message: string }>(
        "/api/settings/multichannel-chat/telegram-link",
        { chat_id: linkCode.trim() }
      );
      toast.success(result.message);
      setLinkCode("");
      // Reload settings to update linked status
      loadSettings();
    } catch (err: unknown) {
      const error = err as { message?: string };
      toast.error(error.message || "Failed to link Telegram");
    } finally {
      setLinking(false);
    }
  }

  if (loading) {
    return (
      <div className="space-y-8">
        <div className="h-8 w-48 animate-pulse rounded bg-muted" />
        <div className="h-32 animate-pulse rounded bg-muted" />
        <div className="h-32 animate-pulse rounded bg-muted" />
      </div>
    );
  }

  if (!settings) {
    return (
      <div className="text-muted-foreground">
        Failed to load chat settings. Please refresh the page.
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <div>
        <h3 className="text-lg font-semibold">Multi-Channel Chat</h3>
        <p className="text-sm text-muted-foreground">
          Talk to Seny through Telegram or Slack, just like web chat.
        </p>
      </div>

      {/* Telegram Section */}
      <div className="space-y-4 rounded-lg border border-border p-4">
        <div className="flex items-center gap-3">
          <Send className="h-5 w-5 text-blue-400" />
          <div className="flex-1">
            <div className="flex items-center gap-2">
              <span className="font-medium">Telegram</span>
              {settings.telegram_bot_linked ? (
                <Badge variant="secondary" className="bg-green-900/50 text-green-400">
                  Connected
                </Badge>
              ) : settings.telegram_bot_configured ? (
                <Badge variant="secondary" className="bg-yellow-900/50 text-yellow-400">
                  Not linked
                </Badge>
              ) : (
                <Badge variant="secondary" className="bg-zinc-800 text-zinc-400">
                  Not configured
                </Badge>
              )}
            </div>
            <p className="text-sm text-muted-foreground">
              {settings.telegram_bot_linked
                ? "Send messages to the Seny bot in Telegram."
                : settings.telegram_bot_configured
                ? "Link your Telegram account to chat with Seny."
                : "Telegram bot is not configured on this server."}
            </p>
          </div>
        </div>

        {settings.telegram_bot_linked && (
          <>
            <Separator />
            <div className="flex items-center justify-between">
              <div>
                <Label htmlFor="telegram-enabled">Enable Telegram Chat</Label>
                <p className="text-xs text-muted-foreground">
                  When disabled, Seny won't respond to your Telegram messages.
                </p>
              </div>
              <Switch
                id="telegram-enabled"
                checked={settings.telegram_chat_enabled}
                onCheckedChange={(checked) => updateSetting("telegram_chat_enabled", checked)}
                disabled={saving}
              />
            </div>
          </>
        )}

        {!settings.telegram_bot_linked && settings.telegram_bot_configured && (
          <div className="space-y-4 rounded-md bg-zinc-800/50 p-4">
            <div>
              <p className="mb-2 font-medium">Link your Telegram account:</p>
              <ol className="list-inside list-decimal space-y-1 text-sm text-muted-foreground">
                <li>Open Telegram and search for your Telegram bot (created via <span className="font-mono">@BotFather</span>)</li>
                <li>Send any message to start a chat</li>
                <li>Copy the code the bot gives you</li>
                <li>Paste it below and click Link</li>
              </ol>
            </div>
            <Separator />
            <div className="flex gap-2">
              <Input
                placeholder="Enter code from Telegram"
                value={linkCode}
                onChange={(e) => setLinkCode(e.target.value)}
                className="flex-1"
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    linkTelegram();
                  }
                }}
              />
              <Button
                onClick={linkTelegram}
                disabled={linking || !linkCode.trim()}
                className="gap-2"
              >
                <Link2 className="h-4 w-4" />
                {linking ? "Linking..." : "Link"}
              </Button>
            </div>
          </div>
        )}
      </div>

      {/* Slack Section */}
      <div className="space-y-4 rounded-lg border border-border p-4">
        <div className="flex items-center gap-3">
          <MessageSquare className="h-5 w-5 text-purple-400" />
          <div className="flex-1">
            <div className="flex items-center gap-2">
              <span className="font-medium">Slack</span>
              {settings.slack_bot_linked ? (
                <Badge variant="secondary" className="bg-green-900/50 text-green-400">
                  Connected
                </Badge>
              ) : (
                <Badge variant="secondary" className="bg-yellow-900/50 text-yellow-400">
                  Reconnect needed
                </Badge>
              )}
            </div>
            <p className="text-sm text-muted-foreground">
              {settings.slack_bot_linked
                ? "DM the Seny bot in your Slack workspace."
                : "Reconnect Slack to enable bot chat."}
            </p>
          </div>
        </div>

        {settings.slack_bot_linked && (
          <>
            <Separator />
            <div className="flex items-center justify-between">
              <div>
                <Label htmlFor="slack-enabled">Enable Slack Chat</Label>
                <p className="text-xs text-muted-foreground">
                  When disabled, Seny won't respond to your Slack DMs.
                </p>
              </div>
              <Switch
                id="slack-enabled"
                checked={settings.slack_chat_enabled}
                onCheckedChange={(checked) => updateSetting("slack_chat_enabled", checked)}
                disabled={saving}
              />
            </div>
          </>
        )}

        {!settings.slack_bot_linked && (
          <div className="rounded-md bg-zinc-800/50 p-3 text-sm">
            <p className="mb-2 text-muted-foreground">
              Your Slack connection needs to be updated for bot chat.
            </p>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                toast.info("Go to Settings > Integrations to reconnect Slack");
              }}
              className="gap-2"
            >
              <ExternalLink className="h-4 w-4" />
              Reconnect Slack
            </Button>
          </div>
        )}
      </div>

      {/* Info */}
      <div className="text-sm text-muted-foreground">
        <p>
          Multi-channel chat gives you the same conversational capabilities as web chat.
          Seny can access your calendar, email, notes, and tasks from any channel.
        </p>
      </div>
    </div>
  );
}
