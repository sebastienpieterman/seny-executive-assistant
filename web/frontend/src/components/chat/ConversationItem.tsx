import { useState } from "react";
import { Edit2, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";

export interface Conversation {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

interface ConversationItemProps {
  conversation: Conversation;
  isActive: boolean;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onRename: (id: string, newTitle: string) => void;
}

function relativeTime(iso: string): string {
  const now = Date.now();
  const then = new Date(iso).getTime();
  const diffSec = Math.floor((now - then) / 1000);

  if (diffSec < 60) return "just now";
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay === 1) return "Yesterday";
  if (diffDay < 7) return `${diffDay}d ago`;
  return new Date(iso).toLocaleDateString();
}

export function ConversationItem({
  conversation,
  isActive,
  onSelect,
  onDelete,
  onRename,
}: ConversationItemProps) {
  const title = conversation.title || "New conversation";
  const [isEditing, setIsEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");

  function commitRename() {
    const trimmed = draftTitle.trim();
    if (trimmed && trimmed !== conversation.title) {
      onRename(conversation.id, trimmed);
    }
    setIsEditing(false);
  }

  return (
    <button
      onClick={isEditing ? undefined : () => onSelect(conversation.id)}
      className={cn(
        "group flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2.5 text-left transition-colors",
        isActive
          ? "bg-sidebar-accent text-foreground"
          : "text-muted-foreground hover:bg-sidebar-accent/50 hover:text-foreground"
      )}
    >
      <div className="min-w-0 flex-1">
        {isEditing ? (
          <input
            autoFocus
            value={draftTitle}
            onChange={(e) => setDraftTitle(e.target.value)}
            onBlur={commitRename}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                commitRename();
              } else if (e.key === "Escape") {
                setIsEditing(false);
              }
            }}
            onClick={(e) => e.stopPropagation()}
            className="w-full rounded bg-background px-1 text-sm font-medium outline-none ring-1 ring-ring"
          />
        ) : (
          <p className="truncate text-sm font-medium">{title}</p>
        )}
        <p className="text-xs text-muted-foreground">
          {relativeTime(conversation.updated_at)}
        </p>
      </div>

      <div className="flex shrink-0 items-center gap-0.5">
        <button
          onClick={(e) => {
            e.stopPropagation();
            setIsEditing(true);
            setDraftTitle(conversation.title ?? "");
          }}
          className="rounded p-1 text-muted-foreground opacity-0 transition-opacity hover:text-foreground group-hover:opacity-100"
          aria-label="Rename conversation"
        >
          <Edit2 className="h-3.5 w-3.5" />
        </button>

        <button
          onClick={(e) => {
            e.stopPropagation();
            onDelete(conversation.id);
          }}
          className="rounded p-1 text-muted-foreground opacity-0 transition-opacity hover:text-destructive group-hover:opacity-100"
          aria-label="Delete conversation"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>
    </button>
  );
}
