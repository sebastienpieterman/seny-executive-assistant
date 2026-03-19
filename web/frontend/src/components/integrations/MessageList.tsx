import { useEffect, useRef, useState } from "react";
import { Loader2, Send } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export interface Message {
  id: string | number;
  text: string;
  user?: string;
  user_name?: string;
  sender_name?: string;
  sender?: string;
  ts?: string;
  date?: string;
  is_outgoing?: boolean;
}

interface MessageListProps {
  messages: Message[];
  loading: boolean;
  title: string;
  onSendReply?: (text: string) => Promise<void>;
  onClose: () => void;
  /** Format timestamp for display */
  formatTime?: (msg: Message) => string;
}

function defaultFormatTime(msg: Message): string {
  // Try Slack-style ts (epoch seconds as string like "1234567890.123456")
  if (msg.ts) {
    const timestamp = parseFloat(msg.ts) * 1000;
    const date = new Date(timestamp);
    const today = new Date();
    if (date.toDateString() === today.toDateString()) {
      return date.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
    }
    return date.toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
      " " + date.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  }
  // Try ISO date string
  if (msg.date) {
    const date = new Date(msg.date);
    const today = new Date();
    if (date.toDateString() === today.toDateString()) {
      return date.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
    }
    return date.toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
      " " + date.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  }
  return "";
}

export function MessageList({
  messages,
  loading,
  title,
  onSendReply,
  onClose,
  formatTime = defaultFormatTime,
}: MessageListProps) {
  const [replyText, setReplyText] = useState("");
  const [sending, setSending] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  async function handleSend() {
    if (!replyText.trim() || !onSendReply) return;
    setSending(true);
    try {
      await onSendReply(replyText.trim());
      setReplyText("");
    } catch (err) {
      console.error("Failed to send reply:", err);
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Header */}
      <div className="shrink-0 flex items-center justify-between border-b border-border px-4 py-3">
        <h3 className="text-sm font-semibold truncate">{title}</h3>
        <Button variant="ghost" size="sm" onClick={onClose} className="text-xs">
          Close
        </Button>
      </div>

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto min-h-0 px-4 py-3 space-y-3">
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : messages.length === 0 ? (
          <p className="text-center text-sm text-muted-foreground py-8">No messages</p>
        ) : (
          messages.map((msg) => {
            const author = msg.sender_name || msg.sender || msg.user_name || msg.user || "Unknown";
            const time = formatTime(msg);
            return (
              <div
                key={msg.id}
                className={cn(
                  "max-w-[85%] rounded-lg px-3 py-2",
                  msg.is_outgoing
                    ? "ml-auto bg-primary/20 text-foreground"
                    : "bg-sidebar-accent/40 text-foreground"
                )}
              >
                <div className="flex items-baseline gap-2 mb-0.5">
                  <span className="text-xs font-medium text-foreground/80">{author}</span>
                  {time && (
                    <span className="text-[10px] text-muted-foreground">{time}</span>
                  )}
                </div>
                <p className="text-sm whitespace-pre-wrap break-words">{msg.text}</p>
              </div>
            );
          })
        )}
      </div>

      {/* Reply input */}
      {onSendReply && (
        <div className="shrink-0 border-t border-border px-4 py-2">
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              handleSend();
            }}
          >
            <Input
              value={replyText}
              onChange={(e) => setReplyText(e.target.value)}
              placeholder="Type a message..."
              className="flex-1 h-9 text-sm"
              disabled={sending}
            />
            <Button
              type="submit"
              size="icon"
              className="h-9 w-9 shrink-0"
              disabled={!replyText.trim() || sending}
            >
              <Send className="h-4 w-4" />
            </Button>
          </form>
        </div>
      )}
    </div>
  );
}
