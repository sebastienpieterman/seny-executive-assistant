import { useEffect, useState } from "react";
import DOMPurify from "dompurify";
import { api } from "@/lib/api";
import { Archive, ArrowLeft, ExternalLink, Loader2, Mail, MailOpen, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";

interface EmailDetail {
  id: string;
  from_: string;
  to: string;
  subject: string;
  date: string;
  body: string;
  attachments: Array<{ filename: string; mimeType: string; size: number }>;
  is_unread: boolean;
  provider?: string | null;
}

interface EmailPreviewProps {
  emailId: string;
  account?: string | null;
  provider?: string | null;
  onBack: (action?: "archived" | "deleted") => void;
}

export function EmailPreview({ emailId, account, provider, onBack }: EmailPreviewProps) {
  const [email, setEmail] = useState<EmailDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  const apiBase = provider === "outlook" ? "/api/microsoft" : "/api/email";
  const params = account ? `?email=${encodeURIComponent(account)}` : "";

  useEffect(() => {
    async function fetchEmail() {
      setLoading(true);
      setError(null);
      try {
        const data = await api.get<EmailDetail>(
          `${apiBase}/message/${emailId}${params}`,
        );
        setEmail(data);

        // Mark as read
        if (data.is_unread) {
          api.post(`${apiBase}/message/${emailId}/read${params}`).catch(() => {});
        }
      } catch (err) {
        setError("Failed to load email");
        console.error(err);
      } finally {
        setLoading(false);
      }
    }
    fetchEmail();
  }, [emailId, account, params, apiBase]);

  async function handleAction(action: string) {
    setActionLoading(action);
    try {
      await api.post(`${apiBase}/message/${emailId}/${action}${params}`);
      if (action === "archive") {
        onBack("archived");
      } else if (action === "trash") {
        onBack("deleted");
      } else if (action === "read" && email) {
        setEmail({ ...email, is_unread: false });
      } else if (action === "unread" && email) {
        setEmail({ ...email, is_unread: true });
      }
    } catch (err) {
      console.error(`Failed to ${action} email:`, err);
    } finally {
      setActionLoading(null);
    }
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (error || !email) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 text-muted-foreground">
        <p>{error || "Email not found"}</p>
        <Button variant="ghost" size="sm" onClick={() => onBack()}>
          Go back
        </Button>
      </div>
    );
  }

  const formattedDate = (() => {
    try {
      return new Date(email.date).toLocaleString("en-US", {
        weekday: "short",
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
    } catch {
      return email.date;
    }
  })();

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <Button variant="ghost" size="icon" onClick={() => onBack()} className="h-8 w-8">
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <h2 className="flex-1 truncate text-base font-semibold">
          {email.subject || "(No subject)"}
        </h2>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            title={email.is_unread ? "Mark as read" : "Mark as unread"}
            disabled={actionLoading !== null}
            onClick={() => handleAction(email.is_unread ? "read" : "unread")}
          >
            {email.is_unread ? <MailOpen className="h-4 w-4" /> : <Mail className="h-4 w-4" />}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            title="Archive"
            disabled={actionLoading !== null}
            onClick={() => handleAction("archive")}
          >
            <Archive className="h-4 w-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8 text-destructive hover:text-destructive"
            title="Delete"
            disabled={actionLoading !== null}
            onClick={() => handleAction("trash")}
          >
            <Trash2 className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* Email metadata */}
      <div className="space-y-1 border-b border-border/50 px-5 py-3">
        <div className="flex items-baseline justify-between">
          <span className="text-sm font-medium text-foreground">
            {email.from_}
          </span>
          <span className="text-xs text-muted-foreground">{formattedDate}</span>
        </div>
        <div className="text-xs text-muted-foreground">
          To: {email.to}
        </div>
      </div>

      {/* Body */}
      <ScrollArea className="flex-1">
        <div className="px-5 py-4">
          <div
            className="prose-email text-sm leading-relaxed text-foreground/90 [&_a]:text-blue-500 [&_a]:underline [&_img]:max-w-full"
            dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(email.body) }}
          />
        </div>

        {/* Attachments */}
        {email.attachments.length > 0 && (
          <div className="border-t border-border/50 px-5 py-3">
            <p className="mb-2 text-xs font-medium text-muted-foreground">
              {email.attachments.length} Attachment{email.attachments.length > 1 ? "s" : ""}
            </p>
            <div className="space-y-1">
              {email.attachments.map((att, i) => (
                <div
                  key={i}
                  className="flex items-center gap-2 rounded-md bg-sidebar-accent/50 px-3 py-1.5 text-xs text-muted-foreground"
                >
                  <ExternalLink className="h-3 w-3" />
                  {att.filename}
                </div>
              ))}
            </div>
          </div>
        )}
      </ScrollArea>
    </div>
  );
}
