import { useState } from "react";
import { api } from "@/lib/api";
import { Checkbox } from "@/components/ui/checkbox";
import { Badge } from "@/components/ui/badge";
import { ChevronDown, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface Task {
  id: number;
  title: string;
  description?: string;
  status: string;
  priority: string;
  due_date?: string;
  category?: string;
  created_at?: string;
}

interface TaskItemProps {
  task: Task;
  onToggled: (updatedTask: Task) => void;
  onDeleted: () => void;
}

function formatDue(dueDate: string | undefined): { text: string; variant: "overdue" | "today" | "normal" } | null {
  if (!dueDate) return null;

  const due = new Date(dueDate);
  if (isNaN(due.getTime())) return null;
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const dueDay = new Date(due.getFullYear(), due.getMonth(), due.getDate());
  const diffDays = Math.floor((dueDay.getTime() - today.getTime()) / (1000 * 60 * 60 * 24));

  if (diffDays < 0) {
    const abs = Math.abs(diffDays);
    return { text: `${abs}d overdue`, variant: "overdue" };
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

function priorityColor(priority: string): string {
  switch (priority) {
    case "urgent": return "text-red-500";
    case "high": return "text-orange-400";
    case "medium": return "text-blue-400";
    case "low": return "text-muted-foreground";
    default: return "text-muted-foreground";
  }
}

export function TaskItem({ task, onToggled, onDeleted }: TaskItemProps) {
  const [completing, setCompleting] = useState(false);
  const [showDelete, setShowDelete] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const isCompleted = task.status === "completed";
  const [justCompleted, setJustCompleted] = useState(false);
  const dueInfo = formatDue(task.due_date);

  async function handleToggle() {
    setCompleting(true);
    // Optimistic update: toggle status locally immediately
    const newStatus = isCompleted ? "active" : "completed";
    if (newStatus === "completed") {
      setJustCompleted(true);
      setTimeout(() => setJustCompleted(false), 600);
    }
    onToggled({ ...task, status: newStatus });
    try {
      const endpoint = isCompleted
        ? `/api/tasks/${task.id}/reopen`
        : `/api/tasks/${task.id}/complete`;
      await api.post(endpoint);
    } catch (err) {
      console.error("Failed to toggle task:", err);
      // Revert on failure
      onToggled(task);
    } finally {
      setCompleting(false);
    }
  }

  async function handleDelete() {
    try {
      await api.delete(`/api/tasks/${task.id}`);
      onDeleted();
    } catch (err) {
      console.error("Failed to delete task:", err);
    }
  }

  return (
    <div
      className={cn(
        "group rounded-lg transition-all hover:bg-sidebar-accent/30",
        justCompleted && "bg-green-500/10 ring-1 ring-green-500/20"
      )}
      onMouseEnter={() => setShowDelete(true)}
      onMouseLeave={() => setShowDelete(false)}
    >
      <div
        className="flex items-start gap-3 px-4 py-2.5 cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <div onClick={(e) => e.stopPropagation()}>
          <Checkbox
            checked={isCompleted}
            onCheckedChange={handleToggle}
            disabled={completing}
            className="mt-0.5 shrink-0"
          />
        </div>
        <div className="flex-1 min-w-0">
          <p className={cn(
            "text-sm leading-snug",
            isCompleted && "line-through text-muted-foreground"
          )}>
            {task.title}
          </p>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            {task.priority && task.priority !== "medium" && (
              <span className={cn("text-xs font-medium capitalize", priorityColor(task.priority))}>
                {task.priority}
              </span>
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
            {task.category && (
              <span className="text-[10px] text-muted-foreground">#{task.category}</span>
            )}
          </div>
        </div>
        <ChevronDown className={cn(
          "h-4 w-4 shrink-0 text-muted-foreground transition-transform",
          expanded && "rotate-180"
        )} />
        {showDelete && (
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 shrink-0 text-muted-foreground hover:text-red-400"
            onClick={(e) => {
              e.stopPropagation();
              handleDelete();
            }}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>
      {expanded && (
        <div className="px-4 pb-3 pl-11 space-y-1.5 text-xs text-muted-foreground">
          {task.description && (
            <p className="text-sm text-foreground/80">{task.description}</p>
          )}
          {task.due_date && (
            <p>Due: {new Date(task.due_date).toLocaleString("en-US", {
              weekday: "short", month: "short", day: "numeric",
              hour: "numeric", minute: "2-digit",
            })}</p>
          )}
          <p>Priority: <span className={cn("capitalize", priorityColor(task.priority))}>{task.priority}</span></p>
          {task.created_at && (
            <p>Created: {new Date(task.created_at).toLocaleDateString("en-US", {
              month: "short", day: "numeric", year: "numeric",
            })}</p>
          )}
          {!task.description && !task.due_date && (
            <p className="italic">No additional details</p>
          )}
        </div>
      )}
    </div>
  );
}
