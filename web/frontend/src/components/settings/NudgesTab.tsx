import { useState, useEffect } from "react";
import { api } from "@/lib/api";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { toast } from "sonner";

const HOURS = Array.from({ length: 24 }, (_, i) => {
  const h = String(i).padStart(2, "0");
  const ampm = i < 12 ? "AM" : "PM";
  const display = i === 0 ? 12 : i > 12 ? i - 12 : i;
  return { value: h, label: `${display} ${ampm}` };
});

const MINUTES = Array.from({ length: 12 }, (_, i) => {
  const m = String(i * 5).padStart(2, "0");
  return { value: m, label: `:${m}` };
});

function TimeSelect({
  value,
  onChange,
  disabled,
}: {
  value: string;
  onChange: (time: string) => void;
  disabled?: boolean;
}) {
  const [hour, minute] = value.split(":");
  return (
    <div className="flex gap-2">
      <Select
        value={hour}
        onValueChange={(h) => onChange(`${h}:${minute}`)}
        disabled={disabled}
      >
        <SelectTrigger className="w-28">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {HOURS.map((h) => (
            <SelectItem key={h.value} value={h.value}>
              {h.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Select
        value={minute}
        onValueChange={(m) => onChange(`${hour}:${m}`)}
        disabled={disabled}
      >
        <SelectTrigger className="w-20">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {MINUTES.map((m) => (
            <SelectItem key={m.value} value={m.value}>
              {m.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

interface NudgePrefs {
  nudge_enabled: boolean;
  nudge_quiet_start: string;
  nudge_quiet_end: string;
  nudge_max_urgent_per_hour: number;
  nudge_batch_interval_minutes: number;
  nudge_channels: string[];
  nudge_batch_channel: string;
  pending_action_notification_channel: string;
  nudge_quiet_skip_weekend: boolean;
}

interface LearnedPatterns {
  responsive_hours: number[];
  item_type_preferences: Record<string, number>;
  last_computed_at: string | null;
  has_data: boolean;
}

interface IntegrationStatus {
  telegram: boolean;
  slack: boolean;
}

const CHANNELS = [
  { value: "push", label: "Push Notification" },
  { value: "telegram", label: "Telegram" },
  { value: "slack", label: "Slack" },
  { value: "email", label: "Email" },
];

// Item type display names
const ITEM_TYPE_LABELS: Record<string, string> = {
  detected_action: "Detected Actions",
  needs_reply: "Needs Your Reply",
  unfulfilled_commitment: "Unfulfilled Commitments",
  cross_source_connection: "Cross-Source Connections",
  open_loop: "Open Loops",
  task_reminder: "Task Reminders",
  nudge: "General Nudges",
};

function formatHour(hour: number): string {
  const ampm = hour < 12 ? "AM" : "PM";
  const display = hour === 0 ? 12 : hour > 12 ? hour - 12 : hour;
  return `${display} ${ampm}`;
}

function formatItemType(type: string): string {
  return ITEM_TYPE_LABELS[type] || type.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

function getPreferenceBadge(score: number): { variant: "default" | "secondary" | "destructive" | "outline"; label: string } {
  if (score > 2) return { variant: "default", label: "More" };
  if (score < -2) return { variant: "destructive", label: "Less" };
  return { variant: "secondary", label: "Neutral" };
}

export function NudgesTab() {
  const [prefs, setPrefs] = useState<NudgePrefs>({
    nudge_enabled: true,
    nudge_quiet_start: "22:00",
    nudge_quiet_end: "08:00",
    nudge_max_urgent_per_hour: 3,
    nudge_batch_interval_minutes: 180,
    nudge_channels: ["push"],
    nudge_batch_channel: "push",
    pending_action_notification_channel: "none",
    nudge_quiet_skip_weekend: false,
  });
  const [patterns, setPatterns] = useState<LearnedPatterns>({
    responsive_hours: [],
    item_type_preferences: {},
    last_computed_at: null,
    has_data: false,
  });
  const [integrations, setIntegrations] = useState<IntegrationStatus>({
    telegram: false,
    slack: false,
  });
  const [loading, setLoading] = useState(true);
  const [resetting, setResetting] = useState(false);

  useEffect(() => {
    loadPrefs();
    loadPatterns();
    loadIntegrations();
  }, []);

  async function loadPrefs() {
    try {
      const data = await api.get<NudgePrefs>("/api/nudges/preferences");
      setPrefs(data);
    } catch {
      // Use defaults
    } finally {
      setLoading(false);
    }
  }

  async function loadPatterns() {
    try {
      const data = await api.get<LearnedPatterns>("/api/settings/patterns");
      setPatterns(data);
    } catch {
      // Keep defaults (no data)
    }
  }

  async function loadIntegrations() {
    try {
      const [telegram, slack] = await Promise.all([
        api.get<{ connected: boolean }>("/api/telegram/status").catch(() => ({ connected: false })),
        api.get<{ connected: boolean }>("/api/slack/status").catch(() => ({ connected: false })),
      ]);
      setIntegrations({
        telegram: telegram.connected,
        slack: slack.connected,
      });
    } catch {
      // Keep defaults (not connected)
    }
  }

  async function savePrefs(updates: Partial<NudgePrefs>) {
    const updated = { ...prefs, ...updates };
    setPrefs(updated);
    try {
      await api.put("/api/nudges/preferences", {
        nudge_enabled: updated.nudge_enabled,
        nudge_quiet_start: updated.nudge_quiet_start,
        nudge_quiet_end: updated.nudge_quiet_end,
        nudge_max_urgent_per_hour: updated.nudge_max_urgent_per_hour,
        nudge_batch_interval_minutes: updated.nudge_batch_interval_minutes,
        nudge_channels: updated.nudge_channels,
        nudge_batch_channel: updated.nudge_batch_channel,
        pending_action_notification_channel: updated.pending_action_notification_channel,
        nudge_quiet_skip_weekend: updated.nudge_quiet_skip_weekend,
      });
      toast.success("Nudge settings saved");
    } catch {
      toast.error("Failed to save nudge settings");
    }
  }

  async function resetPatterns() {
    setResetting(true);
    try {
      await api.post("/api/settings/patterns/reset");
      setPatterns({
        responsive_hours: [],
        item_type_preferences: {},
        last_computed_at: null,
        has_data: false,
      });
      toast.success("Learned preferences reset");
    } catch {
      toast.error("Failed to reset preferences");
    } finally {
      setResetting(false);
    }
  }

  function getChannelLabel(channel: string): string {
    const ch = CHANNELS.find((c) => c.value === channel);
    if (!ch) return channel;

    // Add "(not connected)" for disconnected integrations
    if (channel === "telegram" && !integrations.telegram) {
      return `${ch.label} (not connected)`;
    }
    if (channel === "slack" && !integrations.slack) {
      return `${ch.label} (not connected)`;
    }
    return ch.label;
  }

  function getUrgentChannel(): string {
    return prefs.nudge_channels[0] || "push";
  }

  function setUrgentChannel(channel: string) {
    const newChannels = [channel, ...prefs.nudge_channels.slice(1)];
    savePrefs({ nudge_channels: newChannels });
  }

  // Convert batch interval from minutes to hours for display
  const batchIntervalHours = Math.round(prefs.nudge_batch_interval_minutes / 60);

  if (loading) {
    return (
      <div className="space-y-4">
        <div className="h-6 w-48 animate-pulse rounded bg-muted" />
        <div className="h-32 animate-pulse rounded bg-muted" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h3 className="text-lg font-semibold">Autonomous Nudges</h3>
        <p className="text-sm text-muted-foreground">
          Proactive notifications for urgent items and batch summaries.
        </p>
      </div>

      {/* Enable toggle */}
      <div className="flex items-center justify-between">
        <Label>Enable Nudges</Label>
        <Switch
          checked={prefs.nudge_enabled}
          onCheckedChange={(v) => savePrefs({ nudge_enabled: v })}
        />
      </div>

      {prefs.nudge_enabled && (
        <>
          <Separator />

          {/* Channel preferences */}
          <div className="space-y-3">
            <h4 className="text-sm font-medium">Channel Preferences</h4>

            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">
                Urgent nudge channel
              </Label>
              <Select
                value={getUrgentChannel()}
                onValueChange={setUrgentChannel}
              >
                <SelectTrigger className="w-56">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {CHANNELS.map((ch) => (
                    <SelectItem key={ch.value} value={ch.value}>
                      {getChannelLabel(ch.value)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">
                Batch nudge channel
              </Label>
              <Select
                value={prefs.nudge_batch_channel}
                onValueChange={(v) => savePrefs({ nudge_batch_channel: v })}
              >
                <SelectTrigger className="w-56">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {CHANNELS.map((ch) => (
                    <SelectItem key={ch.value} value={ch.value}>
                      {getChannelLabel(ch.value)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <p className="text-xs text-muted-foreground border-l-2 border-border pl-3 py-1">
              Nudges go to private messages (Telegram Saved Messages, Slack self-DM)
            </p>
          </div>

          <Separator />

          {/* Pending Actions Notifications */}
          <div className="space-y-3">
            <div>
              <h3 className="font-medium text-sm">Pending Actions Notifications</h3>
              <p className="text-xs text-muted-foreground mt-0.5">
                Get notified when Seny queues an action for your approval (email drafts, calendar proposals, tasks).
              </p>
            </div>

            <div className="flex items-center justify-between">
              <Label className="text-sm">Notify via</Label>
              <Select
                value={prefs.pending_action_notification_channel}
                onValueChange={(v) => savePrefs({ pending_action_notification_channel: v })}
              >
                <SelectTrigger className="w-36">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">Off</SelectItem>
                  <SelectItem value="telegram">
                    Telegram{!integrations.telegram ? " (not connected)" : ""}
                  </SelectItem>
                  <SelectItem value="slack">
                    Slack{!integrations.slack ? " (not connected)" : ""}
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          <Separator />

          {/* Quiet hours */}
          <div className="space-y-3">
            <h4 className="text-sm font-medium">Quiet Hours</h4>

            <div className="flex items-center gap-4">
              <div className="space-y-1">
                <Label className="text-xs text-muted-foreground">Start</Label>
                <TimeSelect
                  value={prefs.nudge_quiet_start}
                  onChange={(v) => savePrefs({ nudge_quiet_start: v })}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-muted-foreground">End</Label>
                <TimeSelect
                  value={prefs.nudge_quiet_end}
                  onChange={(v) => savePrefs({ nudge_quiet_end: v })}
                />
              </div>
            </div>

            <div className="flex items-center justify-between">
              <div>
                <Label>Skip weekends</Label>
                <p className="text-sm text-muted-foreground">
                  Don't send nudges on Saturday and Sunday
                </p>
              </div>
              <Switch
                checked={prefs.nudge_quiet_skip_weekend}
                onCheckedChange={(checked) =>
                  savePrefs({ nudge_quiet_skip_weekend: checked })
                }
              />
            </div>

            <p className="text-xs text-muted-foreground">
              No nudges during quiet hours. Uses your digest timezone.
            </p>
          </div>

          <Separator />

          {/* Rate limiting */}
          <div className="space-y-3">
            <h4 className="text-sm font-medium">Rate Limiting</h4>

            <div className="flex items-center gap-4">
              <div className="space-y-1">
                <Label className="text-xs text-muted-foreground">
                  Max urgent/hour
                </Label>
                <Input
                  type="number"
                  min={1}
                  max={20}
                  value={prefs.nudge_max_urgent_per_hour}
                  onChange={(e) => {
                    const val = parseInt(e.target.value, 10);
                    if (!isNaN(val) && val >= 1 && val <= 20) {
                      savePrefs({ nudge_max_urgent_per_hour: val });
                    }
                  }}
                  className="w-20"
                />
              </div>

              <div className="space-y-1">
                <Label className="text-xs text-muted-foreground">
                  Hours between batches
                </Label>
                <Input
                  type="number"
                  min={1}
                  max={12}
                  value={batchIntervalHours}
                  onChange={(e) => {
                    const val = parseInt(e.target.value, 10);
                    if (!isNaN(val) && val >= 1 && val <= 12) {
                      savePrefs({ nudge_batch_interval_minutes: val * 60 });
                    }
                  }}
                  className="w-20"
                />
              </div>
            </div>
          </div>

          <Separator />

          {/* Learned Preferences */}
          <div className="space-y-4">
            <div>
              <h4 className="text-sm font-medium">Learned Preferences</h4>
              <p className="text-xs text-muted-foreground">
                Based on your feedback, Seny has learned these preferences.
                {patterns.last_computed_at && (
                  <span className="ml-1">
                    Last updated: {new Date(patterns.last_computed_at).toLocaleDateString()}
                  </span>
                )}
              </p>
            </div>

            {!patterns.has_data ? (
              <p className="text-sm text-muted-foreground py-4 text-center border border-dashed rounded-md">
                Not enough data yet. Give feedback on nudges and digest items to help Seny learn your preferences.
              </p>
            ) : (
              <div className="space-y-4">
                {/* Responsive Hours */}
                {patterns.responsive_hours.length > 0 && (
                  <div className="space-y-2">
                    <Label className="text-xs text-muted-foreground">Most responsive hours</Label>
                    <p className="text-sm">
                      {patterns.responsive_hours.map(h => formatHour(h)).join(", ")}
                    </p>
                  </div>
                )}

                {/* Item Type Preferences */}
                {Object.keys(patterns.item_type_preferences).length > 0 && (
                  <div className="space-y-2">
                    <Label className="text-xs text-muted-foreground">Item type preferences</Label>
                    <div className="space-y-1">
                      {Object.entries(patterns.item_type_preferences)
                        .sort(([, a], [, b]) => b - a)
                        .map(([type, score]) => {
                          const badge = getPreferenceBadge(score);
                          return (
                            <div key={type} className="flex items-center justify-between py-1">
                              <span className="text-sm">{formatItemType(type)}</span>
                              <Badge variant={badge.variant}>{badge.label}</Badge>
                            </div>
                          );
                        })}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Reset button */}
            <Button
              variant="outline"
              size="sm"
              onClick={resetPatterns}
              disabled={resetting || !patterns.has_data}
            >
              {resetting ? "Resetting..." : "Reset learned preferences"}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
