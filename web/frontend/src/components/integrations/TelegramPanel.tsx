import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Loader2, MessageCircle, RefreshCw, User, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { MessageList, type Message } from "./MessageList";

interface TelegramAccount {
  phone_number: string;
  display_name?: string;
  user_name?: string;
}

interface TelegramChat {
  id: number;
  name: string;
  type: string; // "dm" | "group" | "channel" | "supergroup"
  unread_count?: number;
  last_message?: string;
}

interface TelegramStatusResponse {
  configured: boolean;
  connected: boolean;
  accounts: TelegramAccount[];
}

export function TelegramPanel() {
  const [, setAccounts] = useState<TelegramAccount[]>([]);
  const [selectedAccount, setSelectedAccount] = useState<string | null>(null);
  const [chats, setChats] = useState<TelegramChat[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [notConnected, setNotConnected] = useState(false);
  const [notConfigured, setNotConfigured] = useState(false);

  // Message view state
  const [selectedChat, setSelectedChat] = useState<{ id: number; name: string } | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);

  // Fetch status
  useEffect(() => {
    async function fetchStatus() {
      try {
        const data = await api.get<TelegramStatusResponse>("/api/telegram/status");
        if (!data.configured) {
          setNotConfigured(true);
          setLoading(false);
          return;
        }
        if (!data.connected || data.accounts.length === 0) {
          setNotConnected(true);
          setLoading(false);
          return;
        }
        setAccounts(data.accounts);
        setSelectedAccount(data.accounts[0].phone_number);
      } catch {
        setNotConnected(true);
        setLoading(false);
      }
    }
    fetchStatus();
  }, []);

  // Fetch chats
  const fetchChats = useCallback(async () => {
    if (!selectedAccount) return;
    setRefreshing(true);
    try {
      const url = `/api/telegram/chats?phone_number=${encodeURIComponent(selectedAccount)}`;
      const data = await api.get<{ chats: TelegramChat[] }>(url);
      setChats(data.chats || []);
      setNotConnected(false);
    } catch (err) {
      console.error("Failed to fetch Telegram chats:", err);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [selectedAccount]);

  useEffect(() => {
    if (selectedAccount) {
      fetchChats();
    }
  }, [selectedAccount, fetchChats]);

  // Open chat messages
  async function openChat(chatId: number, chatName: string) {
    setSelectedChat({ id: chatId, name: chatName });
    setMessagesLoading(true);
    try {
      const url = selectedAccount
        ? `/api/telegram/messages/${chatId}?phone_number=${encodeURIComponent(selectedAccount)}`
        : `/api/telegram/messages/${chatId}`;
      const data = await api.get<{ messages: Message[] }>(url);
      // Telegram messages may come reverse chronological — reverse for display
      const msgs = data.messages || [];
      setMessages(msgs.reverse());
      // Clear unread badge locally on open
      setChats(prev => prev.map(c =>
        c.id === chatId ? { ...c, unread_count: 0 } : c
      ));
    } catch (err) {
      console.error("Failed to load Telegram messages:", err);
      setMessages([]);
    } finally {
      setMessagesLoading(false);
    }
  }

  async function handleSendReply(text: string) {
    if (!selectedChat) return;
    await api.post("/api/telegram/send", {
      chat_id: selectedChat.id,
      text,
      phone_number: selectedAccount,
    });
    // Refresh messages
    await openChat(selectedChat.id, selectedChat.name);
  }

  // If viewing messages, show MessageList
  if (selectedChat) {
    return (
      <MessageList
        messages={messages}
        loading={messagesLoading}
        title={selectedChat.name}
        onSendReply={handleSendReply}
        onClose={() => setSelectedChat(null)}
      />
    );
  }

  // Not configured
  if (notConfigured) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-6 text-center">
        <MessageCircle className="h-12 w-12 text-muted-foreground/50" />
        <div>
          <h3 className="text-base font-medium text-foreground">Telegram Not Configured</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            Telegram is not configured on the server. Contact your administrator.
          </p>
        </div>
      </div>
    );
  }

  // Not connected
  if (notConnected) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-6 text-center">
        <MessageCircle className="h-12 w-12 text-muted-foreground/50" />
        <div>
          <h3 className="text-base font-medium text-foreground">Connect Telegram</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            Link your Telegram account to see your chats here.
          </p>
        </div>
        <Button
          onClick={() => {
            // Redirect to legacy UI for Telegram auth (multi-step flow)
            window.location.href = "/legacy/#telegram";
          }}
        >
          Connect Telegram
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

  function chatIcon(type: string) {
    switch (type) {
      case "dm": return <User className="h-3.5 w-3.5 shrink-0" />;
      case "group":
      case "supergroup": return <Users className="h-3.5 w-3.5 shrink-0" />;
      case "channel": return <MessageCircle className="h-3.5 w-3.5 shrink-0" />;
      default: return <MessageCircle className="h-3.5 w-3.5 shrink-0" />;
    }
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Header */}
      <div className="shrink-0 flex items-center justify-between border-b border-border px-4 py-3">
        <h2 className="text-base font-semibold">Telegram</h2>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          onClick={fetchChats}
          disabled={refreshing}
        >
          <RefreshCw className={cn("h-4 w-4", refreshing && "animate-spin")} />
        </Button>
      </div>

      {/* Chat list */}
      <ScrollArea className="flex-1 min-h-0">
        {chats.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
            <MessageCircle className="mb-2 h-8 w-8 opacity-50" />
            <p className="text-sm">No chats found</p>
          </div>
        ) : (
          <div className="py-1">
            {chats.map((chat) => (
              <button
                key={chat.id}
                onClick={() => openChat(chat.id, chat.name)}
                className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-sidebar-accent/30"
              >
                {chatIcon(chat.type)}
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-foreground truncate">{chat.name}</p>
                  {chat.last_message && (
                    <p className="text-xs text-muted-foreground truncate mt-0.5">
                      {chat.last_message}
                    </p>
                  )}
                </div>
                {(chat.unread_count ?? 0) > 0 && (
                  <Badge className="h-5 min-w-[20px] justify-center rounded-full px-1.5 text-[10px] font-semibold">
                    {chat.unread_count}
                  </Badge>
                )}
              </button>
            ))}
          </div>
        )}
      </ScrollArea>
    </div>
  );
}
