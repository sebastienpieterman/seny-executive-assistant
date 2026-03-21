import { useState, useEffect, useCallback, useRef } from "react";
import { Search, X, Brain, GripVertical, Plus, Copy } from "lucide-react";
import { Input } from "@/components/ui/input";
import { CategoryTabs, type Category } from "@/components/second-brain/CategoryTabs";
import { ItemList, type SecondBrainItem } from "@/components/second-brain/ItemList";
import { ItemDetail } from "@/components/second-brain/ItemDetail";
import { AddItemDialog, type Category as AddCategory } from "@/components/second-brain/AddItemDialog";
import { ActivityFeed } from "@/components/second-brain/ActivityFeed";
import { CaptureHistory } from "@/components/second-brain/CaptureHistory";
import { SemanticSearch } from "@/components/second-brain/SemanticSearch";
import { DuplicatesView } from "@/components/second-brain/DuplicatesView";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { toast } from "sonner";

const API_BASE = "/api/second-brain";

interface ItemsResponse {
  items: SecondBrainItem[];
  total: number;
}

interface StatsResponse {
  [key: string]: number;
}

export function SecondBrainPage() {
  const [category, setCategory] = useState<Category>("");
  const [search, setSearch] = useState("");
  const [items, setItems] = useState<SecondBrainItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [detailItem, setDetailItem] = useState<SecondBrainItem | null>(null);
  const [sidebarWidth, setSidebarWidth] = useState(400);
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [showDuplicates, setShowDuplicates] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const searchTimeout = useRef<ReturnType<typeof setTimeout>>(undefined);

  const loadItems = useCallback(async (cat: Category, q: string) => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (cat) params.set("category", cat);
      if (q) params.set("search", q);
      params.set("limit", "200");
      const data = await api.get<ItemsResponse>(`${API_BASE}/items?${params}`);
      setItems(data.items || []);
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadStats = useCallback(async () => {
    try {
      const stats = await api.get<StatsResponse>(`${API_BASE}/stats`);
      setCounts(stats);
    } catch {
      // ignore
    }
  }, []);

  // Initial load
  useEffect(() => {
    loadItems(category, search);
    loadStats();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Reload when category changes
  useEffect(() => {
    loadItems(category, search);
  }, [category]); // eslint-disable-line react-hooks/exhaustive-deps

  // Debounced search
  const handleSearchChange = (value: string) => {
    setSearch(value);
    clearTimeout(searchTimeout.current);
    searchTimeout.current = setTimeout(() => {
      loadItems(category, value);
    }, 300);
  };

  const clearSearch = () => {
    setSearch("");
    loadItems(category, "");
  };

  // Select item
  const handleSelect = useCallback(async (cat: string, id: number) => {
    setSelectedId(id);
    setSelectedCategory(cat);
    try {
      const item = await api.get<SecondBrainItem>(`${API_BASE}/items/${cat}/${id}`);
      setDetailItem(item);
    } catch {
      toast.error("Failed to load item details");
    }
  }, []);

  // Refresh current item (e.g., after undo)
  const handleRefresh = useCallback(async () => {
    if (!selectedCategory || !selectedId) return;
    try {
      const item = await api.get<SecondBrainItem>(`${API_BASE}/items/${selectedCategory}/${selectedId}`);
      setDetailItem(item);
      loadItems(category, search);
    } catch {
      // ignore
    }
  }, [selectedCategory, selectedId, category, search, loadItems]);

  // Save edit
  const handleSave = useCallback(async (data: Record<string, string>) => {
    if (!selectedCategory || !selectedId) return;
    try {
      await api.put(`${API_BASE}/items/${selectedCategory}/${selectedId}`, data);
      toast.success("Item updated");
      // Refresh detail and list
      const item = await api.get<SecondBrainItem>(`${API_BASE}/items/${selectedCategory}/${selectedId}`);
      setDetailItem(item);
      loadItems(category, search);
    } catch {
      toast.error("Failed to save changes");
    }
  }, [selectedCategory, selectedId, category, search, loadItems]);

  // Delete
  const handleDelete = useCallback(async () => {
    if (!selectedCategory || !selectedId) return;
    try {
      await api.delete(`${API_BASE}/items/${selectedCategory}/${selectedId}`);
      toast.success("Item deleted");
      setSelectedId(null);
      setSelectedCategory(null);
      setDetailItem(null);
      loadItems(category, search);
      loadStats();
    } catch {
      toast.error("Failed to delete item");
    }
  }, [selectedCategory, selectedId, category, search, loadItems, loadStats]);

  // Reclassify
  const handleReclassify = useCallback(async (targetCategory: string) => {
    if (!selectedCategory || !selectedId) return;
    try {
      await api.post(`${API_BASE}/items/${selectedCategory}/${selectedId}/reclassify`, {
        target_category: targetCategory,
      });
      toast.success(`Item moved to ${targetCategory}`);
      setSelectedId(null);
      setSelectedCategory(null);
      setDetailItem(null);
      loadItems(category, search);
      loadStats();
    } catch {
      toast.error("Failed to reclassify item");
    }
  }, [selectedCategory, selectedId, category, search, loadItems, loadStats]);

  // Add new item
  const handleAdd = useCallback(async (addCategory: AddCategory, data: Record<string, string>) => {
    try {
      await api.post(`${API_BASE}/items/${addCategory}`, data);
      toast.success(`Added new ${addCategory.slice(0, -1)}`);
      loadItems(category, search);
      loadStats();
    } catch {
      toast.error("Failed to add item");
      throw new Error("Failed to add item");
    }
  }, [category, search, loadItems, loadStats]);

  // Resize drag
  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!dragging.current || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      setSidebarWidth(Math.min(Math.max(e.clientX - rect.left, 400), 500));
    };
    const handleMouseUp = () => {
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.addEventListener("mousemove", handleMouseMove);
    document.addEventListener("mouseup", handleMouseUp);
    return () => {
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
    };
  }, []);

  const startDrag = () => {
    dragging.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  };

  return (
    <div ref={containerRef} className="absolute inset-0 flex">
      {/* Left panel */}
      <div
        className="flex h-full flex-col border-r border-border"
        style={{ width: sidebarWidth, minWidth: 400, maxWidth: 500, flexShrink: 0 }}
      >
        {showDuplicates ? (
          <DuplicatesView
            onBack={() => {
              setShowDuplicates(false);
              loadItems(category, search);
              loadStats();
            }}
          />
        ) : (
          <>
            <CategoryTabs active={category} counts={counts} onSelect={setCategory} />

            {/* Search + Add (hidden when semantic search tab is active) */}
            {category !== "search" && (
              <div className="border-b border-border px-4 py-2">
                <div className="flex gap-2">
                  <div className="relative flex-1">
                    <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                    <Input
                      value={search}
                      onChange={(e) => handleSearchChange(e.target.value)}
                      placeholder="Search items..."
                      className="pl-9 pr-8"
                    />
                    {search && (
                      <button
                        onClick={clearSearch}
                        className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        <X className="h-4 w-4" />
                      </button>
                    )}
                  </div>
                  <Button
                    size="icon"
                    variant="outline"
                    onClick={() => setShowDuplicates(true)}
                    title="Find duplicates"
                  >
                    <Copy className="h-4 w-4" />
                  </Button>
                  <Button
                    size="icon"
                    variant="outline"
                    onClick={() => setAddDialogOpen(true)}
                    title="Add new entry"
                  >
                    <Plus className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            )}

            {category === "activity" ? (
              <ActivityFeed
                onPersonClick={(personId) => {
                  setCategory("people");
                  handleSelect("people", personId);
                }}
              />
            ) : category === "captures" ? (
              <CaptureHistory
                onItemClick={(cat, id) => {
                  setCategory(cat as Parameters<typeof setCategory>[0]);
                  handleSelect(cat, id);
                }}
              />
            ) : category === "search" ? (
              <SemanticSearch
                onSelect={(cat, id) => {
                  setCategory(cat as Parameters<typeof setCategory>[0]);
                  handleSelect(cat, id);
                }}
              />
            ) : (
              <ItemList
                items={items}
                selectedId={selectedId}
                selectedCategory={selectedCategory}
                onSelect={handleSelect}
                loading={loading}
              />
            )}
          </>
        )}
      </div>

      {/* Resize handle */}
      <div
        className="flex w-1.5 cursor-col-resize items-center justify-center hover:bg-accent/50 active:bg-accent"
        onMouseDown={startDrag}
      >
        <GripVertical className="h-4 w-4 text-muted-foreground/50" />
      </div>

      {/* Right panel */}
      <div className="flex-1 overflow-hidden">
        {detailItem ? (
          <ItemDetail
            item={detailItem}
            onSave={handleSave}
            onDelete={handleDelete}
            onReclassify={handleReclassify}
            onRefresh={handleRefresh}
          />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
            <Brain className="h-12 w-12 text-muted-foreground/30" />
            <div>
              <h2 className="text-lg font-semibold text-foreground">Second Brain</h2>
              <p className="mt-1 max-w-sm text-sm text-muted-foreground">
                Your Second Brain captures people, projects, ideas, and tasks automatically as you chat with Seny. Select an item to view details.
              </p>
            </div>
          </div>
        )}
      </div>

      {/* Add Item Dialog */}
      <AddItemDialog
        open={addDialogOpen}
        initialCategory={category ? category as AddCategory : undefined}
        onClose={() => setAddDialogOpen(false)}
        onSubmit={handleAdd}
      />
    </div>
  );
}
