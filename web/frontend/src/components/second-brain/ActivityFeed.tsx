import { useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatDistanceToNow } from "date-fns";
import { Mail, MessageSquare, Send, Trash2 } from "lucide-react";

interface ActivityItem {
  id: number;
  person_id: number;
  person_name: string; // Joined from people table
  action_type: string;
  old_value: string | null;
  new_value: string;
  context_added: string | null; // AI-extracted note (if any)
  source: string;
  source_context: {
    sender?: string;
    snippet?: string;
    timestamp?: string;
  } | null;
  created_at: string;
  deleted_at: string | null;
}

interface ActivityFeedResponse {
  activities: ActivityItem[];
  count: number;
}

interface ActivityFeedProps {
  onPersonClick?: (personId: number) => void;
}

export function ActivityFeed({ onPersonClick }: ActivityFeedProps) {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [loading, setLoading] = useState(true);

  const loadFeed = async () => {
    try {
      const data = await api.get<ActivityFeedResponse>("/api/activity/feed?limit=100");
      setItems(data.activities || []);
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadFeed();
  }, []);

  const handleDelete = async (id: number) => {
    try {
      await api.delete(`/api/activity/${id}`);
      loadFeed();
    } catch {
      // ignore errors
    }
  };

  const getSourceIcon = (source: string) => {
    switch (source) {
      case "gmail":
        return <Mail className="h-4 w-4" />;
      case "slack":
        return <MessageSquare className="h-4 w-4" />;
      case "telegram":
        return <Send className="h-4 w-4" />;
      default:
        return null;
    }
  };

  if (loading) {
    return <div className="p-4 text-muted-foreground">Loading...</div>;
  }

  if (items.length === 0) {
    return (
      <div className="p-4 text-muted-foreground">
        <p>No automated activity yet.</p>
        <p className="text-sm mt-2">
          When people in your tracker email, Slack, or Telegram you, their
          contact info will be updated automatically and shown here.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-2 p-4 overflow-y-auto h-full">
      {items.map((item) => (
        <Card
          key={item.id}
          className={item.deleted_at ? "opacity-50" : ""}
        >
          <CardContent className="flex items-start gap-3 p-3">
            <div className="flex-shrink-0 mt-1 text-muted-foreground">
              {getSourceIcon(item.source)}
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <button
                  onClick={() => onPersonClick?.(item.person_id)}
                  className="font-medium hover:underline"
                >
                  {item.person_name}
                </button>
                <span className="text-muted-foreground text-sm">
                  → Last contact {item.new_value}
                </span>
              </div>

              {/* Show AI-extracted context if present */}
              {item.context_added && (
                <div className="text-sm mt-1 p-2 bg-muted/50 rounded border-l-2 border-primary/30">
                  📝 {item.context_added}
                </div>
              )}

              {/* Show source info */}
              {item.source_context?.sender && (
                <div className="text-sm text-muted-foreground mt-1">
                  From: {item.source_context.sender}
                </div>
              )}
              {item.source_context?.snippet && !item.context_added && (
                <div className="text-xs text-muted-foreground mt-1 truncate italic">
                  "{item.source_context.snippet}"
                </div>
              )}

              <div className="text-xs text-muted-foreground mt-1">
                {formatDistanceToNow(new Date(item.created_at), {
                  addSuffix: true,
                })}
              </div>
            </div>
            <div className="flex-shrink-0">
              {!item.deleted_at && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => handleDelete(item.id)}
                  title="Remove this update"
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              )}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
