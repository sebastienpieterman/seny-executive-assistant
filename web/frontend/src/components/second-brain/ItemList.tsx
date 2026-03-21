import { Brain } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export interface SecondBrainItem {
  id: number;
  category: string;
  name?: string;
  title?: string;
  subtitle?: string;
  confidence?: number | null;
  created_at?: string;
  // category-specific fields
  context?: string;
  notes?: string;
  status?: string;
  next_action?: string;
  summary?: string;
  tags?: string;
  due_date?: string;
  last_contact_date?: string;
  original_text?: string;
  followups?: { description?: string; title?: string; status: string }[];
}

const CATEGORY_COLORS: Record<string, string> = {
  people: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  projects: "bg-green-500/20 text-green-400 border-green-500/30",
  ideas: "bg-purple-500/20 text-purple-400 border-purple-500/30",
};

interface ItemListProps {
  items: SecondBrainItem[];
  selectedId: number | null;
  selectedCategory: string | null;
  onSelect: (category: string, id: number) => void;
  loading: boolean;
}

export function ItemList({ items, selectedId, selectedCategory, onSelect, loading }: ItemListProps) {
  if (loading) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
        Loading items...
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 px-4 text-center">
        <Brain className="h-10 w-10 text-muted-foreground/30" />
        <p className="text-sm text-muted-foreground">
          No items found. As you chat with Seny, people, projects, ideas, and tasks will appear here automatically.
        </p>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto">
      {items.map((item) => {
        const isActive = item.id === selectedId && item.category === selectedCategory;
        const displayName = item.name || item.title || "Untitled";
        return (
          <button
            key={`${item.category}-${item.id}`}
            onClick={() => onSelect(item.category, item.id)}
            className={cn(
              "w-full border-b border-border px-4 py-3 text-left transition-colors",
              isActive
                ? "bg-accent/60"
                : "hover:bg-accent/30"
            )}
          >
            <div className="mb-1 flex items-center gap-2">
              <Badge
                variant="outline"
                className={cn("text-[10px] px-1.5 py-0", CATEGORY_COLORS[item.category])}
              >
                {item.category}
              </Badge>
              {item.confidence != null && (
                <span className={cn(
                  "text-[10px]",
                  item.confidence >= 0.8 ? "text-green-400" :
                  item.confidence >= 0.6 ? "text-yellow-400" : "text-red-400"
                )}>
                  {Math.round(item.confidence * 100)}%
                </span>
              )}
            </div>
            <div className="truncate text-sm font-medium text-foreground">
              {displayName}
            </div>
            {item.subtitle && (
              <div className="mt-0.5 truncate text-xs text-muted-foreground">
                {item.subtitle}
              </div>
            )}
          </button>
        );
      })}
    </div>
  );
}
