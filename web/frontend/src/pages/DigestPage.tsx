import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  RefreshCw,
  Mail,
  MessageSquare,
  Send,
  Calendar,
  Zap,
  Bell,
  AlertTriangle,
  Link2,
  TrendingUp,
  Target,
  Users,
  Trophy,
  ChevronDown,
  ChevronRight,
  CheckCircle2,
  X,
  AlertCircle,
} from "lucide-react";
import { toast } from "sonner";
import { FeedbackButtons } from "@/components/ui/FeedbackButtons";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface NeedsReplyItem {
  source_type: string;
  sender: string;
  subject: string;
  received_at: string | null;
  person_name: string | null;
  person_id: number | null;
  scanned_item_id: number | null;
}

interface DetectedAction {
  action_text: string;
  action_type: string;
  source_type: string;
  source_context: string;
  person_name: string | null;
  person_id: number | null;
  deadline: string | null;
  detected_at: string | null;
  action_id: number | null;
}

interface UnfulfilledCommitment {
  action_text: string;
  source_type: string;
  source_context: string;
  person_name: string | null;
  person_id: number | null;
  committed_at: string | null;
  days_ago: number | null;
  action_id: number | null;
}

interface AttendeeContext {
  name: string;
  email: string;
  last_contact: string | null;
  recent_topic: string | null;
}

interface CalendarEvent {
  summary: string;
  start: string;
  end: string;
  location?: string;
  has_video?: boolean;
  calendar_name?: string;
  id?: string;
  attendee_context: AttendeeContext[];
}

interface Priority {
  next_action: string;
  title: string;
  source: string;
  overdue?: boolean;
}

interface DigestData {
  date: string;
  summary: string;
  top_priorities: Priority[];
  calendar_today: CalendarEvent[];
  relationship_followups: Array<{
    person: string;
    days_since_contact?: number;
    followup?: string;
  }>;
  stuck_items: Array<{ title: string; reason: string }>;
  recent_win: string | null;
  needs_reply: NeedsReplyItem[];
  detected_actions: DetectedAction[];
  unfulfilled_commitments: UnfulfilledCommitment[];
}

interface OpenLoop {
  title: string;
  age_days?: number;
  suggested_action: string;
  linked_person?: string;
  related_activity?: Array<{
    source_type: string;
    preview: string;
    date?: string;
  }>;
}

interface CrossSourceConnection {
  entity_type: string;
  entity_name: string;
  sources: string[];
  source_count: number;
  sample_preview: string;
}

interface WeeklyReviewData {
  week_of: string;
  summary: string;
  what_happened: {
    projects_completed?: string[];
    projects_started?: string[];
    tasks_completed?: number;
    errands_completed?: number;
    people_contacted?: string[];
    ideas_captured?: number;
  };
  open_loops: OpenLoop[];
  cross_source_connections: CrossSourceConnection[];
  patterns_noticed: string[];
  suggested_focus: Array<{ area: string; reason: string }>;
  relationships: {
    contacted_this_week?: string[];
    getting_stale?: Array<{ name: string }>;
  };
  wins_to_celebrate: string[];
}

interface Nudge {
  id: number;
  nudge_type: string;
  channel: string;
  title: string;
  body: string | null;
  urgency: string;
  status: string;
  source_type: string | null;
  created_at: string;
  sent_at: string | null;
  user_response: string | null;
}

interface NudgeListResponse {
  nudges: Nudge[];
  count: number;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function sourceIcon(source: string) {
  switch (source.toLowerCase()) {
    case "gmail":
      return <Mail className="h-4 w-4" />;
    case "slack":
      return <MessageSquare className="h-4 w-4" />;
    case "telegram":
      return <Send className="h-4 w-4" />;
    case "calendar":
      return <Calendar className="h-4 w-4" />;
    default:
      return <Mail className="h-4 w-4" />;
  }
}

function formatTime(isoStr: string): string {
  if (!isoStr.includes("T")) return "All day";
  try {
    const dt = new Date(isoStr);
    return dt.toLocaleTimeString("en-US", {
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return isoStr;
  }
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function DigestSkeleton() {
  return (
    <div className="space-y-4">
      <Skeleton className="h-8 w-64" />
      <Skeleton className="h-20 w-full rounded-xl" />
      <Skeleton className="h-[200px] w-full rounded-xl" />
      <Skeleton className="h-[180px] w-full rounded-xl" />
      <Skeleton className="h-[160px] w-full rounded-xl" />
    </div>
  );
}

function NeedsReplySection({ items }: { items: NeedsReplyItem[] }) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Mail className="h-4 w-4 text-blue-400" />
          <CardTitle className="text-base">Needs Your Reply</CardTitle>
          {items.length > 0 && (
            <Badge variant="secondary" className="text-xs">
              {items.length}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {items.length === 0 ? (
          <p className="py-3 text-center text-sm text-muted-foreground">
            Nothing detected
          </p>
        ) : (
          items.map((item, i) => (
            <div
              key={i}
              className="flex items-start gap-3 rounded-lg border border-blue-500/20 bg-blue-500/5 px-3 py-2.5"
            >
              <div className="mt-0.5 shrink-0 text-blue-400">
                {sourceIcon(item.source_type)}
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium">
                  {item.sender}
                  {item.person_name && (
                    <span className="ml-1.5 text-xs text-muted-foreground">
                      (linked: {item.person_name})
                    </span>
                  )}
                </p>
                <p className="text-sm text-muted-foreground truncate">
                  {item.subject}
                </p>
                <div className="mt-1 flex items-center justify-between">
                  <Badge variant="outline" className="text-[10px] px-1.5 py-0 capitalize">
                    {item.source_type}
                  </Badge>
                  <FeedbackButtons
                    itemType="needs_reply"
                    itemId={item.scanned_item_id}
                    variant="compact"
                  />
                </div>
              </div>
            </div>
          ))
        )}
      </CardContent>
    </Card>
  );
}

function DetectedActionsSection({ items }: { items: DetectedAction[] }) {
  const [dismissed, setDismissed] = useState<Set<number>>(new Set());

  async function handlePromote(actionId: number) {
    try {
      await api.post(`/api/inbound/actions/${actionId}/promote`);
      toast.success("Action promoted to task!");
      setDismissed((prev) => new Set(prev).add(actionId));
    } catch {
      toast.error("Failed to promote action");
    }
  }

  async function handleDismiss(actionId: number) {
    try {
      await api.post(`/api/inbound/actions/${actionId}/dismiss`);
      toast.success("Action dismissed");
      setDismissed((prev) => new Set(prev).add(actionId));
    } catch {
      toast.error("Failed to dismiss action");
    }
  }

  const visible = items.filter((a) => a.action_id && !dismissed.has(a.action_id));

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Zap className="h-4 w-4 text-yellow-400" />
          <CardTitle className="text-base">Detected Actions</CardTitle>
          {visible.length > 0 && (
            <Badge variant="secondary" className="text-xs">
              {visible.length}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {visible.length === 0 ? (
          <p className="py-3 text-center text-sm text-muted-foreground">
            Nothing detected
          </p>
        ) : (
          visible.map((item, i) => (
            <div
              key={item.action_id ?? i}
              className="rounded-lg border border-yellow-500/20 bg-yellow-500/5 px-3 py-2.5"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium">{item.action_text}</p>
                  <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                    <span>From: {item.source_context || item.source_type}</span>
                    {item.action_type && (
                      <Badge variant="outline" className="text-[10px] px-1.5 py-0 capitalize">
                        {item.action_type}
                      </Badge>
                    )}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  {item.action_id && (
                    <>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 text-green-400 hover:text-green-300 hover:bg-green-500/10"
                        title="Promote to task"
                        onClick={() => handlePromote(item.action_id!)}
                      >
                        <CheckCircle2 className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 text-muted-foreground hover:text-red-400 hover:bg-red-500/10"
                        title="Dismiss"
                        onClick={() => handleDismiss(item.action_id!)}
                      >
                        <X className="h-4 w-4" />
                      </Button>
                      <div className="ml-1 border-l border-border/50 pl-1">
                        <FeedbackButtons
                          itemType="detected_action"
                          itemId={item.action_id}
                          variant="compact"
                        />
                      </div>
                    </>
                  )}
                </div>
              </div>
            </div>
          ))
        )}
      </CardContent>
    </Card>
  );
}

function UnfulfilledCommitmentsSection({
  items,
}: {
  items: UnfulfilledCommitment[];
}) {
  const [dismissed, setDismissed] = useState<Set<number>>(new Set());

  async function handlePromote(actionId: number) {
    try {
      await api.post(`/api/inbound/actions/${actionId}/promote`);
      toast.success("Commitment promoted to task!");
      setDismissed((prev) => new Set(prev).add(actionId));
    } catch {
      toast.error("Failed to promote commitment");
    }
  }

  async function handleDismiss(actionId: number) {
    try {
      await api.post(`/api/inbound/actions/${actionId}/dismiss`);
      toast.success("Commitment dismissed");
      setDismissed((prev) => new Set(prev).add(actionId));
    } catch {
      toast.error("Failed to dismiss commitment");
    }
  }

  const visible = items.filter((a) => a.action_id && !dismissed.has(a.action_id));

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Bell className="h-4 w-4 text-orange-400" />
          <CardTitle className="text-base">Unfulfilled Commitments</CardTitle>
          {visible.length > 0 && (
            <Badge variant="secondary" className="text-xs">
              {visible.length}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {visible.length === 0 ? (
          <p className="py-3 text-center text-sm text-muted-foreground">
            Nothing detected
          </p>
        ) : (
          visible.map((item, i) => (
            <div
              key={item.action_id ?? i}
              className="rounded-lg border border-orange-500/20 bg-orange-500/5 px-3 py-2.5"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex-1 min-w-0">
                  <p className="text-sm">
                    You said: &ldquo;<span className="italic">{item.action_text}</span>&rdquo;
                  </p>
                  <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                    <span>
                      {item.days_ago ? `${item.days_ago} days ago` : "Recently"}
                    </span>
                    <span>via {item.source_type}</span>
                    {item.person_name && (
                      <span className="text-orange-400">
                        &rarr; To: {item.person_name}
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  {item.action_id && (
                    <>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 text-green-400 hover:text-green-300 hover:bg-green-500/10"
                        title="Mark as done"
                        onClick={() => handlePromote(item.action_id!)}
                      >
                        <CheckCircle2 className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 text-muted-foreground hover:text-red-400 hover:bg-red-500/10"
                        title="Dismiss"
                        onClick={() => handleDismiss(item.action_id!)}
                      >
                        <X className="h-4 w-4" />
                      </Button>
                      <div className="ml-1 border-l border-border/50 pl-1">
                        <FeedbackButtons
                          itemType="unfulfilled_commitment"
                          itemId={item.action_id}
                          variant="compact"
                        />
                      </div>
                    </>
                  )}
                </div>
              </div>
            </div>
          ))
        )}
      </CardContent>
    </Card>
  );
}

function CalendarSection({ events }: { events: CalendarEvent[] }) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  function toggleExpand(idx: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Calendar className="h-4 w-4 text-muted-foreground" />
          <CardTitle className="text-base">Today&apos;s Calendar</CardTitle>
          {events.length > 0 && (
            <Badge variant="secondary" className="text-xs">
              {events.length}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {events.length === 0 ? (
          <p className="py-3 text-center text-sm text-muted-foreground">
            No events today
          </p>
        ) : (
          events.map((event, i) => {
            const hasContext = event.attendee_context?.some(
              (a) => a.last_contact || a.recent_topic
            );
            const isExpanded = expanded.has(i);
            return (
              <div key={i} className="rounded-lg px-3 py-2.5 hover:bg-sidebar-accent/30 transition-colors">
                <div
                  className={cn("flex items-start gap-2", hasContext && "cursor-pointer")}
                  onClick={() => hasContext && toggleExpand(i)}
                >
                  {hasContext && (
                    <span className="mt-0.5 shrink-0 text-muted-foreground">
                      {isExpanded ? (
                        <ChevronDown className="h-3.5 w-3.5" />
                      ) : (
                        <ChevronRight className="h-3.5 w-3.5" />
                      )}
                    </span>
                  )}
                  <div className="flex-1">
                    <p className="text-sm">
                      <span className="font-medium text-blue-400">
                        {formatTime(event.start)}
                      </span>{" "}
                      &mdash; {event.summary}
                    </p>
                  </div>
                </div>
                {isExpanded && event.attendee_context && (
                  <div className="mt-1.5 ml-5 space-y-1 border-l-2 border-border/50 pl-3">
                    {event.attendee_context
                      .filter((a) => a.last_contact || a.recent_topic)
                      .map((att, j) => (
                        <p key={j} className="text-xs text-muted-foreground">
                          <Users className="inline h-3 w-3 mr-1" />
                          <span className="font-medium">{att.name}</span>:{" "}
                          {att.last_contact
                            ? `last contact ${att.last_contact}`
                            : "no prior contact"}
                          {att.recent_topic && `, recent: ${att.recent_topic}`}
                        </p>
                      ))}
                  </div>
                )}
              </div>
            );
          })
        )}
      </CardContent>
    </Card>
  );
}

function CrossSourceConnectionsSection({
  connections,
}: {
  connections: CrossSourceConnection[];
}) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Link2 className="h-4 w-4 text-blue-400" />
          <CardTitle className="text-base">Cross-Source Connections</CardTitle>
          {connections.length > 0 && (
            <Badge variant="secondary" className="text-xs">
              {connections.length}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {connections.length === 0 ? (
          <p className="py-3 text-center text-sm text-muted-foreground">
            No cross-source connections this week
          </p>
        ) : (
          connections.map((conn, i) => (
            <div
              key={i}
              className="rounded-lg border border-blue-500/20 bg-blue-500/5 px-3 py-2.5"
            >
              <div className="flex items-start justify-between gap-2">
                <p className="text-sm font-medium">{conn.entity_name}</p>
                <FeedbackButtons
                  itemType="cross_source_connection"
                  itemId={null}
                  variant="compact"
                  className="shrink-0"
                />
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                {conn.sources.map((s) => (
                  <Badge
                    key={s}
                    variant="outline"
                    className="text-[10px] px-1.5 py-0 capitalize"
                  >
                    {s}
                  </Badge>
                ))}
                <span className="text-xs text-muted-foreground">
                  ({conn.source_count} sources)
                </span>
              </div>
              {conn.sample_preview && (
                <p className="mt-1 text-xs text-muted-foreground truncate">
                  Latest: {conn.sample_preview}
                </p>
              )}
            </div>
          ))
        )}
      </CardContent>
    </Card>
  );
}

function OpenLoopsSection({ loops }: { loops: OpenLoop[] }) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 text-red-400" />
          <CardTitle className="text-base">Open Loops</CardTitle>
          {loops.length > 0 && (
            <Badge variant="secondary" className="text-xs">
              {loops.length}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {loops.length === 0 ? (
          <p className="py-3 text-center text-sm text-muted-foreground">
            No open loops
          </p>
        ) : (
          loops.slice(0, 5).map((loop, i) => (
            <div
              key={i}
              className="rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2.5"
            >
              <div className="flex items-baseline justify-between gap-2">
                <p className="text-sm font-medium">{loop.title}</p>
                <div className="flex shrink-0 items-center gap-2">
                  {loop.age_days !== undefined && (
                    <Badge
                      variant="outline"
                      className="text-[10px] px-1.5 py-0 text-red-400 border-red-500/50"
                    >
                      {loop.age_days}d
                    </Badge>
                  )}
                  <FeedbackButtons
                    itemType="open_loop"
                    itemId={null}
                    variant="compact"
                  />
                </div>
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                {loop.suggested_action}
              </p>
              {loop.linked_person && (
                <p className="mt-1 text-xs text-muted-foreground">
                  <Users className="inline h-3 w-3 mr-1" />
                  Linked to: {loop.linked_person}
                </p>
              )}
              {loop.related_activity?.slice(0, 2).map((act, j) => (
                <p key={j} className="mt-0.5 text-xs text-muted-foreground">
                  Related: {act.source_type} activity {act.date || ""}
                </p>
              ))}
            </div>
          ))
        )}
      </CardContent>
    </Card>
  );
}

function NudgeResponseButtons({
  nudgeId,
  onResponse,
}: {
  nudgeId: number;
  onResponse: (nudgeId: number, response: string) => void;
}) {
  const [loading, setLoading] = useState(false);

  async function handleResponse(response: string) {
    if (loading) return;
    setLoading(true);
    try {
      await api.post(`/api/nudges/${nudgeId}/respond`, {
        response,
        snooze_minutes: response === "snoozed" ? 60 : undefined,
      });
      toast.success(
        response === "helpful"
          ? "Marked as helpful!"
          : response === "dismissed"
            ? "Dismissed"
            : "Snoozed for 1 hour"
      );
      onResponse(nudgeId, response);
    } catch (error) {
      console.error("Failed to respond to nudge:", error);
      toast.error("Failed to record response");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex items-center gap-1">
      <Button
        variant="ghost"
        size="xs"
        className="text-green-400 hover:text-green-300 hover:bg-green-500/10"
        onClick={() => handleResponse("helpful")}
        disabled={loading}
      >
        <CheckCircle2 className="h-3.5 w-3.5" />
        Helpful
      </Button>
      <Button
        variant="ghost"
        size="xs"
        className="text-muted-foreground hover:text-red-400 hover:bg-red-500/10"
        onClick={() => handleResponse("dismissed")}
        disabled={loading}
      >
        <X className="h-3.5 w-3.5" />
        Dismiss
      </Button>
      <Button
        variant="ghost"
        size="xs"
        className="text-muted-foreground hover:text-yellow-400 hover:bg-yellow-500/10"
        onClick={() => handleResponse("snoozed")}
        disabled={loading}
      >
        <Bell className="h-3.5 w-3.5" />
        Snooze
      </Button>
    </div>
  );
}

function RecentNudgesSection({
  nudges,
  onResponse,
}: {
  nudges: Nudge[];
  onResponse: (nudgeId: number, response: string) => void;
}) {
  function formatRelativeTime(isoStr: string): string {
    try {
      const date = new Date(isoStr);
      const now = new Date();
      const diffMs = now.getTime() - date.getTime();
      const diffMins = Math.floor(diffMs / 60000);
      const diffHours = Math.floor(diffMins / 60);
      const diffDays = Math.floor(diffHours / 24);

      if (diffMins < 1) return "just now";
      if (diffMins < 60) return `${diffMins}m ago`;
      if (diffHours < 24) return `${diffHours}h ago`;
      return `${diffDays}d ago`;
    } catch {
      return isoStr;
    }
  }

  function nudgeTypeLabel(nudge_type: string): string {
    switch (nudge_type) {
      case "meeting_prep":
        return "Meeting Prep";
      case "relationship_check":
        return "Relationship Check";
      case "open_followup":
        return "Open Follow-up";
      default:
        // Graceful fallback: title-case the raw value (e.g. "push_reminder" → "Push Reminder")
        return nudge_type
          .replace(/_/g, " ")
          .replace(/\b\w/g, (c) => c.toUpperCase());
    }
  }

  function responseLabel(response: string): string {
    switch (response) {
      case "helpful":
        return "Helpful";
      case "dismissed":
        return "Dismissed";
      case "snoozed":
        return "Snoozed";
      default:
        return response;
    }
  }

  function responseBadgeStyle(response: string): string {
    switch (response) {
      case "helpful":
        return "bg-green-500/10 text-green-400 border-green-500/30";
      case "dismissed":
        return "bg-red-500/10 text-red-400 border-red-500/30";
      case "snoozed":
        return "bg-yellow-500/10 text-yellow-400 border-yellow-500/30";
      default:
        return "";
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Bell className="h-4 w-4 text-purple-400" />
          <CardTitle className="text-base">Recent Nudges</CardTitle>
          {nudges.length > 0 && (
            <Badge variant="secondary" className="text-xs">
              {nudges.length}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {nudges.length === 0 ? (
          <p className="py-3 text-center text-sm text-muted-foreground">
            No recent nudges
          </p>
        ) : (
          nudges.slice(0, 5).map((nudge) => (
            <div
              key={nudge.id}
              className="rounded-lg border border-purple-500/20 bg-purple-500/5 px-3 py-2.5"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium">{nudge.title}</p>
                  {nudge.body && (
                    <p className="mt-0.5 text-xs text-muted-foreground line-clamp-2">
                      {nudge.body}
                    </p>
                  )}
                  <div className="mt-1.5 flex items-center gap-2 text-[10px] text-muted-foreground">
                    <Badge
                      variant="outline"
                      className="px-1.5 py-0 capitalize"
                    >
                      {nudge.urgency}
                    </Badge>
                    {nudge.nudge_type && (
                      <Badge
                        variant="outline"
                        className="px-1.5 py-0 text-purple-400 border-purple-500/40"
                      >
                        {nudgeTypeLabel(nudge.nudge_type)}
                      </Badge>
                    )}
                    <span>{formatRelativeTime(nudge.sent_at || nudge.created_at)}</span>
                  </div>
                </div>
                <div className="shrink-0">
                  {nudge.user_response ? (
                    <Badge
                      variant="outline"
                      className={cn(
                        "text-[10px] px-1.5 py-0",
                        responseBadgeStyle(nudge.user_response)
                      )}
                    >
                      {responseLabel(nudge.user_response)}
                    </Badge>
                  ) : (
                    <NudgeResponseButtons
                      nudgeId={nudge.id}
                      onResponse={onResponse}
                    />
                  )}
                </div>
              </div>
            </div>
          ))
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function DigestPage() {
  const [activeTab, setActiveTab] = useState("daily");
  const [digest, setDigest] = useState<DigestData | null>(null);
  const [weekly, setWeekly] = useState<WeeklyReviewData | null>(null);
  const [nudges, setNudges] = useState<Nudge[]>([]);
  const [loading, setLoading] = useState(true);
  const [weeklyLoading, setWeeklyLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchDigest = useCallback(async () => {
    setRefreshing(true);
    setError(null);
    try {
      const result = await api.get<DigestData>("/api/settings/digest/today");
      setDigest(result);
    } catch (err) {
      console.error("Failed to fetch digest:", err);
      setError("Failed to load digest.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  const fetchNudges = useCallback(async () => {
    try {
      const result = await api.get<NudgeListResponse>("/api/nudges?hours=48&limit=10");
      setNudges(result.nudges);
    } catch (err) {
      console.error("Failed to fetch nudges:", err);
      // Non-critical - don't set error state
    }
  }, []);

  const fetchWeekly = useCallback(async () => {
    setWeeklyLoading(true);
    try {
      const result = await api.get<WeeklyReviewData>(
        "/api/settings/weekly-review/current"
      );
      setWeekly(result);
    } catch (err) {
      console.error("Failed to fetch weekly review:", err);
    } finally {
      setWeeklyLoading(false);
    }
  }, []);

  // Handler for when user responds to a nudge
  const handleNudgeResponse = useCallback((nudgeId: number, response: string) => {
    setNudges((prev) =>
      prev.map((n) =>
        n.id === nudgeId ? { ...n, user_response: response } : n
      )
    );
  }, []);

  useEffect(() => {
    fetchDigest();
    fetchNudges();
  }, [fetchDigest, fetchNudges]);

  useEffect(() => {
    if (activeTab === "weekly" && !weekly && !weeklyLoading) {
      fetchWeekly();
    }
  }, [activeTab, weekly, weeklyLoading, fetchWeekly]);

  if (loading) {
    return (
      <div className="mx-auto max-w-2xl space-y-6 p-6">
        <DigestSkeleton />
      </div>
    );
  }

  if (error) {
    return (
      <div className="mx-auto max-w-2xl p-6">
        <div className="flex flex-col items-center gap-3 py-12 text-muted-foreground">
          <AlertCircle className="h-8 w-8" />
          <p>{error}</p>
          <Button variant="outline" size="sm" onClick={fetchDigest}>
            Try again
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Daily Digest</h1>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          onClick={activeTab === "daily" ? () => { fetchDigest(); fetchNudges(); } : fetchWeekly}
          disabled={refreshing || weeklyLoading}
        >
          <RefreshCw
            className={cn(
              "h-4 w-4",
              (refreshing || weeklyLoading) && "animate-spin"
            )}
          />
        </Button>
      </div>

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList className="grid w-full grid-cols-2">
          <TabsTrigger value="daily">Daily Digest</TabsTrigger>
          <TabsTrigger value="weekly">Weekly Review</TabsTrigger>
        </TabsList>

        {/* ============ DAILY DIGEST ============ */}
        <TabsContent value="daily" className="space-y-4 mt-4">
          {digest && (
            <>
              {/* Summary */}
              <Card>
                <CardContent className="pt-4">
                  <p className="text-sm text-muted-foreground">
                    {digest.date}
                  </p>
                  <p className="mt-1 text-sm">{digest.summary}</p>
                </CardContent>
              </Card>

              {/* Top Priorities */}
              {digest.top_priorities.length > 0 && (
                <Card>
                  <CardHeader>
                    <div className="flex items-center gap-2">
                      <Target className="h-4 w-4 text-muted-foreground" />
                      <CardTitle className="text-base">
                        Top Priorities
                      </CardTitle>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-2">
                    {digest.top_priorities.map((p, i) => (
                      <div
                        key={i}
                        className="rounded-lg px-3 py-2 hover:bg-sidebar-accent/30 transition-colors"
                      >
                        <p
                          className={cn(
                            "text-sm",
                            p.overdue && "text-red-400 font-medium"
                          )}
                        >
                          {i + 1}. {p.next_action}
                          {p.overdue && " (overdue!)"}
                        </p>
                        {p.source === "project" && (
                          <p className="text-xs text-muted-foreground">
                            Project: {p.title}
                          </p>
                        )}
                      </div>
                    ))}
                  </CardContent>
                </Card>
              )}

              {/* Calendar */}
              <CalendarSection events={digest.calendar_today} />

              {/* Needs Your Reply */}
              <NeedsReplySection items={digest.needs_reply} />

              {/* Detected Actions */}
              <DetectedActionsSection items={digest.detected_actions} />

              {/* Unfulfilled Commitments */}
              <UnfulfilledCommitmentsSection
                items={digest.unfulfilled_commitments}
              />

              {/* Recent Nudges */}
              <RecentNudgesSection
                nudges={nudges}
                onResponse={handleNudgeResponse}
              />

              {/* Relationship Follow-ups */}
              {digest.relationship_followups.length > 0 && (
                <Card>
                  <CardHeader>
                    <div className="flex items-center gap-2">
                      <Users className="h-4 w-4 text-muted-foreground" />
                      <CardTitle className="text-base">
                        Relationship Check-ins
                      </CardTitle>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-2">
                    {digest.relationship_followups.map((f, i) => (
                      <div
                        key={i}
                        className="rounded-lg px-3 py-2 hover:bg-sidebar-accent/30 transition-colors"
                      >
                        <p className="text-sm font-medium">
                          {f.person}
                          {f.days_since_contact && (
                            <span className="ml-1.5 text-xs text-muted-foreground">
                              ({f.days_since_contact} days)
                            </span>
                          )}
                        </p>
                        {f.followup && (
                          <p className="text-xs text-muted-foreground">
                            {f.followup}
                          </p>
                        )}
                      </div>
                    ))}
                  </CardContent>
                </Card>
              )}

              {/* Stuck Items */}
              {digest.stuck_items.length > 0 && (
                <Card>
                  <CardHeader>
                    <div className="flex items-center gap-2">
                      <AlertTriangle className="h-4 w-4 text-red-400" />
                      <CardTitle className="text-base">Stuck Items</CardTitle>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-2">
                    {digest.stuck_items.map((item, i) => (
                      <div
                        key={i}
                        className="rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2"
                      >
                        <p className="text-sm font-medium">{item.title}</p>
                        <p className="text-xs text-muted-foreground">
                          {item.reason}
                        </p>
                      </div>
                    ))}
                  </CardContent>
                </Card>
              )}

              {/* Recent Win */}
              {digest.recent_win && (
                <Card>
                  <CardHeader>
                    <div className="flex items-center gap-2">
                      <Trophy className="h-4 w-4 text-green-400" />
                      <CardTitle className="text-base">Recent Win</CardTitle>
                    </div>
                  </CardHeader>
                  <CardContent>
                    <p className="rounded-lg bg-green-500/5 border border-green-500/20 px-3 py-2 text-sm">
                      {digest.recent_win}
                    </p>
                  </CardContent>
                </Card>
              )}
            </>
          )}
        </TabsContent>

        {/* ============ WEEKLY REVIEW ============ */}
        <TabsContent value="weekly" className="space-y-4 mt-4">
          {weeklyLoading ? (
            <DigestSkeleton />
          ) : weekly ? (
            <>
              {/* Summary */}
              <Card>
                <CardContent className="pt-4">
                  <p className="text-sm text-muted-foreground">
                    {weekly.week_of}
                  </p>
                  <p className="mt-1 text-sm">{weekly.summary}</p>
                </CardContent>
              </Card>

              {/* What Happened */}
              <Card>
                <CardHeader>
                  <div className="flex items-center gap-2">
                    <TrendingUp className="h-4 w-4 text-muted-foreground" />
                    <CardTitle className="text-base">What Happened</CardTitle>
                  </div>
                </CardHeader>
                <CardContent className="space-y-1.5">
                  {(weekly.what_happened.projects_completed?.length ?? 0) >
                    0 && (
                    <p className="text-sm">
                      <span className="font-medium text-green-400">
                        {weekly.what_happened.projects_completed!.length}{" "}
                        project
                        {weekly.what_happened.projects_completed!.length !== 1
                          ? "s"
                          : ""}{" "}
                        completed:
                      </span>{" "}
                      {weekly.what_happened.projects_completed!.join(", ")}
                    </p>
                  )}
                  {(weekly.what_happened.projects_started?.length ?? 0) > 0 && (
                    <p className="text-sm">
                      {weekly.what_happened.projects_started!.length} project
                      {weekly.what_happened.projects_started!.length !== 1
                        ? "s"
                        : ""}{" "}
                      started:{" "}
                      {weekly.what_happened.projects_started!.join(", ")}
                    </p>
                  )}
                  {((weekly.what_happened.tasks_completed ?? 0) +
                    (weekly.what_happened.errands_completed ?? 0)) >
                    0 && (
                    <p className="text-sm">
                      {(weekly.what_happened.tasks_completed ?? 0) +
                        (weekly.what_happened.errands_completed ?? 0)}{" "}
                      tasks/errands completed
                    </p>
                  )}
                  {(weekly.what_happened.people_contacted?.length ?? 0) > 0 && (
                    <p className="text-sm">
                      {weekly.what_happened.people_contacted!.length} people
                      contacted:{" "}
                      {weekly.what_happened.people_contacted!.slice(0, 5).join(", ")}
                    </p>
                  )}
                  {(weekly.what_happened.ideas_captured ?? 0) > 0 && (
                    <p className="text-sm">
                      {weekly.what_happened.ideas_captured} ideas captured
                    </p>
                  )}
                </CardContent>
              </Card>

              {/* Open Loops */}
              <OpenLoopsSection loops={weekly.open_loops} />

              {/* Cross-Source Connections */}
              <CrossSourceConnectionsSection
                connections={weekly.cross_source_connections}
              />

              {/* Patterns Noticed */}
              {weekly.patterns_noticed.length > 0 && (
                <Card>
                  <CardHeader>
                    <div className="flex items-center gap-2">
                      <TrendingUp className="h-4 w-4 text-purple-400" />
                      <CardTitle className="text-base">
                        Patterns Noticed
                      </CardTitle>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-2">
                    {weekly.patterns_noticed.map((pattern, i) => (
                      <div
                        key={i}
                        className="rounded-lg border border-purple-500/20 bg-purple-500/5 px-3 py-2"
                      >
                        <p className="text-sm">{pattern}</p>
                      </div>
                    ))}
                  </CardContent>
                </Card>
              )}

              {/* Suggested Focus */}
              {weekly.suggested_focus.length > 0 && (
                <Card>
                  <CardHeader>
                    <div className="flex items-center gap-2">
                      <Target className="h-4 w-4 text-blue-400" />
                      <CardTitle className="text-base">
                        Suggested Focus
                      </CardTitle>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-2">
                    {weekly.suggested_focus.map((focus, i) => (
                      <div
                        key={i}
                        className="rounded-lg px-3 py-2 hover:bg-sidebar-accent/30 transition-colors"
                      >
                        <p className="text-sm font-medium">
                          {i + 1}. {focus.area}
                        </p>
                        <p className="text-xs text-muted-foreground">
                          {focus.reason}
                        </p>
                      </div>
                    ))}
                  </CardContent>
                </Card>
              )}

              {/* Relationships */}
              {((weekly.relationships.contacted_this_week?.length ?? 0) > 0 ||
                (weekly.relationships.getting_stale?.length ?? 0) > 0) && (
                <Card>
                  <CardHeader>
                    <div className="flex items-center gap-2">
                      <Users className="h-4 w-4 text-muted-foreground" />
                      <CardTitle className="text-base">Relationships</CardTitle>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-2">
                    {(weekly.relationships.contacted_this_week?.length ?? 0) >
                      0 && (
                      <p className="text-sm text-green-400">
                        <span className="font-medium">Connected with:</span>{" "}
                        {weekly.relationships
                          .contacted_this_week!.slice(0, 5)
                          .join(", ")}
                      </p>
                    )}
                    {(weekly.relationships.getting_stale?.length ?? 0) > 0 && (
                      <p className="text-sm text-red-400">
                        <span className="font-medium">Getting stale:</span>{" "}
                        {weekly.relationships
                          .getting_stale!.slice(0, 3)
                          .map((s) => s.name)
                          .join(", ")}
                      </p>
                    )}
                  </CardContent>
                </Card>
              )}

              {/* Wins to Celebrate */}
              {weekly.wins_to_celebrate.length > 0 && (
                <Card>
                  <CardHeader>
                    <div className="flex items-center gap-2">
                      <Trophy className="h-4 w-4 text-green-400" />
                      <CardTitle className="text-base">
                        Wins to Celebrate
                      </CardTitle>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-2">
                    {weekly.wins_to_celebrate.map((win, i) => (
                      <div
                        key={i}
                        className="rounded-lg bg-green-500/5 border border-green-500/20 px-3 py-2"
                      >
                        <p className="text-sm">{win}</p>
                      </div>
                    ))}
                  </CardContent>
                </Card>
              )}
            </>
          ) : (
            <p className="py-8 text-center text-sm text-muted-foreground">
              Failed to load weekly review.
            </p>
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}
