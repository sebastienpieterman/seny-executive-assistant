import { useMemo } from "react";
import { Bot } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { cn } from "@/lib/utils";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  created_at: string;
  tools_used?: string[];
}

interface MessageBubbleProps {
  message: ChatMessage;
  animate?: boolean;
}

/** Map tool IDs to short display labels. */
const TOOL_LABELS: Record<string, string> = {
  web_search: "Web Search",
  email_search: "Email",
  email_read: "Email",
  email_send: "Email Sent",
  calendar_list: "Calendar",
  calendar_get: "Calendar",
  calendar_create: "Calendar",
  calendar_update: "Calendar",
  calendar_delete: "Calendar",
  note_list: "Notes",
  note_create: "Notes",
  note_search: "Notes",
  note_read: "Notes",
  note_update: "Notes",
  note_delete: "Notes",
  note_list_tags: "Notes",
  task_create: "Tasks",
  task_list: "Tasks",
  task_complete: "Tasks",
  task_update: "Tasks",
  task_delete: "Tasks",
  task_add_reminder: "Tasks",
  slack_search: "Slack",
  slack_read: "Slack",
  slack_send: "Slack",
  slack_list_channels: "Slack",
  slack_list_dms: "Slack",
  telegram_search: "Telegram",
  telegram_read: "Telegram",
  telegram_send: "Telegram",
  telegram_list_chats: "Telegram",
  conversation_search: "Memory",
};

function formatTime(iso: string): string {
  // SQLite stores UTC without timezone suffix; append Z so JS parses it as UTC
  const normalized = /[Zz]|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : iso + "Z";
  const d = new Date(normalized);
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function MessageBubble({ message, animate = false }: MessageBubbleProps) {
  const isUser = message.role === "user";

  // Deduplicate tool labels
  const toolBadges = useMemo(() => {
    if (!message.tools_used?.length) return [];
    const labels = new Set(
      message.tools_used.map((t) => TOOL_LABELS[t] ?? t)
    );
    return Array.from(labels);
  }, [message.tools_used]);

  return (
    <div
      className={cn(
        "flex gap-3",
        isUser ? "justify-end" : "justify-start",
        animate && "animate-in fade-in-0 slide-in-from-bottom-2 duration-300"
      )}
    >
      {/* Assistant avatar */}
      {!isUser && (
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-[#1e1e1e]">
          <Bot className="h-4 w-4 text-[#d4a445]" />
        </div>
      )}

      <div
        className={cn(
          "max-w-[85%] space-y-1 lg:max-w-[75%]",
          isUser && "items-end"
        )}
      >
        {/* Message bubble */}
        <div
          className={cn(
            "rounded-xl px-4 py-2.5",
            isUser
              ? "bg-[#d4a445] text-[#0f0f0f]"
              : "bg-[#1a1a1a] text-foreground"
          )}
        >
          {isUser ? (
            <p className="whitespace-pre-wrap text-sm leading-relaxed">
              {message.content}
            </p>
          ) : (
            <MarkdownRenderer
              content={message.content}
              className="text-sm leading-relaxed"
            />
          )}
        </div>

        {/* Tool badges + timestamp row */}
        <div
          className={cn(
            "flex flex-wrap items-center gap-1.5 px-1",
            isUser ? "justify-end" : "justify-start"
          )}
        >
          {toolBadges.map((label) => (
            <Badge
              key={label}
              variant="secondary"
              className="h-5 px-1.5 text-[10px] font-normal text-muted-foreground"
            >
              {label}
            </Badge>
          ))}
          <span className="text-[11px] text-muted-foreground">
            {formatTime(message.created_at)}
          </span>
        </div>
      </div>

      {/* User avatar */}
      {isUser && (
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-[#d4a445]">
          <span className="text-xs font-semibold text-[#0f0f0f]">U</span>
        </div>
      )}
    </div>
  );
}
