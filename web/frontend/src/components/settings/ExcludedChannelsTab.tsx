import { useState, useEffect, useMemo, useRef, useCallback } from "react";
import { api } from "@/lib/api";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Separator } from "@/components/ui/separator";
import { toast } from "sonner";
import { Hash, MessageSquare, Users, Loader2 } from "lucide-react";

interface ChannelInfo {
  id: string;
  name: string;
  excluded: boolean;
  type?: string;
  workspace_id?: string;
  workspace_name?: string;
}

interface SlackChannelsResponse {
  connected: boolean;
  channels: ChannelInfo[];
}

interface TelegramChatsResponse {
  connected: boolean;
  chats: ChannelInfo[];
}

// Group channels by workspace
interface WorkspaceGroup {
  workspace_id: string;
  workspace_name: string;
  channels: ChannelInfo[];
}

export function ExcludedChannelsTab() {
  const [slackData, setSlackData] = useState<SlackChannelsResponse>({
    connected: false,
    channels: [],
  });
  const [telegramData, setTelegramData] = useState<TelegramChatsResponse>({
    connected: false,
    chats: [],
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  // Debounce refs
  const saveTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRef = useRef(false);
  const latestDataRef = useRef({ slackData, telegramData });

  // Keep ref in sync with state
  useEffect(() => {
    latestDataRef.current = { slackData, telegramData };
  }, [slackData, telegramData]);

  // Save function - uses refs to get latest data
  const saveExclusions = useCallback(async () => {
    const { slackData: slack, telegramData: telegram } = latestDataRef.current;

    setSaving(true);
    try {
      await api.put("/api/settings/channel-exclusion", {
        slack_excluded_channels: slack.channels
          .filter((ch) => ch.excluded)
          .map((ch) => ch.id),
        telegram_excluded_chats: telegram.chats
          .filter((ch) => ch.excluded)
          .map((ch) => ch.id),
      });
      pendingRef.current = false;
      toast.success("Exclusions saved");
    } catch {
      toast.error("Failed to save exclusions");
    } finally {
      setSaving(false);
    }
  }, []);

  // Debounced save - waits 600ms after last change
  const debouncedSave = useCallback(() => {
    pendingRef.current = true;

    if (saveTimeoutRef.current) {
      clearTimeout(saveTimeoutRef.current);
    }

    saveTimeoutRef.current = setTimeout(() => {
      saveExclusions();
      saveTimeoutRef.current = null;
    }, 600);
  }, [saveExclusions]);

  // Save on unmount if pending
  useEffect(() => {
    return () => {
      if (saveTimeoutRef.current) {
        clearTimeout(saveTimeoutRef.current);
      }
      if (pendingRef.current) {
        // Fire and forget on unmount
        saveExclusions();
      }
    };
  }, [saveExclusions]);

  useEffect(() => {
    loadData();
  }, []);

  async function loadData() {
    try {
      const [slack, telegram] = await Promise.all([
        api
          .get<SlackChannelsResponse>("/api/settings/channel-exclusion/slack-channels")
          .catch(() => ({ connected: false, channels: [] })),
        api
          .get<TelegramChatsResponse>("/api/settings/channel-exclusion/telegram-chats")
          .catch(() => ({ connected: false, chats: [] })),
      ]);
      setSlackData(slack);
      setTelegramData(telegram);
    } catch {
      // Use defaults
    } finally {
      setLoading(false);
    }
  }

  // Group Slack channels by workspace
  const workspaceGroups = useMemo((): WorkspaceGroup[] => {
    const groups = new Map<string, WorkspaceGroup>();

    for (const channel of slackData.channels) {
      const wsId = channel.workspace_id || "unknown";
      const wsName = channel.workspace_name || "Unknown Workspace";

      if (!groups.has(wsId)) {
        groups.set(wsId, {
          workspace_id: wsId,
          workspace_name: wsName,
          channels: [],
        });
      }
      groups.get(wsId)!.channels.push(channel);
    }

    // Sort channels within each workspace: channels first, then DMs
    for (const group of groups.values()) {
      group.channels.sort((a, b) => {
        if (a.type === "channel" && b.type === "dm") return -1;
        if (a.type === "dm" && b.type === "channel") return 1;
        return a.name.localeCompare(b.name);
      });
    }

    return Array.from(groups.values());
  }, [slackData.channels]);

  function toggleSlackExclusion(channelId: string, excluded: boolean) {
    // Update local state immediately
    setSlackData((prev) => ({
      ...prev,
      channels: prev.channels.map((ch) =>
        ch.id === channelId ? { ...ch, excluded } : ch
      ),
    }));

    // Debounced save
    debouncedSave();
  }

  function toggleTelegramExclusion(chatId: string, excluded: boolean) {
    // Update local state immediately
    setTelegramData((prev) => ({
      ...prev,
      chats: prev.chats.map((ch) =>
        ch.id === chatId ? { ...ch, excluded } : ch
      ),
    }));

    // Debounced save
    debouncedSave();
  }

  function getChannelIcon(type?: string) {
    if (type === "dm") return <MessageSquare className="h-4 w-4" />;
    if (type === "group") return <Users className="h-4 w-4" />;
    if (type === "channel") return <Hash className="h-4 w-4" />;
    return <Hash className="h-4 w-4" />;
  }

  function renderChannelItem(channel: ChannelInfo, prefix: string) {
    return (
      <div
        key={channel.id}
        className="flex items-center justify-between py-1"
      >
        <div className="flex items-center gap-2">
          <Checkbox
            id={`${prefix}-${channel.id}`}
            checked={channel.excluded}
            onCheckedChange={(checked) =>
              prefix === "slack"
                ? toggleSlackExclusion(channel.id, checked === true)
                : toggleTelegramExclusion(channel.id, checked === true)
            }
          />
          <Label
            htmlFor={`${prefix}-${channel.id}`}
            className="flex items-center gap-1.5 text-sm cursor-pointer"
          >
            {getChannelIcon(channel.type)}
            <span className="truncate max-w-[180px]">{channel.name}</span>
            {prefix === "telegram" && channel.type && (
              <span className="text-xs text-muted-foreground">
                ({channel.type})
              </span>
            )}
          </Label>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="space-y-4">
        <div className="h-6 w-48 animate-pulse rounded bg-muted" />
        <div className="h-32 animate-pulse rounded bg-muted" />
      </div>
    );
  }

  const hasSlackChannels = slackData.connected && slackData.channels.length > 0;
  const hasTelegramChats = telegramData.connected && telegramData.chats.length > 0;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <div className="flex items-center gap-2">
          <h3 className="text-lg font-semibold">Excluded Channels</h3>
          {saving && (
            <span className="flex items-center gap-1 text-xs text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              Saving...
            </span>
          )}
        </div>
        <p className="text-sm text-muted-foreground">
          Excluded channels won't appear in unfulfilled commitments or scanner results.
        </p>
      </div>

      {/* Slack section */}
      <div className="space-y-3">
        <h4 className="text-sm font-medium flex items-center gap-2">
          <Hash className="h-4 w-4" />
          Slack Channels
        </h4>

        {!slackData.connected ? (
          <p className="text-sm text-muted-foreground border-l-2 border-border pl-3 py-1">
            Connect Slack in Integrations to manage channel exclusions.
          </p>
        ) : !hasSlackChannels ? (
          <p className="text-sm text-muted-foreground">No channels found.</p>
        ) : (
          <div className="space-y-4 max-h-64 overflow-y-auto pr-2">
            {workspaceGroups.map((group) => (
              <div key={group.workspace_id} className="space-y-2">
                {/* Workspace header - only show if multiple workspaces */}
                {workspaceGroups.length > 1 && (
                  <div className="text-xs font-medium text-muted-foreground uppercase tracking-wide border-b border-border pb-1">
                    {group.workspace_name}
                  </div>
                )}
                <div className="space-y-1">
                  {group.channels.map((channel) =>
                    renderChannelItem(channel, "slack")
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <Separator />

      {/* Telegram section */}
      <div className="space-y-3">
        <h4 className="text-sm font-medium flex items-center gap-2">
          <MessageSquare className="h-4 w-4" />
          Telegram Chats
        </h4>

        {!telegramData.connected ? (
          <p className="text-sm text-muted-foreground border-l-2 border-border pl-3 py-1">
            Connect Telegram in Integrations to manage chat exclusions.
          </p>
        ) : !hasTelegramChats ? (
          <p className="text-sm text-muted-foreground">No chats found.</p>
        ) : (
          <div className="space-y-2 max-h-48 overflow-y-auto pr-2">
            {telegramData.chats.map((chat) =>
              renderChannelItem(chat, "telegram")
            )}
          </div>
        )}
      </div>
    </div>
  );
}
