import { useState, useEffect } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { toast } from "sonner";
import { Brain, Trash2 } from "lucide-react";

interface Memory {
  id: number;
  memory: string;
  category: string;
  created_at: string;
}

interface MemoriesResponse {
  memories: Memory[];
}

const CATEGORY_COLORS: Record<string, string> = {
  behavior: "bg-blue-500/15 text-blue-400 border-blue-500/30",
  preference: "bg-purple-500/15 text-purple-400 border-purple-500/30",
  fact: "bg-green-500/15 text-green-400 border-green-500/30",
  general: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30",
};

function formatDate(dateStr: string): string {
  try {
    const date = new Date(dateStr);
    return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  } catch {
    return dateStr;
  }
}

export function MemoriesTab() {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(true);
  const [deletingId, setDeletingId] = useState<number | null>(null);

  useEffect(() => {
    loadMemories();
  }, []);

  async function loadMemories() {
    try {
      const data = await api.get<MemoriesResponse>("/api/memories");
      setMemories(data.memories);
    } catch {
      toast.error("Failed to load memories");
    } finally {
      setLoading(false);
    }
  }

  async function handleForget(memoryId: number) {
    setDeletingId(memoryId);
    try {
      await api.delete(`/api/memories/${memoryId}`);
      setMemories((prev) => prev.filter((m) => m.id !== memoryId));
      toast.success("Memory deleted");
    } catch {
      toast.error("Failed to delete memory");
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-lg font-semibold">What Seny Knows</h3>
        <p className="text-sm text-muted-foreground mt-1">
          Seny saves lessons from your corrections and applies them to every
          future conversation.
        </p>
      </div>

      {loading ? (
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="h-14 animate-pulse rounded-md bg-muted"
            />
          ))}
        </div>
      ) : memories.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-border py-12 text-center">
          <Brain className="h-8 w-8 text-muted-foreground/50" />
          <div>
            <p className="text-sm font-medium text-muted-foreground">
              Seny hasn't learned anything yet.
            </p>
            <p className="text-xs text-muted-foreground/70 mt-1">
              Correct Seny in chat and it will save what it learns here.
            </p>
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          {memories.map((memory) => (
            <div
              key={memory.id}
              className="flex items-start gap-3 rounded-lg border border-border bg-card p-3"
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1 flex-wrap">
                  <Badge
                    variant="outline"
                    className={`text-xs capitalize border ${
                      CATEGORY_COLORS[memory.category] ??
                      CATEGORY_COLORS.general
                    }`}
                  >
                    {memory.category}
                  </Badge>
                  <span className="text-xs text-muted-foreground">
                    {formatDate(memory.created_at)}
                  </span>
                </div>
                <p className="text-sm text-foreground leading-snug">
                  {memory.memory}
                </p>
              </div>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 shrink-0 text-muted-foreground hover:text-destructive hover:bg-destructive/10"
                disabled={deletingId === memory.id}
                onClick={() => handleForget(memory.id)}
                title="Forget this"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
