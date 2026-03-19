import { useState, useEffect } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";

// ============================================================================
// Types
// ============================================================================

interface Preference {
  item_type: string;
  label: string;
  score: number;
  score_label: string;
  suppressed: boolean;
  override_active: boolean;
}

interface FeedbackStats {
  total: number;
  by_item_type: Record<string, number>;
  by_feedback_type: Record<string, number>;
}

interface PatternsResponse {
  preferences: Preference[];
  suppressed_count: number;
  overridden_count: number;
  feedback_stats: FeedbackStats;
  responsive_hours: number[];
  lessons_learned: Record<string, unknown>;
  last_computed_at: string | null;
  data_quality_note: string | null;
  has_data: boolean;
}

// ============================================================================
// Helpers
// ============================================================================

function formatHour(hour: number): string {
  const ampm = hour < 12 ? "AM" : "PM";
  const display = hour === 0 ? 12 : hour > 12 ? hour - 12 : hour;
  return `${display}:00 ${ampm}`;
}

function scoreColor(scoreLabel: string): string {
  if (scoreLabel === "Well received" || scoreLabel === "Mostly positive") {
    return "text-green-400";
  }
  if (scoreLabel === "Sometimes dismissed") {
    return "text-orange-400";
  }
  return "text-muted-foreground";
}

function feedbackTypeLabel(key: string): string {
  const labels: Record<string, string> = {
    helpful: "helpful",
    not_helpful: "not helpful",
    too_much: "too much",
    snooze: "snoozed",
    accurate: "accurate",
    inaccurate: "inaccurate",
    more_like_this: "more like this",
    less_like_this: "less like this",
    ignore_sender: "ignored sender",
  };
  return labels[key] ?? key.replace(/_/g, " ");
}

// ============================================================================
// Component
// ============================================================================

export function LearningTab() {
  const [data, setData] = useState<PatternsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [resettingType, setResettingType] = useState<string | null>(null);

  useEffect(() => {
    loadPatterns();
  }, []);

  async function loadPatterns() {
    setLoading(true);
    try {
      const result = await api.get<PatternsResponse>("/api/feedback/patterns");
      setData(result);
    } catch {
      toast.error("Failed to load learning data");
    } finally {
      setLoading(false);
    }
  }

  async function handleReset(itemType: string) {
    setResettingType(itemType);
    try {
      await api.delete(`/api/feedback/patterns/${itemType}`);
      toast.success("Seny will resume sending these nudges");
      await loadPatterns();
    } catch {
      toast.error("Failed to reset suppression");
    } finally {
      setResettingType(null);
    }
  }

  // ── Loading state ──────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="space-y-4">
        <div className="h-6 w-48 animate-pulse rounded bg-muted" />
        <div className="h-24 animate-pulse rounded bg-muted" />
        <div className="h-24 animate-pulse rounded bg-muted" />
      </div>
    );
  }

  // ── Empty state ────────────────────────────────────────────────────────────
  if (!data || !data.has_data) {
    return (
      <div className="space-y-6">
        <div>
          <h3 className="text-lg font-semibold">What Seny Learned</h3>
          <p className="text-sm text-muted-foreground mt-1">
            Your feedback shapes which nudges Seny sends and when.
          </p>
        </div>
        <p className="text-sm text-muted-foreground py-6 text-center border border-dashed rounded-md">
          Seny is still getting to know you. Use the 👍 and 👎 buttons on nudges
          and digest items to start building your preferences.
        </p>
      </div>
    );
  }

  const suppressed = data.preferences.filter((p) => p.suppressed);
  const overridden = data.preferences.filter((p) => p.override_active);
  const active = data.preferences.filter((p) => !p.suppressed && !p.override_active);
  const feedbackEntries = Object.entries(data.feedback_stats.by_feedback_type ?? {}).filter(
    ([, count]) => (count as number) > 0
  );

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h3 className="text-lg font-semibold">What Seny Learned</h3>
        <p className="text-sm text-muted-foreground mt-1">
          Your feedback shapes which nudges Seny sends and when.
          {data.last_computed_at && (
            <span className="ml-1">
              Last updated:{" "}
              {new Date(data.last_computed_at).toLocaleDateString("en-US", {
                month: "short",
                day: "numeric",
                year: "numeric",
              })}
              .
            </span>
          )}
        </p>
      </div>

      {/* Data quality note */}
      {data.data_quality_note && (
        <div className="rounded-md border border-yellow-500/30 bg-yellow-500/10 px-4 py-3 text-sm text-yellow-300">
          {data.data_quality_note}
        </div>
      )}

      {/* Feedback signal count */}
      {data.feedback_stats.total > 0 && (
        <p className="text-xs text-muted-foreground">
          Based on {data.feedback_stats.total} feedback signal
          {data.feedback_stats.total !== 1 ? "s" : ""}.
        </p>
      )}

      {/* Feedback breakdown badges */}
      {feedbackEntries.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {feedbackEntries.map(([key, count]) => (
            <span
              key={key}
              className="rounded-full border border-border bg-muted px-2.5 py-0.5 text-xs text-muted-foreground"
            >
              {count as number} {feedbackTypeLabel(key)}
            </span>
          ))}
        </div>
      )}

      {/* Currently Suppressed */}
      {suppressed.length > 0 && (
        <div className="space-y-3">
          <h4 className="text-sm font-medium text-foreground">Currently Suppressed</h4>
          <p className="text-xs text-muted-foreground">
            Seny stopped sending these because you frequently dismissed them. Click
            "Resume sending" to turn them back on.
          </p>
          <div className="space-y-2">
            {suppressed.map((pref) => (
              <div
                key={pref.item_type}
                className="flex items-center justify-between rounded-md border border-border bg-muted/40 px-4 py-3"
              >
                <div className="min-w-0">
                  <p className="text-sm font-medium">{pref.label}</p>
                  <p className="text-xs text-orange-400">{pref.score_label}</p>
                  <p className="text-xs text-muted-foreground/60">
                    score: {pref.score}
                  </p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  className="ml-4 shrink-0"
                  disabled={resettingType === pref.item_type}
                  onClick={() => handleReset(pref.item_type)}
                >
                  {resettingType === pref.item_type ? "Resuming…" : "Resume sending"}
                </Button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Overrides Active */}
      {overridden.length > 0 && (
        <div className="space-y-3">
          <h4 className="text-sm font-medium text-foreground">
            Sending Despite Negative Feedback
          </h4>
          <p className="text-xs text-muted-foreground">
            You've told Seny to keep these active even though the score is low.
          </p>
          <div className="space-y-2">
            {overridden.map((pref) => (
              <div
                key={pref.item_type}
                className="flex items-center justify-between rounded-md border border-border bg-muted/40 px-4 py-3"
              >
                <div className="min-w-0">
                  <p className="text-sm font-medium">{pref.label}</p>
                  <p className="text-xs text-muted-foreground">{pref.score_label}</p>
                  <p className="text-xs text-muted-foreground/60">
                    score: {pref.score}
                  </p>
                </div>
                <span className="ml-4 shrink-0 text-xs text-muted-foreground">
                  Sending anyway
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Active preferences — Your Feedback Signals */}
      {active.length > 0 && (
        <div className="space-y-3">
          <h4 className="text-sm font-medium text-foreground">Your Feedback Signals</h4>
          <div className="space-y-1">
            {active
              .slice()
              .sort((a, b) => b.score - a.score)
              .map((pref) => (
                <div
                  key={pref.item_type}
                  className="flex items-center justify-between py-1.5"
                >
                  <span className="text-sm">{pref.label}</span>
                  <span className={`text-xs ${scoreColor(pref.score_label)}`}>
                    {pref.score_label}
                  </span>
                </div>
              ))}
          </div>
        </div>
      )}

      {/* Responsive Hours */}
      {data.responsive_hours.length > 0 && (
        <div className="space-y-2">
          <h4 className="text-sm font-medium text-foreground">When You're Most Engaged</h4>
          <p className="text-sm text-muted-foreground">
            {data.responsive_hours.map((h) => formatHour(h)).join(", ")}
          </p>
        </div>
      )}
    </div>
  );
}
