import { useState, useEffect } from "react";
import { api } from "@/lib/api";
import { Label } from "@/components/ui/label";
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
import { Loader2, RefreshCw, RotateCcw } from "lucide-react";

interface ScannerPrefs {
  scanner_gmail_interval_minutes: number;
  scanner_slack_interval_minutes: number;
  scanner_telegram_interval_minutes: number;
  scanner_calendar_interval_minutes: number;
  classification_tier: string;
}

// Interval options with human-readable labels
const INTERVAL_OPTIONS = [
  { value: 5, label: "Every 5 min" },
  { value: 15, label: "Every 15 min" },
  { value: 30, label: "Every 30 min" },
  { value: 60, label: "Every hour" },
  { value: 120, label: "Every 2 hours" },
  { value: 240, label: "Every 4 hours" },
  { value: 480, label: "Every 8 hours" },
  { value: 1440, label: "Every 24 hours" },
];

// Source display info
const SOURCES = [
  {
    key: "scanner_gmail_interval_minutes" as const,
    label: "Gmail",
    description: "Email scanning frequency",
  },
  {
    key: "scanner_slack_interval_minutes" as const,
    label: "Slack",
    description: "Slack message scanning frequency",
  },
  {
    key: "scanner_telegram_interval_minutes" as const,
    label: "Telegram",
    description: "Telegram message scanning frequency",
  },
  {
    key: "scanner_calendar_interval_minutes" as const,
    label: "Calendar",
    description: "Calendar event scanning frequency",
  },
];

export function ScannerTab() {
  const [prefs, setPrefs] = useState<ScannerPrefs>({
    scanner_gmail_interval_minutes: 15,
    scanner_slack_interval_minutes: 120,
    scanner_telegram_interval_minutes: 5,
    scanner_calendar_interval_minutes: 60,
    classification_tier: "haiku",
  });
  const [loading, setLoading] = useState(true);
  const [scanLoading, setScanLoading] = useState(false);
  const [resetLoading, setResetLoading] = useState(false);

  useEffect(() => {
    loadPrefs();
  }, []);

  async function loadPrefs() {
    try {
      const data = await api.get<ScannerPrefs>("/api/settings/scanner");
      setPrefs(data);
    } catch {
      // Use defaults
    } finally {
      setLoading(false);
    }
  }

  async function savePrefs(updates: Partial<ScannerPrefs>) {
    const updated = { ...prefs, ...updates };
    setPrefs(updated);
    try {
      await api.put("/api/settings/scanner", updates);
      toast.success("Scanner settings saved");
    } catch {
      toast.error("Failed to save scanner settings");
    }
  }

  async function handleScanNow() {
    setScanLoading(true);
    try {
      await api.post("/api/scanner/scan", { source: "all" });
      toast.success("Scan started — new items will appear within a minute or two.");
    } catch {
      toast.error("Scan failed. Please try again.");
    } finally {
      setScanLoading(false);
    }
  }

  async function handleReset() {
    setResetLoading(true);
    try {
      const data = await api.post<{ reset_count: number; message: string }>("/api/scanner/reset", {});
      if (data.reset_count === 0) {
        toast.success("No stuck scanners found");
      } else {
        toast.success(`Reset ${data.reset_count} stuck scanner${data.reset_count === 1 ? "" : "s"}`);
      }
    } catch {
      toast.error("Reset failed. Please try again.");
    } finally {
      setResetLoading(false);
    }
  }

  function getIntervalLabel(minutes: number): string {
    const option = INTERVAL_OPTIONS.find((o) => o.value === minutes);
    return option?.label || `Every ${minutes} min`;
  }

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
        <h3 className="text-lg font-semibold">Scanner Settings</h3>
        <p className="text-sm text-muted-foreground">
          Control how often Seny scans your connected sources for new items.
        </p>
      </div>

      <Separator />

      {/* Manual Controls */}
      <div className="space-y-3">
        <h4 className="text-sm font-medium">Manual Controls</h4>
        <p className="text-xs text-muted-foreground">
          Trigger a scan immediately or unstick scanners that got stuck.
        </p>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={handleScanNow}
            disabled={scanLoading || resetLoading}
          >
            {scanLoading ? (
              <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="mr-2 h-3.5 w-3.5" />
            )}
            Scan All Sources Now
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={handleReset}
            disabled={scanLoading || resetLoading}
          >
            {resetLoading ? (
              <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
            ) : (
              <RotateCcw className="mr-2 h-3.5 w-3.5" />
            )}
            Reset Stuck Scanners
          </Button>
        </div>
      </div>

      <Separator />

      {/* Source Frequency Controls */}
      <div className="space-y-4">
        <h4 className="text-sm font-medium">Scan Frequency</h4>
        <p className="text-xs text-muted-foreground">
          How often each source is scanned for new messages and events.
        </p>

        <div className="space-y-3">
          {SOURCES.map((source) => (
            <div
              key={source.key}
              className="flex items-center justify-between py-2"
            >
              <div className="space-y-0.5">
                <Label className="text-sm">{source.label}</Label>
                <p className="text-xs text-muted-foreground">
                  {source.description}
                </p>
              </div>
              <Select
                value={String(prefs[source.key])}
                onValueChange={(v) =>
                  savePrefs({ [source.key]: parseInt(v, 10) })
                }
              >
                <SelectTrigger className="w-40">
                  <SelectValue>
                    {getIntervalLabel(prefs[source.key])}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {INTERVAL_OPTIONS.map((option) => (
                    <SelectItem key={option.value} value={String(option.value)}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ))}
        </div>
      </div>

      <Separator />

      {/* AI Analysis Tier */}
      <div className="space-y-4">
        <h4 className="text-sm font-medium">AI Analysis</h4>
        <p className="text-xs text-muted-foreground">
          Choose the AI model used for classifying scanned items.
        </p>

        <div className="flex items-center justify-between py-2">
          <div className="space-y-0.5">
            <Label className="text-sm">Classification Model</Label>
            <p className="text-xs text-muted-foreground">
              Affects how items are analyzed and prioritized
            </p>
          </div>
          <Select
            value={prefs.classification_tier}
            onValueChange={(v: string) => savePrefs({ classification_tier: v })}
          >
            <SelectTrigger className="w-56">
              <SelectValue>
                {prefs.classification_tier === "full"
                  ? "Thorough (Sonnet)"
                  : "Fast & Economical (Haiku)"}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="haiku">
                Fast & Economical (Haiku)
              </SelectItem>
              <SelectItem value="full">
                Thorough (Sonnet)
              </SelectItem>
            </SelectContent>
          </Select>
        </div>

        <p className="text-xs text-muted-foreground border-l-2 border-border pl-3 py-1">
          Haiku is recommended for most users. Sonnet provides deeper analysis
          but costs more.
        </p>
      </div>
    </div>
  );
}
