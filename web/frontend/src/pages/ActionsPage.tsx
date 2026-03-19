import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  Card,
  CardContent,
  CardHeader,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { CheckCircle2, Loader2 } from "lucide-react";
import { toast } from "sonner";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface PendingAction {
  id: number;
  action_type: string; // 'email_draft' | 'calendar_proposal' | 'task_proposal'
  status: string;      // 'pending' | 'approved' | 'dismissed'
  title: string;
  content_json: string; // raw JSON string from the API
  created_at: string;
  updated_at: string | null;
}

interface AuditData {
  has_data: boolean;
  fidelity_score: number | null;
  run_at: string | null;
  negative_unabsorbed_count: number | null;
  suppression_gap_count: number | null;
  proposal_count: number;
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

function typeBadge(actionType: string): string {
  switch (actionType) {
    case "email_draft":
      return "📧 Email Draft";
    case "calendar_proposal":
      return "📅 Calendar";
    case "task_proposal":
      return "✅ Task";
    case "research_proposal":
      return "🧪 Memory Proposal";
    default:
      return actionType;
  }
}

function parseContent(contentJson: string): Record<string, string> {
  try {
    return JSON.parse(contentJson) as Record<string, string>;
  } catch {
    return {};
  }
}

function contentPreview(action: PendingAction): string {
  const c = parseContent(action.content_json);
  if (action.action_type === "email_draft") {
    const to = c.to ?? "";
    const subject = c.subject ?? "";
    const parts: string[] = [];
    if (to) parts.push(`To: ${to}`);
    if (subject) parts.push(`Subject: ${subject}`);
    return parts.join(" · ");
  }
  if (action.action_type === "calendar_proposal") {
    const start = c.start_datetime ?? c.start ?? "";
    if (start) {
      try {
        return new Date(start).toLocaleString();
      } catch {
        return start;
      }
    }
    return "";
  }
  if (action.action_type === "task_proposal") {
    const due = c.due_date ?? "";
    return due ? `Due: ${due}` : "";
  }
  if (action.action_type === "research_proposal") {
    return c.memory_rule ?? "";
  }
  return "";
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

type StatusFilter = "pending" | "approved" | "dismissed";

export function ActionsPage() {
  const [filter, setFilter] = useState<StatusFilter>("pending");
  const [actions, setActions] = useState<PendingAction[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Track which card ids are highlighted
  const [highlightedId, setHighlightedId] = useState<number | null>(null);
  const highlightTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Gmail accounts for the From dropdown in email_draft edit form
  const [gmailAccounts, setGmailAccounts] = useState<string[]>([]);

  // Google Calendar list for the calendar picker on calendar_proposal cards
  const [calendarList, setCalendarList] = useState<{ id: string; summary: string; primary: boolean }[]>([]);

  // Per-action calendar selection (calendar_proposal only)
  const [calendarSelections, setCalendarSelections] = useState<Record<number, string>>({});

  // Dismiss dialog state
  const [dismissTarget, setDismissTarget] = useState<number | null>(null);
  const [dismissReason, setDismissReason] = useState("");

  // Edit state — only one card editable at a time
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState<{ title: string; content: Record<string, string> } | null>(null);
  const [savingId, setSavingId] = useState<number | null>(null);

  // Fidelity banner data
  const [auditData, setAuditData] = useState<AuditData | null>(null);

  useEffect(() => {
    api.get<AuditData>("/api/research/latest-audit")
      .then((data) => {
        if (data && data.has_data) setAuditData(data);
      })
      .catch(() => {
        // Non-fatal — banner simply won't render
      });
  }, []);

  // -------------------------------------------------------------------------
  // Fetch
  // -------------------------------------------------------------------------

  async function fetchActions(status: StatusFilter) {
    setLoading(true);
    setError(null);
    try {
      const data = await api.get<PendingAction[]>(
        `/api/pending-actions?status=${status}`
      );
      setActions(Array.isArray(data) ? data : []);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to load actions";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }

  // On mount: fetch Gmail accounts for the From dropdown
  useEffect(() => {
    api.get<{ accounts: { email: string; created_at: string }[] }>("/api/email/accounts")
      .then((data) => {
        if (data && Array.isArray(data.accounts)) {
          setGmailAccounts(data.accounts.map((a) => a.email));
        }
      })
      .catch(() => {
        // Non-fatal — From dropdown will show "No Gmail accounts connected"
      });
  }, []);

  // On mount: fetch Google Calendars for the calendar picker
  useEffect(() => {
    api.get<{ calendars: { id: string; summary: string; primary: boolean }[] }>("/api/calendar/calendars")
      .then((data) => {
        if (data && Array.isArray(data.calendars)) {
          setCalendarList(data.calendars);
        }
      })
      .catch(() => {
        // Non-fatal — picker will not render if no calendars loaded
      });
  }, []);

  // On mount: fetch and handle highlight param
  useEffect(() => {
    fetchActions(filter);
  }, [filter]);

  // After actions load, handle ?highlight= param
  useEffect(() => {
    if (loading || actions.length === 0) return;
    const params = new URLSearchParams(window.location.search);
    const highlightParam = params.get("highlight");
    if (!highlightParam) return;
    const targetId = parseInt(highlightParam, 10);
    if (isNaN(targetId)) return;

    setHighlightedId(targetId);

    // Scroll to card
    requestAnimationFrame(() => {
      const el = document.getElementById(`action-${targetId}`);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });

    // Remove highlight after 3s
    if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current);
    highlightTimerRef.current = setTimeout(() => {
      setHighlightedId(null);
    }, 3000);

    return () => {
      if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current);
    };
  }, [loading, actions]);

  // -------------------------------------------------------------------------
  // Approve / Dismiss
  // -------------------------------------------------------------------------

  async function handleApprove(id: number, selectedCalendarId?: string) {
    try {
      const calId = selectedCalendarId || calendarSelections[id];
      const url = calId
        ? `/api/pending-actions/${id}/approve?calendar_id=${encodeURIComponent(calId)}`
        : `/api/pending-actions/${id}/approve`;
      await api.post(url);
      // Remove from pending view immediately and update sidebar badge
      setActions((prev) => prev.filter((a) => a.id !== id));
      window.dispatchEvent(new CustomEvent("pending-actions-changed"));
      toast.success("Action approved");
    } catch (err) {
      let message = "Failed to approve action";
      if (err instanceof Error) {
        try {
          const parsed = JSON.parse(err.message);
          if (parsed.detail) message = parsed.detail;
        } catch {
          if (err.message) message = err.message;
        }
      }
      toast.error(message, { icon: "🚨" });
    }
  }

  function openDismissDialog(id: number) {
    setDismissTarget(id);
    setDismissReason("");
  }

  async function confirmDismiss() {
    if (dismissTarget === null) return;
    const id = dismissTarget;
    const reason = dismissReason.trim();
    setDismissTarget(null);
    setDismissReason("");
    try {
      await api.post(`/api/pending-actions/${id}/dismiss`, {
        reason: reason || null,
      });
      setActions((prev) => prev.filter((a) => a.id !== id));
      window.dispatchEvent(new CustomEvent("pending-actions-changed"));
      toast.success("Action dismissed");
    } catch {
      toast.error("Failed to dismiss action");
    }
  }

  async function handleRestore(id: number) {
    try {
      await api.post(`/api/pending-actions/${id}/restore`);
      // Optimistic update — remove from dismissed view
      setActions((prev) => prev.filter((a) => a.id !== id));
      toast.success("Action restored to pending");
    } catch {
      toast.error("Failed to restore action");
    }
  }

  // -------------------------------------------------------------------------
  // Edit / Save
  // -------------------------------------------------------------------------

  function handleEditFirst(action: PendingAction) {
    setEditingId(action.id);
    setEditDraft({
      title: action.title,
      content: parseContent(action.content_json),
    });
  }

  function handleCancelEdit() {
    setEditingId(null);
    setEditDraft(null);
  }

  function updateDraft(field: string, value: string) {
    setEditDraft((prev) =>
      prev ? { ...prev, content: { ...prev.content, [field]: value } } : null
    );
  }

  function updateDraftTitle(value: string) {
    setEditDraft((prev) => (prev ? { ...prev, title: value } : null));
  }

  async function handleSave(actionId: number) {
    if (!editDraft) return;
    setSavingId(actionId);
    try {
      await api.patch(`/api/pending-actions/${actionId}`, {
        title: editDraft.title,
        content_json: JSON.stringify(editDraft.content),
      });
      // Optimistic update
      setActions((prev) =>
        prev.map((a) =>
          a.id === actionId
            ? { ...a, title: editDraft.title, content_json: JSON.stringify(editDraft.content) }
            : a
        )
      );
      setEditingId(null);
      setEditDraft(null);
      toast.success("Changes saved");
    } catch {
      toast.error("Failed to save — please try again");
    } finally {
      setSavingId(null);
    }
  }

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  const tabs: { label: string; value: StatusFilter }[] = [
    { label: "Pending", value: "pending" },
    { label: "Approved", value: "approved" },
    { label: "Dismissed", value: "dismissed" },
  ];

  return (
    <div className="flex flex-col gap-6 p-4 md:p-6 max-w-3xl mx-auto w-full">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Actions</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Review and approve Seny's drafts
        </p>
      </div>

      {/* Filter tabs */}
      <div className="flex gap-2 border-b border-border pb-2">
        {tabs.map((tab) => (
          <button
            key={tab.value}
            onClick={() => setFilter(tab.value)}
            className={cn(
              "px-3 py-1.5 text-sm font-medium rounded-md transition-colors",
              filter === tab.value
                ? "bg-sidebar-accent text-primary"
                : "text-muted-foreground hover:bg-sidebar-accent/50 hover:text-foreground"
            )}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Fidelity banner — pending tab only, when audit data exists */}
      {filter === "pending" && auditData && (
        <FidelityBanner data={auditData} />
      )}

      {/* Content */}
      {loading ? (
        <div className="flex flex-col gap-3">
          {[1, 2, 3].map((i) => (
            <Card key={i} className="animate-pulse">
              <CardHeader className="pb-2">
                <div className="h-4 bg-muted rounded w-1/4" />
                <div className="h-5 bg-muted rounded w-3/4 mt-1" />
              </CardHeader>
              <CardContent>
                <div className="h-4 bg-muted rounded w-1/2" />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : error ? (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
          {error}
        </div>
      ) : actions.length === 0 ? (
        <EmptyState filter={filter} />
      ) : (
        <div className="flex flex-col gap-3">
          {actions.map((action) => (
            <ActionCard
              key={action.id}
              action={action}
              highlighted={highlightedId === action.id}
              isEditing={editingId === action.id}
              editDraft={editingId === action.id ? editDraft : null}
              isSaving={savingId === action.id}
              gmailAccounts={gmailAccounts}
              calendarList={calendarList}
              selectedCalendarId={calendarSelections[action.id] ?? ""}
              onCalendarSelect={(calId) => setCalendarSelections((prev) => ({ ...prev, [action.id]: calId }))}
              onApprove={handleApprove}
              onDismiss={openDismissDialog}
              onRestore={handleRestore}
              onEditFirst={handleEditFirst}
              onCancelEdit={handleCancelEdit}
              onSave={handleSave}
              onUpdateDraft={updateDraft}
              onUpdateDraftTitle={updateDraftTitle}
            />
          ))}
        </div>
      )}

      {/* Dismiss reason dialog */}
      {dismissTarget !== null && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl shadow-xl p-6 max-w-sm w-full mx-4">
            <h3 className="text-base font-semibold mb-1">Dismiss this action?</h3>
            <p className="text-sm text-muted-foreground mb-3">
              Why are you dismissing this?{" "}
              <span className="text-xs opacity-70">(optional — helps Seny learn)</span>
            </p>
            <Textarea
              className="mb-4 resize-none"
              rows={3}
              placeholder="e.g. I'm not in that department, stop suggesting replies to list emails..."
              value={dismissReason}
              onChange={(e) => setDismissReason(e.target.value)}
              autoFocus
            />
            <div className="flex gap-2 justify-end">
              <Button
                variant="outline"
                onClick={() => { setDismissTarget(null); setDismissReason(""); }}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={confirmDismiss}
              >
                Dismiss
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Fidelity banner
// ---------------------------------------------------------------------------

function FidelityBanner({ data }: { data: AuditData }) {
  const fidelityPct = data.fidelity_score !== null
    ? Math.round(data.fidelity_score * 100)
    : null;

  const parts: string[] = [];
  if (data.run_at) parts.push(`Last audit: ${relativeTime(data.run_at)}`);
  if (fidelityPct !== null) parts.push(`Feedback fidelity: ${fidelityPct}%`);
  if (data.proposal_count > 0) parts.push(`${data.proposal_count} proposal${data.proposal_count !== 1 ? "s" : ""} queued`);

  if (parts.length === 0) return null;

  return (
    <div className="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground flex items-center gap-1.5 flex-wrap">
      <span className="font-medium text-foreground/70">🔬 Self-optimizing loop</span>
      <span className="text-muted-foreground/50">·</span>
      {parts.map((part, i) => (
        <span key={i}>
          {i > 0 && <span className="text-muted-foreground/50 mr-1.5">·</span>}
          {part}
        </span>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

function EmptyState({ filter }: { filter: StatusFilter }) {
  const messages: Record<StatusFilter, { heading: string; body: string }> = {
    pending: {
      heading: "No pending actions",
      body: "When Seny drafts an email, proposes a calendar event, or suggests a task, it will appear here for your review.",
    },
    approved: {
      heading: "No approved actions",
      body: "Actions you approve will appear here.",
    },
    dismissed: {
      heading: "No dismissed actions",
      body: "Actions you dismiss will appear here.",
    },
  };

  const { heading, body } = messages[filter];

  return (
    <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
      <CheckCircle2 className="h-10 w-10 text-muted-foreground/50" />
      <p className="text-base font-medium text-foreground">{heading}</p>
      <p className="text-sm text-muted-foreground max-w-sm">{body}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Action card
// ---------------------------------------------------------------------------

interface ActionCardProps {
  action: PendingAction;
  highlighted: boolean;
  isEditing: boolean;
  editDraft: { title: string; content: Record<string, string> } | null;
  isSaving: boolean;
  gmailAccounts: string[];
  calendarList: { id: string; summary: string; primary: boolean }[];
  selectedCalendarId: string;
  onCalendarSelect: (calId: string) => void;
  onApprove: (id: number, calendarId?: string) => void;
  onDismiss: (id: number) => void;
  onRestore: (id: number) => void;
  onEditFirst: (action: PendingAction) => void;
  onCancelEdit: () => void;
  onSave: (id: number) => void;
  onUpdateDraft: (field: string, value: string) => void;
  onUpdateDraftTitle: (value: string) => void;
}

function ActionCard({
  action,
  highlighted,
  isEditing,
  editDraft,
  isSaving,
  gmailAccounts,
  calendarList,
  selectedCalendarId,
  onCalendarSelect,
  onApprove,
  onDismiss,
  onRestore,
  onEditFirst,
  onCancelEdit,
  onSave,
  onUpdateDraft,
  onUpdateDraftTitle,
}: ActionCardProps) {
  const preview = contentPreview(action);

  return (
    <Card
      id={`action-${action.id}`}
      className={cn(
        "transition-all duration-500",
        highlighted && "ring-2 ring-primary"
      )}
    >
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <div className="flex flex-col gap-1 flex-1 min-w-0">
            {/* Type badge */}
            <span className="text-xs text-muted-foreground font-medium">
              {typeBadge(action.action_type)}
            </span>
            {/* Title — editable in edit mode */}
            {isEditing && editDraft ? (
              <Input
                value={editDraft.title}
                onChange={(e) => onUpdateDraftTitle(e.target.value)}
                className="font-semibold text-sm h-8"
              />
            ) : (
              <p className="font-semibold text-sm leading-snug">{action.title}</p>
            )}
          </div>
          {/* Status badge for non-pending */}
          {action.status === "approved" && (
            <Badge
              variant="outline"
              className="shrink-0 border-green-500/40 bg-green-500/10 text-green-400"
            >
              Approved
            </Badge>
          )}
          {action.status === "dismissed" && (
            <Badge
              variant="outline"
              className="shrink-0 border-muted-foreground/30 text-muted-foreground"
            >
              Dismissed
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent>
        <div className="flex flex-col gap-3">
          {/* Edit form or content preview */}
          {isEditing && editDraft ? (
            <EditForm
              actionType={action.action_type}
              editDraft={editDraft}
              gmailAccounts={gmailAccounts}
              calendarList={calendarList}
              onUpdateDraft={onUpdateDraft}
            />
          ) : (
            <>
              {preview && (
                <p className="text-xs text-muted-foreground">{preview}</p>
              )}
              {/* Inline calendar picker for pending calendar_proposal cards */}
              {action.action_type === "calendar_proposal" && action.status === "pending" && calendarList.length > 0 && (
                <div className="flex items-center gap-2">
                  <span className="text-xs text-muted-foreground shrink-0">Calendar:</span>
                  <select
                    value={selectedCalendarId || "primary"}
                    onChange={(e) => onCalendarSelect(e.target.value)}
                    className="flex-1 rounded-md border border-input bg-background px-2 py-1 text-xs shadow-sm focus:outline-none focus:ring-1 focus:ring-ring"
                  >
                    {calendarList.map((cal) => (
                      <option key={cal.id} value={cal.id}>
                        {cal.summary}{cal.primary ? " (primary)" : ""}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              {/* Evidence block for research_proposal cards */}
              {action.action_type === "research_proposal" && action.status === "pending" && (() => {
                const c2 = parseContent(action.content_json);
                const rule = c2.memory_rule ?? "";
                const evidence = c2.evidence ?? "";
                return (
                  <div className="rounded-md border border-primary/20 bg-primary/5 px-3 py-2 space-y-1">
                    <p className="text-sm font-medium">{rule}</p>
                    {evidence && (
                      <p className="text-xs text-muted-foreground">
                        From feedback: &ldquo;{evidence.length > 120 ? evidence.slice(0, 120) + "…" : evidence}&rdquo;
                      </p>
                    )}
                  </div>
                );
              })()}
            </>
          )}
          {/* Footer row: time + buttons */}
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs text-muted-foreground/70">
              {relativeTime(action.created_at)}
            </span>
            {isEditing ? (
              /* Edit mode buttons */
              <div className="flex gap-2">
                <Button
                  size="sm"
                  onClick={() => onSave(action.id)}
                  disabled={isSaving}
                >
                  {isSaving && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
                  Save
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={onCancelEdit}
                  disabled={isSaving}
                >
                  Cancel
                </Button>
              </div>
            ) : (
              action.status === "pending" ? (
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    onClick={() => onApprove(action.id)}
                  >
                    Approve
                  </Button>
                  {action.action_type !== "research_proposal" && (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => onEditFirst(action)}
                    >
                      Edit First
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-destructive hover:text-destructive hover:bg-destructive/10"
                    onClick={() => onDismiss(action.id)}
                  >
                    Dismiss
                  </Button>
                </div>
              ) : action.status === "dismissed" ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => onRestore(action.id)}
                >
                  Restore
                </Button>
              ) : null
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Type-specific edit forms
// ---------------------------------------------------------------------------

interface EditFormProps {
  actionType: string;
  editDraft: { title: string; content: Record<string, string> };
  gmailAccounts: string[];
  calendarList: { id: string; summary: string; primary: boolean }[];
  onUpdateDraft: (field: string, value: string) => void;
}

function EditForm({ actionType, editDraft, gmailAccounts, calendarList, onUpdateDraft }: EditFormProps) {
  if (actionType === "email_draft") {
    return (
      <div className="space-y-3">
        <div>
          <label className="text-xs text-muted-foreground">From</label>
          {gmailAccounts.length === 0 ? (
            <p className="text-xs text-muted-foreground mt-1">No Gmail accounts connected</p>
          ) : (
            <select
              value={editDraft.content.gmail_account ?? ""}
              onChange={(e) => onUpdateDraft("gmail_account", e.target.value)}
              className="w-full mt-1 rounded-md border border-input bg-background px-3 py-1.5 text-sm shadow-sm focus:outline-none focus:ring-1 focus:ring-ring"
            >
              {gmailAccounts.map((email) => (
                <option key={email} value={email}>{email}</option>
              ))}
            </select>
          )}
        </div>
        <div>
          <label className="text-xs text-muted-foreground">To</label>
          <Input
            value={editDraft.content.to ?? ""}
            onChange={(e) => onUpdateDraft("to", e.target.value)}
          />
        </div>
        <div>
          <label className="text-xs text-muted-foreground">Subject</label>
          <Input
            value={editDraft.content.subject ?? ""}
            onChange={(e) => onUpdateDraft("subject", e.target.value)}
          />
        </div>
        <div>
          <label className="text-xs text-muted-foreground">Body</label>
          <Textarea
            rows={6}
            value={editDraft.content.body ?? ""}
            onChange={(e) => onUpdateDraft("body", e.target.value)}
          />
        </div>
      </div>
    );
  }

  if (actionType === "calendar_proposal") {
    return (
      <div className="space-y-3">
        {calendarList.length > 0 && (
          <div>
            <label className="text-xs text-muted-foreground">Calendar</label>
            <select
              value={editDraft.content.calendar_id || "primary"}
              onChange={(e) => onUpdateDraft("calendar_id", e.target.value)}
              className="w-full mt-1 rounded-md border border-input bg-background px-3 py-1.5 text-sm shadow-sm focus:outline-none focus:ring-1 focus:ring-ring"
            >
              {calendarList.map((cal) => (
                <option key={cal.id} value={cal.id}>
                  {cal.summary}{cal.primary ? " (primary)" : ""}
                </option>
              ))}
            </select>
          </div>
        )}
        <div>
          <label className="text-xs text-muted-foreground">Title</label>
          <Input
            value={editDraft.content.title ?? ""}
            onChange={(e) => onUpdateDraft("title", e.target.value)}
          />
        </div>
        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="text-xs text-muted-foreground">Start</label>
            <Input
              type="datetime-local"
              value={editDraft.content.start_datetime ?? ""}
              onChange={(e) => onUpdateDraft("start_datetime", e.target.value)}
            />
          </div>
          <div>
            <label className="text-xs text-muted-foreground">End</label>
            <Input
              type="datetime-local"
              value={editDraft.content.end_datetime ?? ""}
              onChange={(e) => onUpdateDraft("end_datetime", e.target.value)}
            />
          </div>
        </div>
        <div>
          <label className="text-xs text-muted-foreground">Location</label>
          <Input
            value={editDraft.content.location ?? ""}
            onChange={(e) => onUpdateDraft("location", e.target.value)}
          />
        </div>
        <div>
          <label className="text-xs text-muted-foreground">Description</label>
          <Textarea
            rows={3}
            value={editDraft.content.description ?? ""}
            onChange={(e) => onUpdateDraft("description", e.target.value)}
          />
        </div>
      </div>
    );
  }

  if (actionType === "task_proposal") {
    return (
      <div className="space-y-3">
        <div>
          <label className="text-xs text-muted-foreground">Title</label>
          <Input
            value={editDraft.content.title ?? ""}
            onChange={(e) => onUpdateDraft("title", e.target.value)}
          />
        </div>
        <div>
          <label className="text-xs text-muted-foreground">Due Date</label>
          <Input
            type="date"
            value={editDraft.content.due_date ?? ""}
            onChange={(e) => onUpdateDraft("due_date", e.target.value)}
          />
        </div>
        <div>
          <label className="text-xs text-muted-foreground">Description</label>
          <Textarea
            rows={3}
            value={editDraft.content.description ?? ""}
            onChange={(e) => onUpdateDraft("description", e.target.value)}
          />
        </div>
      </div>
    );
  }

  // Fallback for unknown types
  return (
    <div>
      <label className="text-xs text-muted-foreground">Content (JSON)</label>
      <Textarea
        rows={4}
        value={JSON.stringify(editDraft.content, null, 2)}
        onChange={(e) => {
          try {
            const parsed = JSON.parse(e.target.value) as Record<string, string>;
            Object.entries(parsed).forEach(([k, v]) => onUpdateDraft(k, v));
          } catch {
            // Invalid JSON — ignore
          }
        }}
      />
    </div>
  );
}
