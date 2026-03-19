import { useState, useEffect } from "react";
import { Pencil, Trash2, ArrowRightLeft, Activity, Undo2, Mail, MessageSquare, Send } from "lucide-react";
import { formatDistanceToNow } from "date-fns";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { ReclassifyDialog } from "./ReclassifyDialog";
import type { SecondBrainItem } from "./ItemList";
import { cn } from "@/lib/utils";

const CATEGORY_COLORS: Record<string, string> = {
  people: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  projects: "bg-green-500/20 text-green-400 border-green-500/30",
  ideas: "bg-purple-500/20 text-purple-400 border-purple-500/30",
  admin: "bg-orange-500/20 text-orange-400 border-orange-500/30",
};

interface ActivityItem {
  id: number;
  person_id: number;
  person_name: string;
  action_type: string;
  old_value: string | null;
  new_value: string;
  context_added: string | null;
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

interface ItemDetailProps {
  item: SecondBrainItem;
  onSave: (data: Record<string, string>) => void;
  onDelete: () => void;
  onReclassify: (targetCategory: string) => void;
  onRefresh?: () => void;
}

interface FieldDef {
  key: string;
  label: string;
  type: "text" | "textarea";
}

function getFields(category: string): FieldDef[] {
  switch (category) {
    case "people":
      return [
        { key: "name", label: "Name", type: "text" },
        { key: "relationship_type", label: "Relationship Type", type: "text" },
        { key: "context", label: "Context", type: "text" },
        { key: "notes", label: "Notes", type: "textarea" },
      ];
    case "projects":
      return [
        { key: "name", label: "Name", type: "text" },
        { key: "status", label: "Status", type: "text" },
        { key: "next_action", label: "Next Action", type: "text" },
        { key: "notes", label: "Notes", type: "textarea" },
      ];
    case "ideas":
      return [
        { key: "title", label: "Title", type: "text" },
        { key: "summary", label: "Summary", type: "text" },
        { key: "notes", label: "Notes", type: "textarea" },
        { key: "tags", label: "Tags", type: "text" },
      ];
    case "admin":
      return [
        { key: "title", label: "Title", type: "text" },
        { key: "status", label: "Status", type: "text" },
        { key: "due_date", label: "Due Date", type: "text" },
        { key: "notes", label: "Notes", type: "textarea" },
      ];
    default:
      return [];
  }
}

export function ItemDetail({ item, onSave, onDelete, onReclassify, onRefresh }: ItemDetailProps) {
  const [editing, setEditing] = useState(false);
  const [reclassifyOpen, setReclassifyOpen] = useState(false);
  const [formData, setFormData] = useState<Record<string, string>>({});
  const [activityHistory, setActivityHistory] = useState<ActivityItem[]>([]);

  const fields = getFields(item.category);
  const displayName = item.name || item.title || "Untitled";

  const startEdit = () => {
    const data: Record<string, string> = {};
    for (const f of fields) {
      data[f.key] = ((item as unknown as Record<string, unknown>)[f.key] as string) || "";
    }
    setFormData(data);
    setEditing(true);
  };

  const saveEdit = () => {
    onSave(formData);
    setEditing(false);
  };

  const cancelEdit = () => setEditing(false);

  // Load activity history for people
  const loadActivityHistory = async () => {
    if (item?.category === "people" && item?.id) {
      try {
        const data = await api.get<ActivityFeedResponse>(
          `/api/activity/person/${item.id}?include_deleted=true`
        );
        setActivityHistory(data.activities || []);
      } catch {
        setActivityHistory([]);
      }
    } else {
      setActivityHistory([]);
    }
  };

  useEffect(() => {
    loadActivityHistory();
  }, [item?.id, item?.category]);

  const handleUndo = async (activityId: number) => {
    try {
      await api.post(`/api/activity/${activityId}/undo`);
      // Refresh both activity history and parent data
      loadActivityHistory();
      onRefresh?.();
    } catch {
      // ignore errors
    }
  };

  const getSourceIcon = (source: string) => {
    switch (source) {
      case "gmail":
        return <Mail className="h-3 w-3" />;
      case "slack":
        return <MessageSquare className="h-3 w-3" />;
      case "telegram":
        return <Send className="h-3 w-3" />;
      default:
        return null;
    }
  };

  if (editing) {
    return (
      <div className="flex h-full flex-col overflow-y-auto p-6">
        <div className="mb-6 flex items-center justify-between">
          <h2 className="text-lg font-semibold">Edit {item.category}</h2>
          <div className="flex gap-2">
            <Button size="sm" onClick={saveEdit}>Save</Button>
            <Button size="sm" variant="ghost" onClick={cancelEdit}>Cancel</Button>
          </div>
        </div>
        <div className="space-y-4">
          {fields.map((f) => (
            <div key={f.key}>
              <Label className="mb-1.5 block text-xs text-muted-foreground">{f.label}</Label>
              {f.type === "textarea" ? (
                <Textarea
                  value={formData[f.key] || ""}
                  onChange={(e) => setFormData((d) => ({ ...d, [f.key]: e.target.value }))}
                  rows={4}
                />
              ) : (
                <Input
                  value={formData[f.key] || ""}
                  onChange={(e) => setFormData((d) => ({ ...d, [f.key]: e.target.value }))}
                />
              )}
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-y-auto p-6">
      {/* Header */}
      <div className="mb-6">
        <div className="mb-2 flex items-center gap-2">
          <Badge variant="outline" className={cn("text-xs", CATEGORY_COLORS[item.category])}>
            {item.category}
          </Badge>
          {item.confidence != null && (
            <span className={cn(
              "text-xs",
              item.confidence >= 0.8 ? "text-green-400" :
              item.confidence >= 0.6 ? "text-yellow-400" : "text-red-400"
            )}>
              {Math.round(item.confidence * 100)}% confidence
            </span>
          )}
        </div>
        <h2 className="text-xl font-semibold text-foreground">{displayName}</h2>
        {item.created_at && (
          <p className="mt-1 text-xs text-muted-foreground">
            Created: {new Date(item.created_at).toLocaleDateString()}
          </p>
        )}
      </div>

      {/* Actions */}
      <div className="mb-6 flex gap-2">
        <Button size="sm" variant="outline" onClick={startEdit} className="gap-1.5">
          <Pencil className="h-3.5 w-3.5" /> Edit
        </Button>
        <Button size="sm" variant="outline" onClick={() => setReclassifyOpen(true)} className="gap-1.5">
          <ArrowRightLeft className="h-3.5 w-3.5" /> Reclassify
        </Button>
        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button size="sm" variant="outline" className="gap-1.5 text-red-400 hover:text-red-300 border-red-500/30 hover:border-red-500/50">
              <Trash2 className="h-3.5 w-3.5" /> Delete
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent className="bg-[#1a1a1a] border-border">
            <AlertDialogHeader>
              <AlertDialogTitle>Delete item?</AlertDialogTitle>
              <AlertDialogDescription>
                This will permanently delete "{displayName}". This cannot be undone.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction onClick={onDelete} className="bg-red-600 hover:bg-red-700">
                Delete
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>

      {/* Fields */}
      <div className="space-y-4">
        {fields.map((f) => {
          const val = (item as unknown as Record<string, unknown>)[f.key];
          if (!val) return null;
          return (
            <div key={f.key}>
              <Label className="mb-1 block text-xs text-muted-foreground">{f.label}</Label>
              <p className="text-sm text-foreground whitespace-pre-wrap">{String(val)}</p>
            </div>
          );
        })}

        {/* People follow-ups */}
        {item.category === "people" && item.followups && item.followups.length > 0 && (
          <div>
            <Label className="mb-1 block text-xs text-muted-foreground">Follow-ups</Label>
            <ul className="space-y-1">
              {item.followups.map((f, i) => (
                <li key={i} className="text-sm text-foreground">
                  {f.description || f.title || ""}{" "}
                  <span className="text-xs text-muted-foreground">({f.status})</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* People last contact */}
        {item.category === "people" && item.last_contact_date && (
          <div>
            <Label className="mb-1 block text-xs text-muted-foreground">Last Contact</Label>
            <p className="text-sm text-foreground">{item.last_contact_date}</p>
          </div>
        )}
      </div>

      {/* Activity History - only for People */}
      {item.category === "people" && activityHistory.length > 0 && (
        <div className="mt-6">
          <h3 className="text-sm font-medium mb-3 flex items-center gap-2">
            <Activity className="h-4 w-4" />
            Activity History
          </h3>
          <div className="space-y-2">
            {activityHistory.map((activity) => (
              <div
                key={activity.id}
                className={cn(
                  "text-sm p-3 rounded border",
                  activity.deleted_at
                    ? "opacity-50 bg-muted/30 border-muted"
                    : "bg-card border-border"
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex-1 flex items-center gap-2 min-w-0">
                    <span className="flex-shrink-0 text-muted-foreground">
                      {getSourceIcon(activity.source)}
                    </span>
                    <span className="font-medium truncate">
                      Last contact → {activity.new_value}
                    </span>
                    {activity.old_value && (
                      <span className="text-muted-foreground text-xs">
                        (was {activity.old_value})
                      </span>
                    )}
                  </div>
                  {!activity.deleted_at && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleUndo(activity.id)}
                      title="Undo this change"
                      className="flex-shrink-0 h-7 w-7 p-0"
                    >
                      <Undo2 className="h-3 w-3" />
                    </Button>
                  )}
                </div>

                {/* Show AI-extracted context if present */}
                {activity.context_added && (
                  <div className="text-sm mt-2 p-2 bg-muted/50 rounded text-muted-foreground border-l-2 border-primary/30">
                    📝 {activity.context_added}
                  </div>
                )}

                {activity.source_context?.sender && (
                  <div className="text-xs text-muted-foreground mt-2">
                    From: {activity.source_context.sender}
                  </div>
                )}
                <div className="text-xs text-muted-foreground mt-1">
                  {formatDistanceToNow(new Date(activity.created_at), { addSuffix: true })}
                  {activity.deleted_at && (
                    <span className="ml-2 text-yellow-500">(undone)</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Capture info */}
      {(item.confidence != null || item.original_text) && (
        <div className="mt-6 rounded-lg border border-border bg-accent/20 p-4">
          <h3 className="mb-2 text-sm font-medium text-muted-foreground">Capture Info</h3>
          {item.original_text && (
            <p className="text-sm italic text-muted-foreground">"{item.original_text}"</p>
          )}
        </div>
      )}

      {/* Reclassify dialog */}
      <ReclassifyDialog
        open={reclassifyOpen}
        currentCategory={item.category}
        onConfirm={(target) => {
          setReclassifyOpen(false);
          onReclassify(target);
        }}
        onCancel={() => setReclassifyOpen(false)}
      />
    </div>
  );
}
