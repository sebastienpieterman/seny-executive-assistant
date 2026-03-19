import { useCallback, useEffect, useState } from "react";
import { useNavigate, useOutletContext } from "react-router-dom";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import {
  RefreshCw,
  Mail,
  MessageSquare,
  Send,
  Brain,
  CheckCircle2,
  ArrowRight,
  Users,
  AlertCircle,
  Plug,
} from "lucide-react";
import type { SettingsTab } from "@/components/settings/SettingsDialog";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface DashboardTask {
  id: number;
  title: string;
  due_date: string | null;
  priority: string;
  type: string;
  status: string;
}

interface PeopleFollowup {
  followup_id: number;
  person_name: string;
  followup_text: string;
  due_date: string | null;
  person_id: number;
}

interface RecentActivity {
  new_emails: number | null;
  new_slack_messages: number | null;
  new_telegram_messages: number | null;
  captures_today: number | null;
  tasks_completed_today: number | null;
}

interface DashboardData {
  priority_tasks: DashboardTask[] | null;
  people_followups: PeopleFollowup[] | null;
  recent_activity: RecentActivity | null;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getGreeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

function formatDue(dueDate: string | null): { text: string; variant: "overdue" | "today" | "normal" } | null {
  if (!dueDate) return null;
  const due = new Date(dueDate);
  if (isNaN(due.getTime())) return null;
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const dueDay = new Date(due.getFullYear(), due.getMonth(), due.getDate());
  const diffDays = Math.floor((dueDay.getTime() - today.getTime()) / (1000 * 60 * 60 * 24));

  if (diffDays < 0) {
    return { text: `${Math.abs(diffDays)}d overdue`, variant: "overdue" };
  }
  if (diffDays === 0) {
    return { text: "Today", variant: "today" };
  }
  if (diffDays === 1) {
    return { text: "Tomorrow", variant: "normal" };
  }
  if (diffDays < 7) {
    return { text: due.toLocaleDateString("en-US", { weekday: "short" }), variant: "normal" };
  }
  return { text: due.toLocaleDateString("en-US", { month: "short", day: "numeric" }), variant: "normal" };
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function DashboardSkeleton() {
  return (
    <div className="space-y-6">
      <Skeleton className="h-8 w-48" />
      <Skeleton className="h-[200px] w-full rounded-xl" />
      <Skeleton className="h-[160px] w-full rounded-xl" />
      <Skeleton className="h-[120px] w-full rounded-xl" />
    </div>
  );
}

function TaskRow({
  task,
  onToggle,
}: {
  task: DashboardTask;
  onToggle: (task: DashboardTask) => void;
}) {
  const [completing, setCompleting] = useState(false);
  const [justCompleted, setJustCompleted] = useState(false);
  const isCompleted = task.status === "completed";
  const dueInfo = formatDue(task.due_date);

  async function handleToggle() {
    setCompleting(true);
    const newStatus = isCompleted ? "active" : "completed";
    if (newStatus === "completed") {
      setJustCompleted(true);
      setTimeout(() => setJustCompleted(false), 600);
    }
    // Optimistic update
    onToggle({ ...task, status: newStatus });
    try {
      const endpoint = isCompleted
        ? `/api/tasks/${task.id}/reopen`
        : `/api/tasks/${task.id}/complete`;
      await api.post(endpoint);
    } catch (err) {
      console.error("Failed to toggle task:", err);
      onToggle(task); // revert
    } finally {
      setCompleting(false);
    }
  }

  return (
    <div
      className={cn(
        "flex items-start gap-3 rounded-lg px-3 py-2.5 transition-all hover:bg-sidebar-accent/30",
        justCompleted && "bg-green-500/10 ring-1 ring-green-500/20"
      )}
    >
      <Checkbox
        checked={isCompleted}
        onCheckedChange={handleToggle}
        disabled={completing}
        className="mt-0.5 shrink-0"
      />
      <div className="flex-1 min-w-0">
        <p
          className={cn(
            "text-sm leading-snug",
            isCompleted && "line-through text-muted-foreground"
          )}
        >
          {task.title}
        </p>
        <div className="mt-1 flex flex-wrap items-center gap-1.5">
          {task.type && task.type !== "task" && (
            <Badge variant="outline" className="text-[10px] px-1.5 py-0 capitalize">
              {task.type}
            </Badge>
          )}
          {dueInfo && (
            <Badge
              variant="outline"
              className={cn(
                "text-[10px] px-1.5 py-0",
                dueInfo.variant === "overdue" && "bg-red-500/20 border-red-500/50 text-red-400",
                dueInfo.variant === "today" && "bg-yellow-500/20 border-yellow-500/50 text-yellow-400"
              )}
            >
              {dueInfo.text}
            </Badge>
          )}
        </div>
      </div>
    </div>
  );
}

function ActivityStat({
  icon: Icon,
  label,
  count,
  onClick,
}: {
  icon: React.ElementType;
  label: string;
  count: number | null;
  onClick?: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className="flex flex-col items-center gap-1.5 rounded-lg border border-border/50 bg-sidebar-accent/20 p-3 transition-colors hover:bg-sidebar-accent/40"
    >
      <Icon className="h-5 w-5 text-muted-foreground" />
      <span className="text-xl font-semibold tabular-nums">
        {count !== null ? count : "\u2014"}
      </span>
      <span className="text-[11px] text-muted-foreground">{label}</span>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface LayoutContext {
  toggleSlideOver: (panel: "slack" | "telegram") => void;
  openSettings: (tab?: SettingsTab) => void;
}

const INTEGRATIONS_BANNER_KEY = "seny_integrations_banner_dismissed";

export function HomePage() {
  const navigate = useNavigate();
  const { toggleSlideOver, openSettings } = useOutletContext<LayoutContext>();
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showWelcome, setShowWelcome] = useState(() => {
    const justCompleted = sessionStorage.getItem("seny_setup_just_completed");
    if (justCompleted) {
      sessionStorage.removeItem("seny_setup_just_completed");
      return true;
    }
    return false;
  });

  // Integration awareness banner state
  const [bannerDismissed, setBannerDismissed] = useState(() =>
    localStorage.getItem(INTEGRATIONS_BANNER_KEY) === "true"
  );
  const [hasIntegration, setHasIntegration] = useState<boolean | null>(null);

  // Check if user has any integrations connected
  useEffect(() => {
    if (bannerDismissed) return; // Skip check if already dismissed
    async function checkIntegrations() {
      try {
        const [gmail, slack, telegram, microsoft] = await Promise.all([
          api.get<{ connected: boolean }>("/api/email/health").catch(() => ({ connected: false })),
          api.get<{ connected: boolean }>("/api/slack/health").catch(() => ({ connected: false })),
          api.get<{ connected: boolean }>("/api/telegram/status").catch(() => ({ connected: false })),
          api.get<{ connected: boolean }>("/api/microsoft/status").catch(() => ({ connected: false })),
        ]);
        setHasIntegration(
          gmail.connected || slack.connected || telegram.connected || microsoft.connected
        );
      } catch {
        // If check fails, don't show banner
        setHasIntegration(true);
      }
    }
    checkIntegrations();
  }, [bannerDismissed]);

  function dismissIntegrationsBanner() {
    localStorage.setItem(INTEGRATIONS_BANNER_KEY, "true");
    setBannerDismissed(true);
  }

  const showIntegrationsBanner = !bannerDismissed && hasIntegration === false;

  const fetchDashboard = useCallback(async () => {
    setRefreshing(true);
    setError(null);
    try {
      const result = await api.get<DashboardData>("/api/dashboard");
      setData(result);
    } catch (err) {
      console.error("Failed to fetch dashboard:", err);
      setError("Failed to load dashboard data.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchDashboard();
  }, [fetchDashboard]);

  // Task toggle handler — update local state optimistically
  function handleTaskToggle(updated: DashboardTask) {
    setData((prev) => {
      if (!prev || !prev.priority_tasks) return prev;
      return {
        ...prev,
        priority_tasks: prev.priority_tasks.map((t) =>
          t.id === updated.id ? updated : t
        ),
      };
    });
  }

  if (loading) {
    return (
      <div className="mx-auto max-w-2xl space-y-6 p-6">
        <DashboardSkeleton />
      </div>
    );
  }

  if (error) {
    return (
      <div className="mx-auto max-w-2xl p-6">
        <div className="flex flex-col items-center gap-3 py-12 text-muted-foreground">
          <AlertCircle className="h-8 w-8" />
          <p>{error}</p>
          <Button variant="outline" size="sm" onClick={fetchDashboard}>
            Try again
          </Button>
        </div>
      </div>
    );
  }

  const tasks = data?.priority_tasks ?? [];
  const followups = data?.people_followups ?? [];
  const activity = data?.recent_activity;
  const displayTasks = tasks.slice(0, 5);
  const activityAllZero =
    activity &&
    (activity.new_emails ?? 0) === 0 &&
    (activity.new_slack_messages ?? 0) === 0 &&
    (activity.new_telegram_messages ?? 0) === 0 &&
    (activity.captures_today ?? 0) === 0 &&
    (activity.tasks_completed_today ?? 0) === 0;

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">{getGreeting()}</h1>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          onClick={fetchDashboard}
          disabled={refreshing}
        >
          <RefreshCw className={cn("h-4 w-4", refreshing && "animate-spin")} />
        </Button>
      </div>

      {/* Welcome banner (one-time, after setup completion) */}
      {showWelcome && (
        <Card>
          <CardContent className="flex items-start justify-between gap-4 p-4">
            <div>
              <p className="font-medium text-foreground">Welcome to Seny!</p>
              <p className="mt-1 text-sm text-muted-foreground">
                Your assistant is set up and ready. Try chatting — say hi, ask
                about your schedule, or tell Seny what you're working on.
              </p>
            </div>
            <Button
              variant="ghost"
              size="sm"
              className="shrink-0 text-muted-foreground"
              onClick={() => setShowWelcome(false)}
            >
              Dismiss
            </Button>
          </CardContent>
        </Card>
      )}

      {/* Integration awareness banner */}
      {showIntegrationsBanner && (
        <Card className="border-blue-500/30 bg-blue-500/5">
          <CardContent className="p-4">
            <div className="flex items-start gap-3">
              <Plug className="mt-0.5 h-5 w-5 shrink-0 text-blue-400" />
              <div className="flex-1">
                <p className="font-medium text-foreground">
                  Connect Your Integrations
                </p>
                <p className="mt-1 text-sm text-muted-foreground">
                  Seny works best when connected to your accounts. Set up
                  integrations to unlock email, calendar, Slack, and Telegram
                  features.
                </p>
                <div className="mt-3 flex items-center gap-2">
                  <Button
                    size="sm"
                    onClick={() => openSettings("integrations")}
                  >
                    Go to Integrations
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="text-muted-foreground"
                    onClick={dismissIntegrationsBanner}
                  >
                    Dismiss
                  </Button>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Section 1: Priority Tasks */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <CardTitle className="text-base">What Needs Your Attention</CardTitle>
            {tasks.length > 0 && (
              <Badge variant="secondary" className="text-xs">
                {tasks.length}
              </Badge>
            )}
          </div>
        </CardHeader>
        <CardContent className="space-y-1">
          {displayTasks.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted-foreground">
              Nothing urgent — you're on top of things.
            </p>
          ) : (
            <>
              {displayTasks.map((task) => (
                <TaskRow key={task.id} task={task} onToggle={handleTaskToggle} />
              ))}
              <button
                onClick={() => navigate("/tasks")}
                className="mt-2 flex w-full items-center justify-center gap-1 rounded-lg py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent/30 hover:text-foreground"
              >
                See all tasks <ArrowRight className="h-3.5 w-3.5" />
              </button>
            </>
          )}
        </CardContent>
      </Card>

      {/* Section 2: People Follow-ups */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <Users className="h-4 w-4 text-muted-foreground" />
            <CardTitle className="text-base">People to Follow Up With</CardTitle>
          </div>
        </CardHeader>
        <CardContent className="space-y-1">
          {followups.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted-foreground">
              No pending follow-ups.
            </p>
          ) : (
            <>
              {followups.map((fu) => (
                <div
                  key={fu.followup_id}
                  className="rounded-lg px-3 py-2.5 transition-colors hover:bg-sidebar-accent/30"
                >
                  <p className="text-sm font-medium">{fu.person_name}</p>
                  <p className="mt-0.5 text-sm text-muted-foreground">{fu.followup_text}</p>
                  {fu.due_date && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      {new Date(fu.due_date).toLocaleDateString("en-US", {
                        month: "short",
                        day: "numeric",
                      })}
                    </p>
                  )}
                </div>
              ))}
              <button
                onClick={() => navigate("/second-brain")}
                className="mt-2 flex w-full items-center justify-center gap-1 rounded-lg py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent/30 hover:text-foreground"
              >
                See all follow-ups <ArrowRight className="h-3.5 w-3.5" />
              </button>
            </>
          )}
        </CardContent>
      </Card>

      {/* Section 3: Recent Activity */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Since You've Been Gone</CardTitle>
        </CardHeader>
        <CardContent>
          {activityAllZero ? (
            <p className="py-4 text-center text-sm text-muted-foreground">
              Quiet day so far.
            </p>
          ) : (
            <div className="grid grid-cols-3 gap-3 sm:grid-cols-5">
              <ActivityStat
                icon={Mail}
                label="Emails"
                count={activity?.new_emails ?? null}
                onClick={() => navigate("/mail")}
              />
              <ActivityStat
                icon={MessageSquare}
                label="Slack"
                count={activity?.new_slack_messages ?? null}
                onClick={() => toggleSlideOver("slack")}
              />
              <ActivityStat
                icon={Send}
                label="Telegram"
                count={activity?.new_telegram_messages ?? null}
                onClick={() => toggleSlideOver("telegram")}
              />
              <ActivityStat
                icon={Brain}
                label="Captures"
                count={activity?.captures_today ?? null}
                onClick={() => navigate("/second-brain")}
              />
              <ActivityStat
                icon={CheckCircle2}
                label="Completed"
                count={activity?.tasks_completed_today ?? null}
                onClick={() => navigate("/tasks")}
              />
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
