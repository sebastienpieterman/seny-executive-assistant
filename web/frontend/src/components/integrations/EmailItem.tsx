import { cn } from "@/lib/utils";

export interface EmailSummary {
  id: string;
  from_: string;
  subject: string;
  snippet: string;
  date: string;
  is_unread: boolean;
  account?: string | null;
  provider?: string | null; // "gmail" or "outlook"
}

interface EmailItemProps {
  email: EmailSummary;
  isSelected: boolean;
  onClick: () => void;
}

/** Extract display name from "John Doe <john@example.com>" format */
function extractName(from: string): string {
  if (!from) return "Unknown";
  const match = from.match(/^([^<]+)</);
  if (match) return match[1].trim();
  return from.split("@")[0];
}

/** Format email date to relative display */
function formatDate(dateStr: string): string {
  if (!dateStr) return "";
  try {
    const date = new Date(dateStr);
    const now = new Date();
    if (date.toDateString() === now.toDateString()) {
      return date.toLocaleTimeString("en-US", {
        hour: "numeric",
        minute: "2-digit",
      });
    }
    return date.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
    });
  } catch {
    return dateStr;
  }
}

export function EmailItem({ email, isSelected, onClick }: EmailItemProps) {
  const senderName = extractName(email.from_);

  return (
    <button
      onClick={onClick}
      className={cn(
        "w-full text-left px-4 py-3 border-b border-border/50 transition-colors",
        "hover:bg-sidebar-accent/50",
        isSelected && "bg-sidebar-accent",
      )}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span
          className={cn(
            "truncate text-sm",
            email.is_unread ? "font-semibold text-foreground" : "text-muted-foreground",
          )}
        >
          {senderName}
        </span>
        <span className="shrink-0 text-[11px] text-muted-foreground">
          {formatDate(email.date)}
        </span>
      </div>

      <div className="flex items-center gap-1.5 mt-0.5">
        {email.is_unread && (
          <span className="h-2 w-2 shrink-0 rounded-full bg-primary" />
        )}
        <span
          className={cn(
            "truncate text-sm",
            email.is_unread ? "font-medium text-foreground" : "text-muted-foreground",
          )}
        >
          {email.subject || "(No subject)"}
        </span>
      </div>

      <p className="mt-0.5 truncate text-xs text-muted-foreground/70">
        {email.snippet}
      </p>
    </button>
  );
}
