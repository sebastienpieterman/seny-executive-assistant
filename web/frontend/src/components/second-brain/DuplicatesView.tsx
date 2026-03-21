import { useState, useEffect, useCallback } from "react";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { MergeDialog } from "./MergeDialog";
import { api } from "@/lib/api";
import { toast } from "sonner";

const API_BASE = "/api/second-brain";

interface DuplicateItem {
  id: number;
  name?: string;
  title?: string;
}

interface DuplicateGroup {
  items: DuplicateItem[];
  match_type: string;
  confidence: number;
}

interface DuplicatesResponse {
  people?: DuplicateGroup[];
  ideas?: DuplicateGroup[];
}

interface DuplicatesViewProps {
  onBack: () => void;
}

const MATCH_BADGE_COLORS: Record<string, string> = {
  exact: "bg-red-500/10 text-red-600 border-red-200",
  contains: "bg-orange-500/10 text-orange-600 border-orange-200",
  first_name: "bg-yellow-500/10 text-yellow-700 border-yellow-200",
  word_overlap: "bg-yellow-500/10 text-yellow-700 border-yellow-200",
};

export function DuplicatesView({ onBack }: DuplicatesViewProps) {
  const [loading, setLoading] = useState(true);
  const [peopleGroups, setPeopleGroups] = useState<DuplicateGroup[]>([]);
  const [ideasGroups, setIdeasGroups] = useState<DuplicateGroup[]>([]);
  const [selectedWinners, setSelectedWinners] = useState<Record<string, number>>({});
  const [mergeDialog, setMergeDialog] = useState<{
    open: boolean;
    category: string;
    groupKey: string;
    winner: DuplicateItem | null;
    loser: DuplicateItem | null;
  }>({ open: false, category: "", groupKey: "", winner: null, loser: null });

  const loadDuplicates = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.get<DuplicatesResponse>(`${API_BASE}/duplicates`);
      setPeopleGroups(data.people || []);
      setIdeasGroups(data.ideas || []);
    } catch {
      toast.error("Failed to load duplicates");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDuplicates();
  }, [loadDuplicates]);

  const getGroupKey = (category: string, index: number) => `${category}-${index}`;

  const handleSelectWinner = (groupKey: string, id: number) => {
    setSelectedWinners((prev) => ({ ...prev, [groupKey]: id }));
  };

  const handleMerge = (category: string, groupKey: string, group: DuplicateGroup) => {
    const winnerId = selectedWinners[groupKey];
    if (!winnerId) {
      toast.error("Select which record to keep first");
      return;
    }
    const winner = group.items.find((i) => i.id === winnerId) || null;
    const loser = group.items.find((i) => i.id !== winnerId) || null;
    setMergeDialog({ open: true, category, groupKey, winner, loser });
  };

  const confirmMerge = async () => {
    const { category, groupKey, winner, loser } = mergeDialog;
    if (!winner || !loser) return;

    try {
      await api.post(`${API_BASE}/merge`, {
        category,
        winner_id: winner.id,
        loser_id: loser.id,
      });
      toast.success(`Merged "${category === "people" ? loser.name : loser.title}" into "${category === "people" ? winner.name : winner.title}"`);

      // Remove group from list
      if (category === "people") {
        setPeopleGroups((prev) => prev.filter((_, i) => getGroupKey("people", i) !== groupKey));
      } else {
        setIdeasGroups((prev) => prev.filter((_, i) => getGroupKey("ideas", i) !== groupKey));
      }
    } catch {
      toast.error("Merge failed");
    } finally {
      setMergeDialog({ open: false, category: "", groupKey: "", winner: null, loser: null });
    }
  };

  const handleDismiss = async (category: string, groupKey: string, group: DuplicateGroup) => {
    try {
      await api.post(`${API_BASE}/duplicates/dismiss`, {
        category,
        ids: group.items.map((i) => i.id),
      });
      toast.success("Marked as not a duplicate");

      if (category === "people") {
        setPeopleGroups((prev) => prev.filter((_, i) => getGroupKey("people", i) !== groupKey));
      } else {
        setIdeasGroups((prev) => prev.filter((_, i) => getGroupKey("ideas", i) !== groupKey));
      }
    } catch {
      toast.error("Failed to dismiss");
    }
  };

  const renderGroup = (category: string, group: DuplicateGroup, index: number) => {
    const groupKey = getGroupKey(category, index);
    const winnerId = selectedWinners[groupKey];
    const badgeColor = MATCH_BADGE_COLORS[group.match_type] || "bg-muted text-muted-foreground";

    return (
      <Card key={groupKey} className="mb-3">
        <CardContent className="p-4">
          <div className="mb-3 flex items-center gap-2">
            <Badge variant="outline" className={badgeColor}>
              {group.match_type}
            </Badge>
            <span className="text-xs text-muted-foreground">
              {Math.round(group.confidence * 100)}% confidence
            </span>
          </div>

          <div className="space-y-2">
            {group.items.map((item) => {
              const label = category === "people" ? item.name : item.title;
              const isSelected = winnerId === item.id;
              return (
                <button
                  key={item.id}
                  onClick={() => handleSelectWinner(groupKey, item.id)}
                  className={`flex w-full items-center gap-2 rounded-md border px-3 py-2 text-left text-sm transition-colors ${
                    isSelected
                      ? "border-primary bg-primary/5 text-foreground"
                      : "border-border bg-background text-muted-foreground hover:bg-accent/50"
                  }`}
                >
                  <div
                    className={`h-3 w-3 shrink-0 rounded-full border-2 ${
                      isSelected ? "border-primary bg-primary" : "border-muted-foreground/40"
                    }`}
                  />
                  <span className="truncate">{label}</span>
                  <span className="ml-auto text-xs text-muted-foreground">#{item.id}</span>
                </button>
              );
            })}
          </div>

          <div className="mt-3 flex gap-2">
            <Button
              size="sm"
              onClick={() => handleMerge(category, groupKey, group)}
              disabled={!winnerId}
            >
              Merge
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => handleDismiss(category, groupKey, group)}
            >
              Not a Duplicate
            </Button>
          </div>
        </CardContent>
      </Card>
    );
  };

  const totalGroups = peopleGroups.length + ideasGroups.length;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <Button size="icon" variant="ghost" onClick={onBack}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <h2 className="text-sm font-semibold">Find Duplicates</h2>
        {!loading && (
          <span className="text-xs text-muted-foreground">
            {totalGroups} {totalGroups === 1 ? "group" : "groups"} found
          </span>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {loading ? (
          <p className="text-center text-sm text-muted-foreground">Scanning for duplicates...</p>
        ) : totalGroups === 0 ? (
          <p className="text-center text-sm text-muted-foreground">No duplicates found.</p>
        ) : (
          <>
            {peopleGroups.length > 0 && (
              <div className="mb-6">
                <h3 className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  People Duplicates ({peopleGroups.length})
                </h3>
                {peopleGroups.map((group, i) => renderGroup("people", group, i))}
              </div>
            )}
            {ideasGroups.length > 0 && (
              <div>
                <h3 className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Ideas Duplicates ({ideasGroups.length})
                </h3>
                {ideasGroups.map((group, i) => renderGroup("ideas", group, i))}
              </div>
            )}
          </>
        )}
      </div>

      <MergeDialog
        open={mergeDialog.open}
        category={mergeDialog.category}
        winner={mergeDialog.winner}
        loser={mergeDialog.loser}
        onConfirm={confirmMerge}
        onCancel={() => setMergeDialog({ open: false, category: "", groupKey: "", winner: null, loser: null })}
      />
    </div>
  );
}
