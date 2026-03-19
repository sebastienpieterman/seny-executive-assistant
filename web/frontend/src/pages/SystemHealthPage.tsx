import { useEffect, useState } from "react";
import { TOKEN_KEY } from "@/lib/api";
import {
  Card,
  CardContent,
  CardHeader,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface SubsystemHealth {
  subsystem: string;
  status: "green" | "yellow" | "red";
  last_run_at: string | null;
  minutes_ago: number | null;
  threshold_minutes: number;
  last_error: string | null;
}

interface SystemsHealthResponse {
  subsystems: SubsystemHealth[];
  checked_at: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relativeTime(isoString: string): string {
  const now = Date.now();
  const then = new Date(isoString).getTime();
  const diffMs = now - then;
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return "just now";
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin} minute${diffMin !== 1 ? "s" : ""} ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr} hour${diffHr !== 1 ? "s" : ""} ago`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay} day${diffDay !== 1 ? "s" : ""} ago`;
}

function formatSubsystemName(name: string): string {
  return name
    .split("-")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function formatThreshold(minutes: number): string {
  if (minutes >= 60) {
    const hours = Math.round(minutes / 60);
    return `Expected every ${hours} hour${hours !== 1 ? "s" : ""}`;
  }
  return `Expected every ${minutes} min`;
}

function statusBadgeClass(status: "green" | "yellow" | "red"): string {
  switch (status) {
    case "green":
      return "bg-green-500/20 text-green-400 border-green-500/30";
    case "yellow":
      return "bg-yellow-500/20 text-yellow-400 border-yellow-500/30";
    case "red":
      return "bg-red-500/20 text-red-400 border-red-500/30";
  }
}

function statusLabel(status: "green" | "yellow" | "red"): string {
  switch (status) {
    case "green":
      return "Healthy";
    case "yellow":
      return "Stale";
    case "red":
      return "Down";
  }
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function SystemHealthPage() {
  const [data, setData] = useState<SystemsHealthResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function fetchHealth() {
    const token = localStorage.getItem(TOKEN_KEY);
    try {
      const res = await fetch("/api/qa/systems-health", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `HTTP ${res.status}`);
      }
      const json = (await res.json()) as SystemsHealthResponse;
      setData(json);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load health data");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchHealth();
    const interval = setInterval(fetchHealth, 60_000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="flex flex-col gap-6 p-4 md:p-6 max-w-4xl mx-auto w-full">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight">System Health</h1>
        <p className="text-muted-foreground text-sm mt-1">
          {data
            ? `Last checked: ${relativeTime(data.checked_at)}`
            : loading
            ? "Loading..."
            : "Status of all background jobs"}
        </p>
      </div>

      {/* Loading state */}
      {loading && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <Card key={i} className="animate-pulse">
              <CardHeader className="pb-2">
                <div className="flex items-center justify-between">
                  <div className="h-4 bg-muted rounded w-1/2" />
                  <div className="h-5 bg-muted rounded w-16" />
                </div>
              </CardHeader>
              <CardContent>
                <div className="h-4 bg-muted rounded w-1/3 mb-1" />
                <div className="h-3 bg-muted rounded w-1/2" />
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Error state */}
      {!loading && error && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
          {error}
        </div>
      )}

      {/* Success state */}
      {!loading && !error && data && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {data.subsystems.map((sub) => (
            <Card key={sub.subsystem}>
              <CardHeader className="pb-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-semibold text-sm leading-snug">
                    {formatSubsystemName(sub.subsystem)}
                  </span>
                  <Badge
                    variant="outline"
                    className={statusBadgeClass(sub.status)}
                  >
                    {statusLabel(sub.status)}
                  </Badge>
                </div>
              </CardHeader>
              <CardContent>
                <div className="flex flex-col gap-1">
                  <p className="text-sm text-muted-foreground">
                    {sub.last_run_at
                      ? `Last run: ${relativeTime(sub.last_run_at)}`
                      : "Never ran"}
                  </p>
                  <p className="text-xs text-muted-foreground/70">
                    {formatThreshold(sub.threshold_minutes)}
                  </p>
                  {sub.last_error && (
                    <p className="text-xs text-red-400/80 mt-1 break-words">
                      {sub.last_error}
                    </p>
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
