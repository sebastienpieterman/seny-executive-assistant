import { cn } from "@/lib/utils";

export type Category = "" | "people" | "projects" | "ideas" | "admin" | "activity" | "captures" | "search";

interface CategoryTabsProps {
  active: Category;
  counts: Record<string, number>;
  onSelect: (cat: Category) => void;
}

const TABS: { value: Category; label: string }[] = [
  { value: "", label: "All" },
  { value: "people", label: "People" },
  { value: "projects", label: "Projects" },
  { value: "ideas", label: "Ideas" },
  { value: "admin", label: "Admin" },
  { value: "activity", label: "Activity" },
  { value: "captures", label: "Captures" },
  { value: "search", label: "Search" },
];

export function CategoryTabs({ active, counts, onSelect }: CategoryTabsProps) {
  const total = (counts.people || 0) + (counts.projects || 0) + (counts.ideas || 0) + (counts.admin || 0);

  return (
    <div className="flex flex-wrap gap-1 border-b border-border px-3 py-2">
      {TABS.map((tab) => {
        const count = tab.value === "" ? total : (counts[tab.value] || 0);
        // Activity tab doesn't show count (it's a different concept)
        const showCount = tab.value !== "activity" && tab.value !== "captures" && tab.value !== "search" && count > 0;
        return (
          <button
            key={tab.value}
            onClick={() => onSelect(tab.value)}
            className={cn(
              "rounded-md px-2 py-1.5 text-sm font-medium transition-colors whitespace-nowrap",
              active === tab.value
                ? "bg-accent text-accent-foreground"
                : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
            )}
          >
            {tab.label}
            {showCount && (
              <span className="ml-1 text-xs opacity-70">{count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
