import { useState, useEffect, useCallback, useRef } from "react";
import { Search, Plus, X, FileText, Tag, Check } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { api } from "@/lib/api";
import { NotesListSkeleton } from "@/components/ui/LoadingSkeletons";

interface NoteListItem {
  id: number;
  title: string;
  content_preview: string;
  tags: string[];
  updated_at: string;
}

interface TagInfo {
  tag: string;
  count: number;
}

interface NotesListProps {
  selectedNoteId: number | null;
  onSelectNote: (id: number) => void;
  onNewNote: () => void;
  refreshKey: number;
}

function formatRelativeDate(dateStr: string): string {
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
  });
}

export function NotesList({
  selectedNoteId,
  onSelectNote,
  onNewNote,
  refreshKey,
}: NotesListProps) {
  const [notes, setNotes] = useState<NoteListItem[]>([]);
  const [tags, setTags] = useState<TagInfo[]>([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [filterMode, setFilterMode] = useState<"and" | "or">("and");
  const [tagDropdownOpen, setTagDropdownOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const tagListRef = useRef<HTMLDivElement>(null);

  // Reset scroll when dropdown opens
  useEffect(() => {
    if (tagDropdownOpen && tagListRef.current) {
      tagListRef.current.scrollTop = 0;
    }
  }, [tagDropdownOpen]);

  const toggleTag = (tag: string) => {
    setSelectedTags((prev) =>
      prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag]
    );
    setSearchQuery("");
  };

  const fetchNotes = useCallback(async () => {
    try {
      setLoading(true);
      let url = "/api/notes";
      if (searchQuery) {
        url = `/api/notes/search?q=${encodeURIComponent(searchQuery)}`;
      } else if (selectedTags.length === 1) {
        url = `/api/notes/by-tag/${encodeURIComponent(selectedTags[0])}`;
      }
      const data = await api.get<{ notes: NoteListItem[] }>(url);
      let results = data.notes || [];
      // Client-side filtering for multiple tags (AND or OR mode)
      if (!searchQuery && selectedTags.length > 1) {
        results = results.filter((note) =>
          filterMode === "and"
            ? selectedTags.every((tag) => note.tags?.includes(tag))
            : selectedTags.some((tag) => note.tags?.includes(tag))
        );
      }
      setNotes(results);
    } catch {
      setNotes([]);
    } finally {
      setLoading(false);
    }
  }, [searchQuery, selectedTags, filterMode]);

  const fetchTags = useCallback(async () => {
    try {
      const data = await api.get<{ tags: TagInfo[] }>("/api/notes/tags");
      setTags(data.tags || []);
    } catch {
      setTags([]);
    }
  }, []);

  useEffect(() => {
    fetchTags();
  }, [fetchTags, refreshKey]);

  useEffect(() => {
    const timeout = setTimeout(() => {
      fetchNotes();
    }, searchQuery ? 300 : 0);
    return () => clearTimeout(timeout);
  }, [fetchNotes, refreshKey]);

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold text-foreground">Notes</h2>
        <Button
          variant="ghost"
          size="sm"
          onClick={onNewNote}
          className="h-7 gap-1 px-2 text-xs"
        >
          <Plus className="h-3.5 w-3.5" />
          New
        </Button>
      </div>

      {/* Search */}
      <div className="space-y-2 border-b border-border px-3 py-2">
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search notes..."
            value={searchQuery}
            onChange={(e) => {
              setSearchQuery(e.target.value);
              if (e.target.value) setSelectedTags([]);
            }}
            className="h-8 pl-8 pr-8 text-xs"
          />
          {searchQuery && (
            <button
              onClick={() => setSearchQuery("")}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
        {/* Multi-tag filter */}
        <div className="flex flex-wrap items-center gap-1">
          <Popover open={tagDropdownOpen} onOpenChange={setTagDropdownOpen}>
            <PopoverTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                className="h-7 gap-1 px-2 text-xs"
              >
                <Tag className="h-3 w-3" />
                {selectedTags.length === 0
                  ? "Filter tags"
                  : `${selectedTags.length} tag${selectedTags.length > 1 ? "s" : ""}`}
              </Button>
            </PopoverTrigger>
            <PopoverContent className="w-48 p-0" align="start">
              <div
                ref={tagListRef}
                className="max-h-48 overflow-y-auto p-1"
              >
                {tags.map((t) => (
                  <button
                    key={t.tag}
                    onClick={() => toggleTag(t.tag)}
                    className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-xs hover:bg-accent"
                  >
                    <span className={`flex h-3.5 w-3.5 items-center justify-center rounded-sm border ${
                      selectedTags.includes(t.tag)
                        ? "border-primary bg-primary text-primary-foreground"
                        : "border-muted-foreground/30"
                    }`}>
                      {selectedTags.includes(t.tag) && (
                        <Check className="h-2.5 w-2.5" />
                      )}
                    </span>
                    <span className="flex-1 text-left">{t.tag}</span>
                    <span className="text-muted-foreground">({t.count})</span>
                  </button>
                ))}
              </div>
            </PopoverContent>
          </Popover>
          {selectedTags.map((tag) => (
            <Badge
              key={tag}
              variant="secondary"
              className="h-5 gap-0.5 px-1.5 text-[10px]"
            >
              {tag}
              <button
                onClick={() => toggleTag(tag)}
                className="ml-0.5 hover:text-foreground"
              >
                <X className="h-2.5 w-2.5" />
              </button>
            </Badge>
          ))}
          {selectedTags.length > 1 && (
            <button
              onClick={() => setFilterMode((m) => (m === "and" ? "or" : "and"))}
              className="rounded border border-border px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground hover:text-foreground hover:border-foreground/30 transition-colors"
            >
              {filterMode === "and" ? "All" : "Any"}
            </button>
          )}
          {selectedTags.length > 0 && (
            <button
              onClick={() => setSelectedTags([])}
              className="text-[10px] text-muted-foreground hover:text-foreground"
            >
              Clear
            </button>
          )}
        </div>
      </div>

      {/* Notes list */}
      <ScrollArea className="flex-1">
        {loading ? (
          <NotesListSkeleton />
        ) : notes.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 p-8 text-center">
            <FileText className="h-8 w-8 text-muted-foreground/50" />
            <p className="text-sm text-muted-foreground">No notes found</p>
          </div>
        ) : (
          <div className="space-y-0.5 p-1.5">
            {notes.map((note) => (
              <button
                key={note.id}
                onClick={() => onSelectNote(note.id)}
                className={`w-full rounded-lg px-3 py-2.5 text-left transition-colors ${
                  note.id === selectedNoteId
                    ? "bg-accent text-accent-foreground"
                    : "hover:bg-accent/50"
                }`}
              >
                <div className="text-sm font-medium leading-tight truncate">
                  {note.title}
                </div>
                <div className="mt-0.5 text-xs text-muted-foreground truncate">
                  {note.content_preview}
                </div>
                <div className="mt-1 flex items-center gap-1.5">
                  <span className="text-[10px] text-muted-foreground/70">
                    {formatRelativeDate(note.updated_at)}
                  </span>
                  {note.tags?.slice(0, 2).map((tag) => (
                    <Badge
                      key={tag}
                      variant="secondary"
                      className="h-4 px-1.5 text-[10px]"
                    >
                      {tag}
                    </Badge>
                  ))}
                </div>
              </button>
            ))}
          </div>
        )}
      </ScrollArea>
    </div>
  );
}
