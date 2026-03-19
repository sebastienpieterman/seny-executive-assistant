import { useEffect, useState, useMemo } from "react";
import { Search } from "lucide-react";
import { api } from "@/lib/api";
import { Input } from "@/components/ui/input";
import { ConversationListSkeleton } from "@/components/ui/LoadingSkeletons";
import {
  ConversationItem,
  type Conversation,
} from "./ConversationItem";

interface ConversationsResponse {
  conversations: Conversation[];
}

interface ConversationListProps {
  activeId?: string | null;
  onSelect?: (id: string) => void;
}

export function ConversationList({
  activeId = null,
  onSelect,
}: ConversationListProps) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<ConversationsResponse>("/api/conversations")
      .then((data) => {
        // Sort by updated_at descending
        const sorted = [...(data.conversations ?? [])].sort(
          (a, b) =>
            new Date(b.updated_at).getTime() -
            new Date(a.updated_at).getTime()
        );
        setConversations(sorted);
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  const filtered = useMemo(() => {
    if (!search.trim()) return conversations;
    const q = search.toLowerCase();
    return conversations.filter((c) =>
      (c.title ?? "").toLowerCase().includes(q)
    );
  }, [conversations, search]);

  function handleDelete(id: string) {
    api
      .delete(`/api/conversations/${id}`)
      .then(() =>
        setConversations((prev) => prev.filter((c) => c.id !== id))
      )
      .catch(() => {
        /* silently fail for now */
      });
  }

  function handleRename(id: string, newTitle: string) {
    api
      .patch(`/api/conversations/${id}/title`, { title: newTitle })
      .then(() =>
        setConversations((prev) =>
          prev.map((c) => (c.id === id ? { ...c, title: newTitle } : c))
        )
      )
      .catch(() => {
        /* silently fail — title reverts on next load */
      });
  }

  return (
    <div className="flex h-full flex-col gap-3">
      {/* Search */}
      <div className="relative">
        <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search conversations..."
          className="pl-9"
        />
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto pr-2">
        {loading ? (
          <ConversationListSkeleton />
        ) : error ? (
          <p className="px-3 py-4 text-sm text-destructive">
            Error: {error}
          </p>
        ) : filtered.length === 0 ? (
          <p className="px-3 py-4 text-sm text-muted-foreground">
            {search ? "No matches." : "No conversations yet."}
          </p>
        ) : (
          <div className="flex flex-col gap-0.5">
            {filtered.map((c) => (
              <ConversationItem
                key={c.id}
                conversation={c}
                isActive={c.id === activeId}
                onSelect={onSelect ?? (() => {})}
                onDelete={handleDelete}
                onRename={handleRename}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
