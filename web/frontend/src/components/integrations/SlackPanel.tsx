import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Hash, Loader2, Lock, MessageSquare, RefreshCw, Users } from "lucide-react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { MessageList, type Message } from "./MessageList";

interface SlackWorkspace {
  team_id: string;
  team_name: string;
}

interface SlackChannel {
  id: string;
  name: string;
  is_private?: boolean;
  type?: string;
}

interface SlackDM {
  id: string;
  name: string;
}

interface SlackSidebarData {
  channels: SlackChannel[];
  dms: SlackDM[];
  group_dms?: SlackDM[];
}

interface SlackStatusResponse {
  workspaces: SlackWorkspace[];
}

export function SlackPanel() {
  const [workspaces, setWorkspaces] = useState<SlackWorkspace[]>([]);
  const [selectedWorkspace, setSelectedWorkspace] = useState<string | null>(null);
  const [sidebarData, setSidebarData] = useState<SlackSidebarData | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [notConnected, setNotConnected] = useState(false);

  // Message view state
  const [selectedChannel, setSelectedChannel] = useState<{ id: string; name: string } | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);

  // Collapsible sections
  const [channelsOpen, setChannelsOpen] = useState(true);
  const [dmsOpen, setDmsOpen] = useState(true);

  // Fetch status
  useEffect(() => {
    async function fetchStatus() {
      try {
        const data = await api.get<SlackStatusResponse>("/api/slack/status");
        const ws = data.workspaces || [];
        setWorkspaces(ws);
        if (ws.length === 0) {
          setNotConnected(true);
          setLoading(false);
        } else {
          setSelectedWorkspace(ws[0].team_id);
        }
      } catch {
        setNotConnected(true);
        setLoading(false);
      }
    }
    fetchStatus();
  }, []);

  // Fetch sidebar when workspace selected
  const fetchSidebar = useCallback(async () => {
    if (!selectedWorkspace) return;
    setRefreshing(true);
    try {
      const data = await api.get<SlackSidebarData>(
        `/api/slack/sidebar?team_id=${selectedWorkspace}`
      );
      setSidebarData(data);
      setNotConnected(false);
    } catch (err) {
      console.error("Failed to fetch Slack sidebar:", err);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [selectedWorkspace]);

  useEffect(() => {
    if (selectedWorkspace) {
      fetchSidebar();
    }
  }, [selectedWorkspace, fetchSidebar]);

  // Open channel messages
  async function openChannel(channelId: string, channelName: string) {
    setSelectedChannel({ id: channelId, name: channelName });
    setMessagesLoading(true);
    try {
      let url = `/api/slack/messages/${channelId}?limit=30`;
      if (selectedWorkspace) {
        url += `&team_id=${selectedWorkspace}`;
      }
      const data = await api.get<{ messages: Message[] }>(url);
      // Messages come reverse chronological — reverse for display
      setMessages((data.messages || []).reverse());
    } catch (err) {
      console.error("Failed to load Slack messages:", err);
      setMessages([]);
    } finally {
      setMessagesLoading(false);
    }
  }

  async function handleSendReply(text: string) {
    if (!selectedChannel || !selectedWorkspace) return;
    await api.post("/api/slack/send", {
      channel_id: selectedChannel.id,
      text,
      team_id: selectedWorkspace,
    });
    // Refresh messages
    await openChannel(selectedChannel.id, selectedChannel.name);
  }

  // If viewing messages, show MessageList
  if (selectedChannel) {
    return (
      <MessageList
        messages={messages}
        loading={messagesLoading}
        title={selectedChannel.name}
        onSendReply={handleSendReply}
        onClose={() => setSelectedChannel(null)}
      />
    );
  }

  // Not connected
  if (notConnected) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-6 text-center">
        <MessageSquare className="h-12 w-12 text-muted-foreground/50" />
        <div>
          <h3 className="text-base font-medium text-foreground">Connect Slack</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            Link your Slack workspace to see channels and messages here.
          </p>
        </div>
        <Button
          onClick={async () => {
            try {
              const data = await api.get<{ auth_url: string }>("/api/slack/auth-url");
              window.location.href = data.auth_url;
            } catch (err) {
              console.error("Failed to get Slack auth URL:", err);
            }
          }}
        >
          Connect Slack
        </Button>
      </div>
    );
  }

  // Loading
  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const channels = sidebarData?.channels || [];
  const dms = sidebarData?.dms || [];
  const groupDms = sidebarData?.group_dms || [];
  const allDms = [...dms, ...groupDms];

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Header */}
      <div className="shrink-0 flex items-center justify-between border-b border-border px-4 py-3">
        {workspaces.length > 1 ? (
          <Select
            value={selectedWorkspace || ""}
            onValueChange={(val) => setSelectedWorkspace(val)}
          >
            <SelectTrigger className="h-8 w-[180px] text-sm font-semibold">
              <SelectValue placeholder="Select workspace" />
            </SelectTrigger>
            <SelectContent>
              {workspaces.map((ws) => (
                <SelectItem key={ws.team_id} value={ws.team_id}>
                  {ws.team_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          <h2 className="text-base font-semibold">
            {workspaces.length === 1 ? workspaces[0].team_name : "Slack"}
          </h2>
        )}
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          onClick={fetchSidebar}
          disabled={refreshing}
        >
          <RefreshCw className={cn("h-4 w-4", refreshing && "animate-spin")} />
        </Button>
      </div>

      {/* Channel / DM list */}
      <ScrollArea className="flex-1 min-h-0">
        <div className="py-2">
          {/* Channels */}
          <Collapsible open={channelsOpen} onOpenChange={setChannelsOpen}>
            <CollapsibleTrigger className="flex w-full items-center gap-1.5 px-4 py-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground hover:text-foreground">
              Channels ({channels.length})
            </CollapsibleTrigger>
            <CollapsibleContent>
              {channels.map((ch) => (
                <button
                  key={ch.id}
                  onClick={() => openChannel(ch.id, `${ch.is_private ? "" : "#"}${ch.name}`)}
                  className="flex w-full items-center gap-2 px-4 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent/30 hover:text-foreground"
                >
                  {ch.is_private ? (
                    <Lock className="h-3.5 w-3.5 shrink-0" />
                  ) : (
                    <Hash className="h-3.5 w-3.5 shrink-0" />
                  )}
                  <span className="truncate">{ch.name}</span>
                </button>
              ))}
            </CollapsibleContent>
          </Collapsible>

          {/* DMs */}
          {allDms.length > 0 && (
            <Collapsible open={dmsOpen} onOpenChange={setDmsOpen}>
              <CollapsibleTrigger className="flex w-full items-center gap-1.5 px-4 py-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground hover:text-foreground mt-2">
                Direct Messages ({allDms.length})
              </CollapsibleTrigger>
              <CollapsibleContent>
                {allDms.map((dm) => (
                  <button
                    key={dm.id}
                    onClick={() => openChannel(dm.id, dm.name || "DM")}
                    className="flex w-full items-center gap-2 px-4 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent/30 hover:text-foreground"
                  >
                    <Users className="h-3.5 w-3.5 shrink-0" />
                    <span className="truncate">{dm.name || "Unknown"}</span>
                  </button>
                ))}
              </CollapsibleContent>
            </Collapsible>
          )}

          {channels.length === 0 && allDms.length === 0 && (
            <div className="flex flex-col items-center justify-center py-8 text-muted-foreground">
              <MessageSquare className="mb-2 h-8 w-8 opacity-50" />
              <p className="text-sm">No channels or DMs</p>
            </div>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
