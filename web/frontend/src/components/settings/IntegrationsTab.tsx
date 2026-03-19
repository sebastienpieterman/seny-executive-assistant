import { useState, useEffect } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Chrome,
  Mail,
  Calendar,
  HardDrive,
  MessageSquare,
  Send,
  MapPin,
  AlertTriangle,
} from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

type StatusKey = "gmail" | "calendar" | "drive" | "slack" | "telegram" | "location" | "microsoft";

interface IntegrationStatus {
  connected: boolean;
  healthy: boolean;
}

interface StatusMap {
  gmail: IntegrationStatus;
  calendar: IntegrationStatus;
  drive: IntegrationStatus;
  slack: IntegrationStatus;
  telegram: IntegrationStatus;
  location: IntegrationStatus;
  microsoft: IntegrationStatus;
}

interface GoogleAccount { email: string }
interface SlackWorkspace { team_id: string; team_name: string }
interface TelegramAccount { phone_number: string; display_name?: string }
interface MicrosoftAccount { email: string }

export function IntegrationsTab() {
  const [status, setStatus] = useState<StatusMap>({
    gmail: { connected: false, healthy: false },
    calendar: { connected: false, healthy: false },
    drive: { connected: false, healthy: false },
    slack: { connected: false, healthy: false },
    telegram: { connected: false, healthy: false },
    location: { connected: false, healthy: false },
    microsoft: { connected: false, healthy: false },
  });
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [driveSyncing, setDriveSyncing] = useState(false);

  // Account lists for disconnect
  const [googleAccounts, setGoogleAccounts] = useState<GoogleAccount[]>([]);
  const [slackWorkspaces, setSlackWorkspaces] = useState<SlackWorkspace[]>([]);
  const [telegramAccounts, setTelegramAccounts] = useState<TelegramAccount[]>([]);
  const [microsoftAccounts, setMicrosoftAccounts] = useState<MicrosoftAccount[]>([]);

  // Disconnect confirmation: stores the identifier string of the item being confirmed
  const [confirmKey, setConfirmKey] = useState<string | null>(null);
  const [disconnecting, setDisconnecting] = useState(false);

  useEffect(() => {
    const check = (key: StatusKey, fn: () => Promise<IntegrationStatus>) => {
      fn()
        .then((result) =>
          setStatus((prev) => ({ ...prev, [key]: result }))
        )
        .catch(() => {})
        .finally(() => setChecked((prev) => new Set(prev).add(key)));
    };

    // Gmail uses health endpoint to check if token is valid
    check("gmail", async () => {
      const d = await api.get<{ connected: boolean; healthy: boolean }>("/api/email/health");
      return { connected: d.connected, healthy: d.healthy };
    });
    // Calendar shares Google OAuth with Gmail
    check("calendar", async () => {
      const d = await api.get<{ connected: boolean; healthy: boolean }>("/api/email/health");
      return { connected: d.connected, healthy: d.healthy };
    });
    // Slack uses health endpoint
    check("slack", async () => {
      const d = await api.get<{ connected: boolean; healthy: boolean }>("/api/slack/health");
      return { connected: d.connected, healthy: d.healthy };
    });
    // Telegram uses bot token, just check status (no refresh issues)
    check("telegram", async () => {
      const d = await api.get<{ connected: boolean; accounts?: TelegramAccount[] }>("/api/telegram/status");
      if (d.accounts && d.accounts.length > 0) {
        setTelegramAccounts(d.accounts);
      }
      return { connected: d.connected, healthy: d.connected };
    });
    // Drive shares Google OAuth with Gmail
    check("drive", async () => {
      const d = await api.get<{ connected: boolean; healthy: boolean }>("/api/email/health");
      return { connected: d.connected, healthy: d.healthy };
    });
    // Location is local data, just check if records exist
    check("location", async () => {
      const d = await api.get<{ total_records: number }>("/api/location/stats");
      const hasData = d.total_records > 0;
      return { connected: hasData, healthy: hasData };
    });
    // Microsoft uses status for now (could add health later)
    check("microsoft", async () => {
      const d = await api.get<{ connected: boolean }>("/api/microsoft/status");
      return { connected: d.connected, healthy: d.connected };
    });
  }, []);

  // Fetch account lists once integrations are checked and connected
  useEffect(() => {
    if (checked.has("gmail") && status.gmail.connected) {
      api.get<{ accounts: GoogleAccount[] }>("/api/email/accounts")
        .then((d) => setGoogleAccounts(d.accounts ?? []))
        .catch(() => {});
    }
  }, [checked, status.gmail.connected]);

  useEffect(() => {
    if (checked.has("slack") && status.slack.connected) {
      api.get<{ workspaces: SlackWorkspace[] }>("/api/slack/workspaces")
        .then((d) => setSlackWorkspaces(d.workspaces ?? []))
        .catch(() => {});
    }
  }, [checked, status.slack.connected]);

  useEffect(() => {
    if (checked.has("microsoft") && status.microsoft.connected) {
      api.get<{ accounts: MicrosoftAccount[] }>("/api/microsoft/accounts")
        .then((d) => setMicrosoftAccounts(d.accounts ?? []))
        .catch(() => {});
    }
  }, [checked, status.microsoft.connected]);

  async function connectGoogle() {
    try {
      const data = await api.get<{ auth_url: string }>("/api/email/auth-url");
      window.location.href = data.auth_url;
    } catch {
      toast.error("Failed to start Google connection. Please try again.");
    }
  }

  async function connectSlack() {
    try {
      const data = await api.get<{ auth_url: string }>("/api/slack/auth-url");
      window.location.href = data.auth_url;
    } catch {
      toast.error("Failed to start Slack connection. Please try again.");
    }
  }

  async function syncDrive() {
    setDriveSyncing(true);
    try {
      await api.post("/api/drive/sync");
      toast.success("Drive sync started. This may take a few minutes.");
    } catch {
      toast.error("Failed to start Drive sync");
    } finally {
      setDriveSyncing(false);
    }
  }

  async function connectMicrosoft() {
    try {
      const data = await api.get<{ auth_url: string }>("/api/microsoft/auth-url");
      window.location.href = data.auth_url;
    } catch {
      toast.error("Failed to start Microsoft connection. Please try again.");
    }
  }

  function connectTelegram() {
    toast.info(
      "Telegram connection is managed through the Telegram bot. Send /start to your Telegram bot (created via @BotFather)."
    );
  }

  async function handleDisconnect(type: string, id: string) {
    setDisconnecting(true);
    try {
      if (type === "google") {
        await api.delete(`/api/email/disconnect?email=${encodeURIComponent(id)}`);
        setGoogleAccounts((prev) => prev.filter((a) => a.email !== id));
        if (googleAccounts.length <= 1) {
          setStatus((prev) => ({
            ...prev,
            gmail: { connected: false, healthy: false },
            calendar: { connected: false, healthy: false },
            drive: { connected: false, healthy: false },
          }));
        }
        toast.success(`Disconnected ${id}`);
      } else if (type === "slack") {
        await api.delete(`/api/slack/disconnect/${encodeURIComponent(id)}`);
        setSlackWorkspaces((prev) => prev.filter((w) => w.team_id !== id));
        if (slackWorkspaces.length <= 1) {
          setStatus((prev) => ({ ...prev, slack: { connected: false, healthy: false } }));
        }
        toast.success("Slack workspace disconnected");
      } else if (type === "telegram") {
        await api.delete(`/api/telegram/disconnect/${encodeURIComponent(id)}`);
        setTelegramAccounts((prev) => prev.filter((a) => a.phone_number !== id));
        if (telegramAccounts.length <= 1) {
          setStatus((prev) => ({ ...prev, telegram: { connected: false, healthy: false } }));
        }
        toast.success(`Disconnected ${id}`);
      } else if (type === "microsoft") {
        await api.delete(`/api/microsoft/disconnect?email=${encodeURIComponent(id)}`);
        setMicrosoftAccounts((prev) => prev.filter((a) => a.email !== id));
        if (microsoftAccounts.length <= 1) {
          setStatus((prev) => ({ ...prev, microsoft: { connected: false, healthy: false } }));
        }
        toast.success(`Disconnected ${id}`);
      }
    } catch {
      toast.error("Disconnect failed. Please try again.");
    } finally {
      setDisconnecting(false);
      setConfirmKey(null);
    }
  }

  function DisconnectButton({ type, id }: { type: string; id: string }) {
    const key = `${type}:${id}`;
    if (confirmKey === key) {
      return (
        <div className="flex items-center gap-1.5">
          <span className="text-xs text-muted-foreground">Sure?</span>
          <Button
            variant="destructive"
            size="sm"
            className="h-6 text-xs px-2"
            onClick={() => handleDisconnect(type, id)}
            disabled={disconnecting}
          >
            {disconnecting ? "…" : "Yes"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="h-6 text-xs px-2"
            onClick={() => setConfirmKey(null)}
            disabled={disconnecting}
          >
            Cancel
          </Button>
        </div>
      );
    }
    return (
      <Button
        variant="ghost"
        size="sm"
        className="h-6 text-xs px-2 text-destructive hover:text-destructive hover:bg-destructive/10"
        onClick={() => setConfirmKey(key)}
      >
        Disconnect
      </Button>
    );
  }

  function statusBadge(key: StatusKey) {
    if (!checked.has(key)) {
      return (
        <Badge variant="secondary" className="text-[10px]">
          Checking...
        </Badge>
      );
    }

    const { connected, healthy } = status[key];

    if (!connected) {
      return (
        <Badge variant="secondary" className="text-[10px]">
          Not connected
        </Badge>
      );
    }

    if (!healthy) {
      return (
        <Badge variant="destructive" className="text-[10px] flex items-center gap-1">
          <AlertTriangle className="h-3 w-3" />
          Reconnect needed
        </Badge>
      );
    }

    return (
      <Badge variant="default" className="text-[10px]">
        Connected
      </Badge>
    );
  }

  // Google is connected if any of its sub-services are
  const googleConnected = status.gmail.connected || status.calendar.connected || status.drive.connected;
  const googleHealthy = status.gmail.healthy;
  const googleChecked =
    checked.has("gmail") || checked.has("calendar") || checked.has("drive");

  return (
    <div className="space-y-8">
      <div>
        <h3 className="text-lg font-semibold">Integrations</h3>
        <p className="text-sm text-muted-foreground">
          Connect your accounts to give Seny access to your data.
        </p>
      </div>

      <div className="space-y-4">
        {/* Google (grouped) */}
        <div className="rounded-lg border border-border bg-card">
          <div className="flex items-center justify-between p-4">
            <div className="flex items-center gap-3">
              <div className="flex h-9 w-9 items-center justify-center rounded-md bg-muted">
                <Chrome className="h-5 w-5 text-muted-foreground" />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">Google</span>
                  {!googleChecked ? (
                    <Badge variant="secondary" className="text-[10px]">
                      Checking...
                    </Badge>
                  ) : !googleConnected ? (
                    <Badge variant="secondary" className="text-[10px]">
                      Not connected
                    </Badge>
                  ) : !googleHealthy ? (
                    <Badge variant="destructive" className="text-[10px] flex items-center gap-1">
                      <AlertTriangle className="h-3 w-3" />
                      Reconnect needed
                    </Badge>
                  ) : (
                    <Badge variant="default" className="text-[10px]">
                      Connected
                    </Badge>
                  )}
                </div>
                <p className="text-xs text-muted-foreground">
                  Gmail, Calendar, and Drive share one Google sign-in.
                </p>
              </div>
            </div>
            <Button variant="outline" size="sm" onClick={connectGoogle}>
              {googleConnected ? "Add Account" : "Connect Google"}
            </Button>
          </div>

          {/* Sub-services */}
          <div className="border-t border-border px-4 py-3 space-y-2">
            <div className="flex items-center gap-2 pl-12">
              <Mail className="h-4 w-4 text-muted-foreground" />
              <span className="text-xs text-muted-foreground">Gmail</span>
              {statusBadge("gmail")}
            </div>
            <div className="flex items-center gap-2 pl-12">
              <Calendar className="h-4 w-4 text-muted-foreground" />
              <span className="text-xs text-muted-foreground">Calendar</span>
              {statusBadge("calendar")}
            </div>
            <div className="flex items-center gap-2 pl-12">
              <HardDrive className="h-4 w-4 text-muted-foreground" />
              <span className="text-xs text-muted-foreground">Drive</span>
              {statusBadge("drive")}
              {googleConnected && (
                <Button variant="ghost" size="sm" onClick={syncDrive} disabled={driveSyncing} className="ml-auto h-6 text-xs px-2">
                  {driveSyncing ? "Syncing..." : "Sync Now"}
                </Button>
              )}
            </div>

            {/* Per-account disconnect rows */}
            {googleAccounts.length > 0 && (
              <div className="pt-1 pl-12 space-y-1">
                {googleAccounts.map((acct) => (
                  <div key={acct.email} className="flex items-center justify-between">
                    <span className="text-xs text-muted-foreground">{acct.email}</span>
                    <DisconnectButton type="google" id={acct.email} />
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Microsoft (Outlook) */}
        <div className="rounded-lg border border-border bg-card">
          <div className="flex items-center justify-between p-4">
            <div className="flex items-center gap-3">
              <div className="flex h-9 w-9 items-center justify-center rounded-md bg-muted">
                <Mail className="h-5 w-5 text-muted-foreground" />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">Microsoft (Outlook)</span>
                  {statusBadge("microsoft")}
                </div>
                <p className="text-xs text-muted-foreground">
                  Outlook email and calendar
                </p>
              </div>
            </div>
            <Button variant="outline" size="sm" onClick={connectMicrosoft}>
              {status.microsoft.connected ? "Add Account" : "Connect Outlook"}
            </Button>
          </div>
          {microsoftAccounts.length > 0 && (
            <div className="border-t border-border px-4 py-3 pl-[52px] space-y-1">
              {microsoftAccounts.map((acct) => (
                <div key={acct.email} className="flex items-center justify-between">
                  <span className="text-xs text-muted-foreground">{acct.email}</span>
                  <DisconnectButton type="microsoft" id={acct.email} />
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Slack */}
        <div className="rounded-lg border border-border bg-card">
          <div className="flex items-center justify-between p-4">
            <div className="flex items-center gap-3">
              <div className="flex h-9 w-9 items-center justify-center rounded-md bg-muted">
                <MessageSquare className="h-5 w-5 text-muted-foreground" />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">Slack</span>
                  {statusBadge("slack")}
                </div>
                <p className="text-xs text-muted-foreground">
                  Read and send Slack messages
                </p>
              </div>
            </div>
            <Button variant="outline" size="sm" onClick={connectSlack}>
              {status.slack.connected ? "Add Workspace" : "Connect Slack"}
            </Button>
          </div>
          {slackWorkspaces.length > 0 && (
            <div className="border-t border-border px-4 py-3 pl-[52px] space-y-1">
              {slackWorkspaces.map((ws) => (
                <div key={ws.team_id} className="flex items-center justify-between">
                  <span className="text-xs text-muted-foreground">{ws.team_name}</span>
                  <DisconnectButton type="slack" id={ws.team_id} />
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Telegram */}
        <div className="rounded-lg border border-border bg-card">
          <div className="flex items-center justify-between p-4">
            <div className="flex items-center gap-3">
              <div className="flex h-9 w-9 items-center justify-center rounded-md bg-muted">
                <Send className="h-5 w-5 text-muted-foreground" />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">Telegram</span>
                  {statusBadge("telegram")}
                </div>
                <p className="text-xs text-muted-foreground">
                  Read and send Telegram messages
                </p>
              </div>
            </div>
            <Button variant="outline" size="sm" onClick={connectTelegram}>
              {status.telegram.connected ? "Add Account" : "Connect Telegram"}
            </Button>
          </div>
          {telegramAccounts.length > 0 && (
            <div className="border-t border-border px-4 py-3 pl-[52px] space-y-1">
              {telegramAccounts.map((acct) => (
                <div key={acct.phone_number} className="flex items-center justify-between">
                  <span className="text-xs text-muted-foreground">
                    {acct.display_name ? `${acct.display_name} (${acct.phone_number})` : acct.phone_number}
                  </span>
                  <DisconnectButton type="telegram" id={acct.phone_number} />
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Location History */}
        <div className="flex items-center justify-between rounded-lg border border-border bg-card p-4">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-md bg-muted">
              <MapPin className="h-5 w-5 text-muted-foreground" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">Location History</span>
                {statusBadge("location")}
              </div>
              <p className="text-xs text-muted-foreground">
                Import location data for context
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
