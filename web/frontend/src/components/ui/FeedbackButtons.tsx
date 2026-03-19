/**
 * FeedbackButtons - Reusable feedback reaction component
 * Phase 17-02
 *
 * Provides thumbs up/down (compact) or helpful/dismiss/snooze (full) variants
 * for giving feedback on nudges and digest intelligence sections.
 */

import { useState } from "react";
import { ThumbsUp, ThumbsDown, Clock, Check, X } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { toast } from "sonner";

export type FeedbackItemType =
  | "nudge"
  | "detected_action"
  | "needs_reply"
  | "unfulfilled_commitment"
  | "cross_source_connection"
  | "open_loop";

interface FeedbackButtonsProps {
  itemType: FeedbackItemType;
  itemId?: number | null;
  variant?: "compact" | "full";
  onFeedback?: (type: string) => void;
  className?: string;
}

interface FeedbackAPIResponse {
  success: boolean;
  feedback_id?: number;
}

/**
 * FeedbackButtons component
 *
 * compact variant: Small thumbs up/down icons (for digest sections)
 * full variant: Labeled Helpful/Not useful/Snooze buttons (for nudges)
 */
export function FeedbackButtons({
  itemType,
  itemId,
  variant = "compact",
  onFeedback,
  className,
}: FeedbackButtonsProps) {
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [showReasonInput, setShowReasonInput] = useState(false);
  const [reasonText, setReasonText] = useState("");
  const [pendingFeedbackType, setPendingFeedbackType] = useState<string | null>(null);

  async function submitFeedback(feedbackType: string, reason?: string) {
    setLoading(true);
    try {
      await api.post<FeedbackAPIResponse>("/api/feedback/react", {
        item_type: itemType,
        item_id: itemId ?? null,
        feedback_type: feedbackType,
        ...(reason ? { reason } : {}),
      });

      setSubmitted(feedbackType);
      setShowReasonInput(false);
      setReasonText("");
      setPendingFeedbackType(null);
      toast.success("Thanks for the feedback!");
      onFeedback?.(feedbackType);
    } catch (error) {
      console.error("Failed to submit feedback:", error);
      toast.error("Failed to submit feedback");
    } finally {
      setLoading(false);
    }
  }

  function handleFeedback(feedbackType: string) {
    if (submitted || loading) return;

    const negativeTypes = ["not_helpful", "thumbs_down"];
    if (negativeTypes.includes(feedbackType)) {
      setPendingFeedbackType(feedbackType);
      setShowReasonInput(true);
    } else {
      submitFeedback(feedbackType);
    }
  }

  // Reason input UI (shown after negative reactions)
  if (showReasonInput && pendingFeedbackType) {
    return (
      <div className={cn("flex flex-col gap-1.5", className)}>
        <textarea
          rows={2}
          placeholder="What was wrong? (optional)"
          value={reasonText}
          onChange={(e) => setReasonText(e.target.value)}
          className="w-full text-xs rounded border border-border bg-background px-2 py-1 text-foreground placeholder:text-muted-foreground resize-none focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="xs"
            className="text-muted-foreground hover:text-foreground"
            onClick={() => submitFeedback(pendingFeedbackType, reasonText || undefined)}
            disabled={loading}
          >
            Send
          </Button>
          <button
            className="text-xs text-muted-foreground hover:text-foreground underline underline-offset-2"
            onClick={() => submitFeedback(pendingFeedbackType)}
            disabled={loading}
          >
            Skip
          </button>
        </div>
      </div>
    );
  }

  // Compact variant: thumbs up/down only
  if (variant === "compact") {
    if (submitted) {
      return (
        <div className={cn("flex items-center gap-1 text-xs text-muted-foreground", className)}>
          <Check className="h-3 w-3 text-green-500" />
          <span>Noted</span>
        </div>
      );
    }

    return (
      <div className={cn("flex items-center gap-0.5", className)}>
        <Button
          variant="ghost"
          size="icon-xs"
          className="text-muted-foreground hover:text-green-400 hover:bg-green-500/10"
          onClick={() => handleFeedback("helpful")}
          disabled={loading}
          title="Helpful"
        >
          <ThumbsUp className="h-3 w-3" />
        </Button>
        <Button
          variant="ghost"
          size="icon-xs"
          className="text-muted-foreground hover:text-red-400 hover:bg-red-500/10"
          onClick={() => handleFeedback("not_helpful")}
          disabled={loading}
          title="Not helpful"
        >
          <ThumbsDown className="h-3 w-3" />
        </Button>
      </div>
    );
  }

  // Full variant: Helpful / Not useful / Snooze buttons with labels
  if (submitted) {
    const label =
      submitted === "helpful"
        ? "Helpful"
        : submitted === "not_helpful"
          ? "Not useful"
          : "Snoozed";
    return (
      <div className={cn("flex items-center gap-1.5 text-xs text-muted-foreground", className)}>
        <Check className="h-3.5 w-3.5 text-green-500" />
        <span>{label}</span>
      </div>
    );
  }

  return (
    <div className={cn("flex items-center gap-1.5", className)}>
      <Button
        variant="ghost"
        size="xs"
        className="text-green-400 hover:text-green-300 hover:bg-green-500/10"
        onClick={() => handleFeedback("helpful")}
        disabled={loading}
      >
        <ThumbsUp className="h-3.5 w-3.5" />
        Helpful
      </Button>
      <Button
        variant="ghost"
        size="xs"
        className="text-muted-foreground hover:text-red-400 hover:bg-red-500/10"
        onClick={() => handleFeedback("not_helpful")}
        disabled={loading}
      >
        <X className="h-3.5 w-3.5" />
        Not useful
      </Button>
      <Button
        variant="ghost"
        size="xs"
        className="text-muted-foreground hover:text-yellow-400 hover:bg-yellow-500/10"
        onClick={() => handleFeedback("snooze")}
        disabled={loading}
      >
        <Clock className="h-3.5 w-3.5" />
        Snooze
      </Button>
    </div>
  );
}
