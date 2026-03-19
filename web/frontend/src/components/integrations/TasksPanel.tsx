import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { CheckSquare, RefreshCw } from "lucide-react";
import { TaskListSkeleton } from "@/components/ui/LoadingSkeletons";
import { ErrorState } from "@/components/ui/ErrorState";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import { TaskItem, type Task } from "./TaskItem";
import { AddTaskDialog } from "./AddTaskDialog";

type TaskFilter = "all" | "today" | "overdue" | "week";

const FILTERS: { value: TaskFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "today", label: "Today" },
  { value: "overdue", label: "Overdue" },
  { value: "week", label: "Week" },
];

function filterUrl(filter: TaskFilter): string {
  switch (filter) {
    case "today": return "/api/tasks/today";
    case "overdue": return "/api/tasks/overdue";
    case "week": return "/api/tasks/upcoming?days=7";
    default: return "/api/tasks";
  }
}

export function TasksPanel() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [filter, setFilter] = useState<TaskFilter>("all");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchTasks = useCallback(async (activeFilter: TaskFilter) => {
    setRefreshing(true);
    setError(null);
    try {
      const data = await api.get<{ tasks: Task[] }>(filterUrl(activeFilter));
      setTasks(data.tasks || []);
    } catch (err) {
      console.error("Failed to fetch tasks:", err);
      setError("Failed to load tasks.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchTasks(filter);
  }, [filter, fetchTasks]);

  function refresh() {
    fetchTasks(filter);
  }

  // Loading state
  if (loading) {
    return (
      <div className="flex h-full flex-col overflow-hidden">
        <div className="shrink-0 border-b border-border px-4 py-3">
          <h2 className="text-base font-semibold">Tasks</h2>
        </div>
        <TaskListSkeleton />
      </div>
    );
  }

  // Error state
  if (error) {
    return (
      <div className="flex h-full flex-col overflow-hidden">
        <div className="shrink-0 border-b border-border px-4 py-3">
          <h2 className="text-base font-semibold">Tasks</h2>
        </div>
        <ErrorState message={error} onRetry={refresh} />
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Header */}
      <div className="shrink-0 border-b border-border px-4 py-3">
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold">Tasks</h2>
          <div className="flex items-center gap-1">
            <AddTaskDialog onTaskCreated={refresh} />
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={refresh}
              disabled={refreshing}
            >
              <RefreshCw className={cn("h-4 w-4", refreshing && "animate-spin")} />
            </Button>
          </div>
        </div>

        {/* Filter tabs */}
        <div className="mt-2 flex gap-1">
          {FILTERS.map((f) => (
            <button
              key={f.value}
              onClick={() => setFilter(f.value)}
              className={cn(
                "rounded-full px-3 py-1 text-xs font-medium transition-colors",
                filter === f.value
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-sidebar-accent/50 hover:text-foreground"
              )}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>

      {/* Task list */}
      <ScrollArea className="flex-1 min-h-0">
        {tasks.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
            <CheckSquare className="mb-2 h-8 w-8 opacity-50" />
            <p className="text-sm">
              {filter === "all" ? "No tasks yet" : `No ${filter} tasks`}
            </p>
            <p className="mt-1 text-xs text-muted-foreground/70">
              Click + to add one, or ask Seny to create a task
            </p>
          </div>
        ) : (
          <div className="py-1">
            {tasks.map((task) => (
              <TaskItem
                key={task.id}
                task={task}
                onToggled={(updatedTask) => {
                  setTasks((prev) =>
                    prev.map((t) => (t.id === updatedTask.id ? updatedTask : t))
                  );
                }}
                onDeleted={refresh}
              />
            ))}
          </div>
        )}
      </ScrollArea>
    </div>
  );
}
