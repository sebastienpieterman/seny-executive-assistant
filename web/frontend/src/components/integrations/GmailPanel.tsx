import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { Mail, RefreshCw } from "lucide-react";
import { EmailListSkeleton } from "@/components/ui/LoadingSkeletons";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { EmailItem, type EmailSummary } from "./EmailItem";
import { EmailPreview } from "./EmailPreview";
import { usePrefetch } from "@/contexts/PrefetchContext";
import type { MailAccount } from "@/contexts/PrefetchContext";

export function GmailPanel() {
  const {
    mailAccounts: accounts,
    mailEmails: emails,
    mailLoading: loading,
    mailNotConnected: notConnected,
    refreshMail,
    setMailEmails: setEmails,
  } = usePrefetch();

  const [selectedAccount, setSelectedAccount] = useState<string>("all");
  const [refreshing, setRefreshing] = useState(false);

  // Email preview state
  const [selectedEmailId, setSelectedEmailId] = useState<string | null>(null);
  const [selectedEmailAccount, setSelectedEmailAccount] = useState<string | null>(null);
  const [selectedEmailProvider, setSelectedEmailProvider] = useState<string | null>(null);

  // Find provider for a given account email
  const getAccountProvider = useCallback(
    (email: string): string => {
      const acct = accounts.find((a: MailAccount) => a.email === email);
      return acct?.provider || "gmail";
    },
    [accounts],
  );

  // Fetch for a specific account (or refresh all)
  const fetchInbox = useCallback(async () => {
    setRefreshing(true);
    try {
      if (selectedAccount === "all") {
        await refreshMail();
      } else {
        const provider = getAccountProvider(selectedAccount);
        const apiBase = provider === "outlook" ? "/api/microsoft" : "/api/email";
        const data = await api.get<{ emails: EmailSummary[] }>(
          `${apiBase}/inbox?email=${encodeURIComponent(selectedAccount)}&max_results=20`,
        );
        setEmails(data.emails);
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        // not connected — prefetch already handles this
      }
      console.error("Failed to fetch inbox:", err);
    } finally {
      setRefreshing(false);
    }
  }, [selectedAccount, refreshMail, setEmails, getAccountProvider]);

  // Auto-fetch when account selection changes
  useEffect(() => {
    fetchInbox();
  }, [selectedAccount]); // eslint-disable-line react-hooks/exhaustive-deps

  // Show email preview
  if (selectedEmailId) {
    return (
      <EmailPreview
        emailId={selectedEmailId}
        account={selectedEmailAccount}
        provider={selectedEmailProvider}
        onBack={(action) => {
          const removedId = selectedEmailId;
          setSelectedEmailId(null);
          setSelectedEmailAccount(null);
          setSelectedEmailProvider(null);
          if (action === "archived" || action === "deleted") {
            setEmails((prev) => prev.filter((e) => e.id !== removedId));
          }
        }}
      />
    );
  }

  // Not connected state
  if (notConnected) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-6 text-center">
        <Mail className="h-12 w-12 text-muted-foreground/50" />
        <div>
          <h3 className="text-base font-medium text-foreground">Connect Email</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            Link your Gmail or Outlook account to see your inbox here.
          </p>
        </div>
        <Button
          onClick={() => {
            window.location.href = "/api/email/connect";
          }}
        >
          Connect Email
        </Button>
      </div>
    );
  }

  // Loading state
  if (loading) {
    return (
      <div className="flex h-full flex-col overflow-hidden">
        <div className="shrink-0 flex items-center justify-between border-b border-border px-4 py-3">
          <h2 className="text-base font-semibold">Inbox</h2>
        </div>
        <EmailListSkeleton />
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Header */}
      <div className="shrink-0 flex items-center justify-between border-b border-border px-4 py-3">
        <h2 className="text-base font-semibold">Inbox</h2>
        <div className="flex items-center gap-2">
          {accounts.length > 1 && (
            <Select value={selectedAccount} onValueChange={setSelectedAccount}>
              <SelectTrigger className="h-8 w-auto min-w-[120px] text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All accounts</SelectItem>
                {accounts.map((acc) => (
                  <SelectItem key={acc.email} value={acc.email}>
                    {acc.email}
                    {accounts.some((o) => o.provider !== acc.provider) && (
                      <span className="ml-1 text-muted-foreground">
                        ({acc.provider === "outlook" ? "Outlook" : "Gmail"})
                      </span>
                    )}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={fetchInbox}
            disabled={refreshing}
          >
            <RefreshCw
              className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`}
            />
          </Button>
        </div>
      </div>

      {/* Email list */}
      <ScrollArea className="flex-1 min-h-0">
        {emails.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
            <Mail className="mb-2 h-8 w-8 opacity-50" />
            <p className="text-sm">No emails in inbox</p>
          </div>
        ) : (
          emails.map((email) => (
            <EmailItem
              key={email.id}
              email={email}
              isSelected={email.id === selectedEmailId}
              onClick={() => {
                setSelectedEmailId(email.id);
                setSelectedEmailAccount(email.account || null);
                setSelectedEmailProvider(email.provider || null);
              }}
            />
          ))
        )}
      </ScrollArea>
    </div>
  );
}
