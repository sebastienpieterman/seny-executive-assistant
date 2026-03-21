import { useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { api } from "@/lib/api";
import { formatDistanceToNow } from "date-fns";
import { Trash2, Users, FolderOpen, Lightbulb, Eye, EyeOff } from "lucide-react";

interface CaptureEntry {
  id: number;
  original_text: string;
  classification: string;
  confidence: number | null;
  routed_to_table: string | null;
  routed_to_id: number | null;
  item_name: string | null;
  item_category: string | null;
  created_at: string;
}

interface CaptureHistoryResponse {
  captures: CaptureEntry[];
  count: number;
}

interface CaptureHistoryProps {
  onItemClick?: (category: string, id: number) => void;
}

const CATEGORY_CONFIG: Record<string, { label: string; icon: React.ReactNode; color: string }> = {
  people:  { label: "People",  icon: <Users className="h-3 w-3" />,       color: "bg-blue-500/15 text-blue-400 border-blue-500/30" },
  project: { label: "Project", icon: <FolderOpen className="h-3 w-3" />,  color: "bg-purple-500/15 text-purple-400 border-purple-500/30" },
  idea:    { label: "Idea",    icon: <Lightbulb className="h-3 w-3" />,   color: "bg-yellow-500/15 text-yellow-400 border-yellow-500/30" },
  none:    { label: "Ignored", icon: null,                                  color: "bg-muted/50 text-muted-foreground border-border" },
};

function ConfidenceBadge({ value }: { value: number | null }) {
  if (value === null) return null;
  const pct = Math.round(value * 100);
  const color = pct >= 80 ? "text-green-400" : pct >= 50 ? "text-yellow-400" : "text-red-400";
  return <span className={`text-xs ${color}`}>{pct}%</span>;
}

export function CaptureHistory({ onItemClick }: CaptureHistoryProps) {
  const [entries, setEntries] = useState<CaptureEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [showIgnored, setShowIgnored] = useState(false);

  const load = async (includeIgnored: boolean) => {
    setLoading(true);
    try {
      const data = await api.get<CaptureHistoryResponse>(
        `/api/second-brain/captures?include_ignored=${includeIgnored}&limit=100`
      );
      setEntries(data.captures || []);
    } catch {
      setEntries([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(showIgnored); }, [showIgnored]);

  const handleDelete = async (id: number) => {
    try {
      await api.delete(`/api/second-brain/captures/${id}`);
      setEntries((prev) => prev.filter((e) => e.id !== id));
    } catch {
      // ignore
    }
  };

  const toggleShowIgnored = () => setShowIgnored((v) => !v);

  if (loading) {
    return <div className="p-4 text-muted-foreground">Loading...</div>;
  }

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-border">
        <p className="text-sm text-muted-foreground">
          {entries.length} {showIgnored ? "entries" : "saved captures"}
        </p>
        <Button variant="ghost" size="sm" onClick={toggleShowIgnored} className="gap-1.5 text-xs">
          {showIgnored ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
          {showIgnored ? "Hide ignored" : "Show ignored"}
        </Button>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto p-4 space-y-2">
        {entries.length === 0 ? (
          <div className="text-muted-foreground">
            <p>No captures yet.</p>
            <p className="text-sm mt-2">
              When you mention people, projects, ideas, or tasks in chat, Seny will
              automatically capture them here.
            </p>
          </div>
        ) : (
          entries.map((entry) => {
            const cfg = CATEGORY_CONFIG[entry.classification] ?? CATEGORY_CONFIG.none;
            return (
              <Card key={entry.id}>
                <CardContent className="flex items-start gap-3 p-3">
                  <div className="flex-1 min-w-0 space-y-1.5">
                    {/* Header row: badge + confidence + time */}
                    <div className="flex items-center gap-2 flex-wrap">
                      <Badge
                        variant="outline"
                        className={`gap-1 text-xs px-1.5 py-0.5 ${cfg.color}`}
                      >
                        {cfg.icon}
                        {cfg.label}
                      </Badge>
                      <ConfidenceBadge value={entry.confidence} />
                      <span className="text-xs text-muted-foreground ml-auto">
                        {formatDistanceToNow(new Date(entry.created_at), { addSuffix: true })}
                      </span>
                    </div>

                    {/* Item name — clickable if we have a link target */}
                    {entry.item_name ? (
                      onItemClick && entry.item_category && entry.routed_to_id ? (
                        <button
                          onClick={() => onItemClick(entry.item_category!, entry.routed_to_id!)}
                          className="text-sm font-medium text-left hover:underline text-foreground"
                        >
                          {entry.item_name}
                        </button>
                      ) : (
                        <p className="text-sm font-medium">{entry.item_name}</p>
                      )
                    ) : null}

                    {/* Original message */}
                    <p className="text-sm text-muted-foreground leading-snug line-clamp-2">
                      {entry.original_text}
                    </p>
                  </div>

                  {/* Delete button — removes the log entry only, not the saved item */}
                  <Button
                    variant="ghost"
                    size="sm"
                    className="flex-shrink-0 text-muted-foreground hover:text-destructive"
                    onClick={() => handleDelete(entry.id)}
                    title="Remove from capture log"
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </CardContent>
              </Card>
            );
          })
        )}
      </div>
    </div>
  );
}
