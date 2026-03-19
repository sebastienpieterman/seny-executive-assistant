import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { toast } from "sonner";
import { Trash2, Mail, MessageSquare, Send } from "lucide-react";

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
}: {
  value: string;
  onChange: (time: string) => void;
}) {
  const [hour, minute] = value.split(":");
  return (
    <div className="flex gap-2">
      <Select
        value={hour}
        onValueChange={(h) => onChange(`${h}:${minute}`)}
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

interface DigestPrefs {
  digest_enabled: boolean;
  digest_time: string;
  digest_email: boolean;
  digest_push: boolean;
  digest_timezone: string;
}

interface WeeklyPrefs {
  weekly_review_enabled: boolean;
  weekly_review_day: string;
  weekly_review_time: string;
  timezone: string;
}

interface IgnoredSender {
  id: number;
  source_type: string;
  sender_identifier: string;
  ignored_at: string;
}

const DAYS = [
  "sunday",
  "monday",
  "tuesday",
  "wednesday",
  "thursday",
  "friday",
  "saturday",
];

export function DigestTab() {
  const [digest, setDigest] = useState<DigestPrefs>({
    digest_enabled: true,
    digest_time: "07:00",
    digest_email: true,
    digest_push: true,
    digest_timezone: "America/Chicago",
  });
  const [weekly, setWeekly] = useState<WeeklyPrefs>({
    weekly_review_enabled: true,
    weekly_review_day: "sunday",
    weekly_review_time: "18:00",
    timezone: "America/Chicago",
  });
  const [digestLoading, setDigestLoading] = useState(true);
  const [weeklyLoading, setWeeklyLoading] = useState(true);
  const [previewingDigest, setPreviewingDigest] = useState(false);
  const [previewingWeekly, setPreviewingWeekly] = useState(false);
  const [ignoredSenders, setIgnoredSenders] = useState<IgnoredSender[]>([]);
  const [ignoredLoading, setIgnoredLoading] = useState(true);
  const [removingId, setRemovingId] = useState<number | null>(null);

  useEffect(() => {
    loadDigest();
    loadWeekly();
    loadIgnoredSenders();
  }, []);

  async function loadDigest() {
    try {
      const data = await api.get<DigestPrefs>("/api/settings/digest");
      setDigest(data);
    } catch {
      // Use defaults
    } finally {
      setDigestLoading(false);
    }
  }

  async function loadWeekly() {
    try {
      const data = await api.get<WeeklyPrefs>("/api/settings/weekly-review");
      setWeekly(data);
    } catch {
      // Use defaults
    } finally {
      setWeeklyLoading(false);
    }
  }

  async function loadIgnoredSenders() {
    try {
      const data = await api.get<{ senders: IgnoredSender[]; count: number }>(
        "/api/feedback/ignored-senders"
      );
      setIgnoredSenders(data.senders);
    } catch {
      // Empty list on error
      setIgnoredSenders([]);
    } finally {
      setIgnoredLoading(false);
    }
  }

  async function removeIgnoredSender(sender: IgnoredSender) {
    setRemovingId(sender.id);
    try {
      await api.delete("/api/feedback/ignored-senders", {
        source_type: sender.source_type,
        sender_identifier: sender.sender_identifier,
      });
      setIgnoredSenders((prev) => prev.filter((s) => s.id !== sender.id));
      toast.success(`Removed ${sender.sender_identifier} from ignore list`);
    } catch {
      toast.error("Failed to remove ignored sender");
    } finally {
      setRemovingId(null);
    }
  }

  async function saveDigest(updates: Partial<DigestPrefs>) {
    const updated = { ...digest, ...updates };
    setDigest(updated);
    try {
      await api.put("/api/settings/digest", {
        digest_enabled: updated.digest_enabled,
        digest_time: updated.digest_time,
        digest_email: updated.digest_email,
        digest_push: updated.digest_push,
        digest_timezone: updated.digest_timezone,
      });
      toast.success("Digest settings saved");
    } catch {
      toast.error("Failed to save digest settings");
    }
  }

  async function saveWeekly(updates: Partial<WeeklyPrefs>) {
    const updated = { ...weekly, ...updates };
    setWeekly(updated);
    try {
      await api.put("/api/settings/weekly-review", {
        weekly_review_enabled: updated.weekly_review_enabled,
        weekly_review_day: updated.weekly_review_day,
        weekly_review_time: updated.weekly_review_time,
      });
      toast.success("Weekly review settings saved");
    } catch {
      toast.error("Failed to save weekly review settings");
    }
  }

  async function previewDigest() {
    setPreviewingDigest(true);
    try {
      await api.post("/api/settings/digest/send-now");
      toast.success("Digest sent! Check your email/notifications.");
    } catch {
      toast.error("Failed to generate digest preview");
    } finally {
      setPreviewingDigest(false);
    }
  }

  async function previewWeekly() {
    setPreviewingWeekly(true);
    try {
      await api.post("/api/settings/weekly-review/send-now");
      toast.success("Weekly review sent! Check your email/notifications.");
    } catch {
      toast.error("Failed to generate weekly review");
    } finally {
      setPreviewingWeekly(false);
    }
  }

  if (digestLoading || weeklyLoading) {
    return (
      <div className="space-y-4">
        <div className="h-6 w-48 animate-pulse rounded bg-muted" />
        <div className="h-32 animate-pulse rounded bg-muted" />
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {/* Daily Digest */}
      <div>
        <h3 className="text-lg font-semibold">Daily Digest</h3>
        <p className="text-sm text-muted-foreground">
          Get a morning briefing of your priorities, calendar, and follow-ups.
        </p>
      </div>

      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <Label>Enable Daily Digest</Label>
          <Switch
            checked={digest.digest_enabled}
            onCheckedChange={(v) => saveDigest({ digest_enabled: v })}
          />
        </div>

        {digest.digest_enabled && (
          <>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">
                Delivery Time
              </Label>
              <TimeSelect
                value={digest.digest_time}
                onChange={(time) => saveDigest({ digest_time: time })}
              />
            </div>

            <div className="flex items-center justify-between">
              <Label className="text-xs text-muted-foreground">
                Email delivery
              </Label>
              <Switch
                size="sm"
                checked={digest.digest_email}
                onCheckedChange={(v) => saveDigest({ digest_email: v })}
              />
            </div>

            <div className="flex items-center justify-between">
              <Label className="text-xs text-muted-foreground">
                Push notification
              </Label>
              <Switch
                size="sm"
                checked={digest.digest_push}
                onCheckedChange={(v) => saveDigest({ digest_push: v })}
              />
            </div>

            <Button
              variant="outline"
              size="sm"
              onClick={previewDigest}
              disabled={previewingDigest}
            >
              {previewingDigest ? "Sending..." : "Send Digest Now"}
            </Button>
          </>
        )}
      </div>

      <Separator />

      {/* Weekly Review */}
      <div>
        <h3 className="text-lg font-semibold">Weekly Review</h3>
        <p className="text-sm text-muted-foreground">
          A weekly summary of accomplishments, patterns, and upcoming focus
          areas.
        </p>
      </div>

      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <Label>Enable Weekly Review</Label>
          <Switch
            checked={weekly.weekly_review_enabled}
            onCheckedChange={(v) =>
              saveWeekly({ weekly_review_enabled: v })
            }
          />
        </div>

        {weekly.weekly_review_enabled && (
          <>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">Day</Label>
              <Select
                value={weekly.weekly_review_day}
                onValueChange={(v) => saveWeekly({ weekly_review_day: v })}
              >
                <SelectTrigger className="w-40">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {DAYS.map((d) => (
                    <SelectItem key={d} value={d}>
                      {d.charAt(0).toUpperCase() + d.slice(1)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">Time</Label>
              <TimeSelect
                value={weekly.weekly_review_time}
                onChange={(time) =>
                  saveWeekly({ weekly_review_time: time })
                }
              />
            </div>

            <Button
              variant="outline"
              size="sm"
              onClick={previewWeekly}
              disabled={previewingWeekly}
            >
              {previewingWeekly ? "Sending..." : "Send Weekly Review Now"}
            </Button>
          </>
        )}
      </div>

      <Separator />

      {/* Ignored Senders */}
      <div>
        <h3 className="text-lg font-semibold">Ignored Senders</h3>
        <p className="text-sm text-muted-foreground">
          Messages from these senders are excluded from your digests.
        </p>
      </div>

      <div className="space-y-2">
        {ignoredLoading ? (
          <div className="h-20 animate-pulse rounded bg-muted" />
        ) : ignoredSenders.length === 0 ? (
          <p className="text-sm text-muted-foreground italic py-4">
            No ignored senders. Click "Ignore sender" in a digest email to add
            one.
          </p>
        ) : (
          <div className="space-y-2">
            {ignoredSenders.map((sender) => (
              <div
                key={sender.id}
                className="flex items-center justify-between rounded-md border border-border bg-card px-3 py-2"
              >
                <div className="flex items-center gap-3">
                  {sender.source_type === "gmail" && (
                    <Mail className="h-4 w-4 text-muted-foreground" />
                  )}
                  {sender.source_type === "slack" && (
                    <MessageSquare className="h-4 w-4 text-muted-foreground" />
                  )}
                  {sender.source_type === "telegram" && (
                    <Send className="h-4 w-4 text-muted-foreground" />
                  )}
                  <div>
                    <p className="text-sm font-medium">
                      {sender.sender_identifier}
                    </p>
                    <p className="text-xs text-muted-foreground capitalize">
                      {sender.source_type}
                    </p>
                  </div>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => removeIgnoredSender(sender)}
                  disabled={removingId === sender.id}
                  className="h-8 w-8 text-muted-foreground hover:text-destructive"
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
