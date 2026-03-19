import {
  useState,
  useEffect,
  useRef,
  useCallback,
} from "react";
import { Save, Trash2, X, Check } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { api } from "@/lib/api";
import { toast } from "sonner";

interface Note {
  id: number;
  title: string;
  content: string;
  tags: string[];
  updated_at: string;
}

interface NoteListItem {
  id: number;
  title: string;
}

interface NoteEditorProps {
  noteId: number | null; // null = new note
  onSaved: (noteId: number) => void;
  onCancel: () => void;
  onDeleted: () => void;
}

export function NoteEditor({
  noteId,
  onSaved,
  onCancel,
  onDeleted,
}: NoteEditorProps) {
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [autoSaveStatus, setAutoSaveStatus] = useState<"idle" | "saving" | "saved">("idle");
  const autoSaveFadeTimer = useRef<ReturnType<typeof setTimeout>>(undefined);

  // Autocomplete state
  const [acVisible, setAcVisible] = useState(false);
  const [acMatches, setAcMatches] = useState<NoteListItem[]>([]);
  const [acIndex, setAcIndex] = useState(0);
  const [acStart, setAcStart] = useState(-1);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const acRef = useRef<HTMLDivElement>(null);
  const autoSaveTimer = useRef<ReturnType<typeof setTimeout>>(undefined);

  // Load note data when editing
  useEffect(() => {
    if (noteId) {
      setLoading(true);
      api
        .get<Note>(`/api/notes/${noteId}`)
        .then((note) => {
          setTitle(note.title);
          setContent(note.content);
        })
        .catch(() => toast.error("Failed to load note"))
        .finally(() => setLoading(false));
    } else {
      setTitle("");
      setContent("");
    }
  }, [noteId]);

  // Auto-save with debounce
  const triggerAutoSave = useCallback(() => {
    if (!noteId) return;
    if (autoSaveTimer.current) clearTimeout(autoSaveTimer.current);
    autoSaveTimer.current = setTimeout(async () => {
      try {
        setAutoSaveStatus("saving");
        await api.put(`/api/notes/${noteId}`, {
          title: title.trim() || "Untitled",
          content,
        });
        setAutoSaveStatus("saved");
        if (autoSaveFadeTimer.current) clearTimeout(autoSaveFadeTimer.current);
        autoSaveFadeTimer.current = setTimeout(() => setAutoSaveStatus("idle"), 2000);
      } catch {
        setAutoSaveStatus("idle");
      }
    }, 2000);
  }, [noteId, title, content]);

  useEffect(() => {
    if (noteId && title && !loading) {
      triggerAutoSave();
    }
    return () => {
      if (autoSaveTimer.current) clearTimeout(autoSaveTimer.current);
    };
  }, [title, content, noteId, loading, triggerAutoSave]);

  // Extract tags and links from content
  const detectedTags = content.match(/#[a-zA-Z][a-zA-Z0-9_-]*/g) || [];
  const detectedLinks =
    content.match(/\[\[([^\]]+)\]\]/g)?.map((m) => m.slice(2, -2)) || [];

  // Wiki-link autocomplete logic
  const checkAutocomplete = useCallback(async () => {
    const textarea = textareaRef.current;
    if (!textarea) return;

    const cursorPos = textarea.selectionStart;
    const beforeCursor = textarea.value.substring(0, cursorPos);
    const lastOpen = beforeCursor.lastIndexOf("[[");

    if (lastOpen === -1) {
      setAcVisible(false);
      return;
    }

    const afterOpen = beforeCursor.substring(lastOpen + 2);
    if (afterOpen.includes("]]")) {
      setAcVisible(false);
      return;
    }

    const query = afterOpen.toLowerCase().trim();
    try {
      const data = await api.get<{ notes: NoteListItem[] }>("/api/notes");
      const matches = (data.notes || [])
        .filter(
          (n) =>
            n.id !== noteId &&
            (!query || n.title.toLowerCase().includes(query))
        )
        .slice(0, 8);

      setAcMatches(matches);
      setAcStart(lastOpen);
      setAcIndex(0);
      setAcVisible(matches.length > 0);
    } catch {
      setAcVisible(false);
    }
  }, [noteId]);

  const insertAutocomplete = useCallback(
    (selectedTitle: string) => {
      const textarea = textareaRef.current;
      if (!textarea || acStart < 0) return;

      const before = textarea.value.substring(0, acStart);
      const after = textarea.value.substring(textarea.selectionStart);
      const newText = `${before}[[${selectedTitle}]]${after}`;
      setContent(newText);

      setAcVisible(false);

      // Restore cursor position
      requestAnimationFrame(() => {
        const newPos = acStart + selectedTitle.length + 4;
        textarea.selectionStart = newPos;
        textarea.selectionEnd = newPos;
        textarea.focus();
      });
    },
    [acStart]
  );

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Handle autocomplete navigation
    if (acVisible) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setAcIndex((i) => Math.min(i + 1, acMatches.length - 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setAcIndex((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        if (acMatches.length > 0) {
          e.preventDefault();
          insertAutocomplete(acMatches[acIndex].title);
          return;
        }
      }
      if (e.key === "Escape") {
        setAcVisible(false);
        return;
      }
    }

    // Tab indentation
    if (e.key === "Tab" && !acVisible) {
      e.preventDefault();
      const textarea = textareaRef.current;
      if (!textarea) return;
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      setContent(
        content.substring(0, start) + "  " + content.substring(end)
      );
      requestAnimationFrame(() => {
        textarea.selectionStart = start + 2;
        textarea.selectionEnd = start + 2;
      });
    }

    // Ctrl/Cmd+S to save
    if ((e.ctrlKey || e.metaKey) && e.key === "s") {
      e.preventDefault();
      handleSave();
    }
  };

  const handleSave = async () => {
    if (!title.trim()) {
      toast.error("Please enter a title");
      return;
    }

    setSaving(true);
    try {
      if (noteId) {
        await api.put(`/api/notes/${noteId}`, {
          title: title.trim(),
          content,
        });
        toast.success("Note saved");
        onSaved(noteId);
      } else {
        const result = await api.post<{ id: number }>("/api/notes", {
          title: title.trim(),
          content,
        });
        toast.success("Note created");
        onSaved(result.id);
      }
    } catch {
      toast.error("Failed to save note");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!noteId) return;
    try {
      await api.delete(`/api/notes/${noteId}`);
      toast.success("Note deleted");
      onDeleted();
    } catch {
      toast.error("Failed to delete note");
    }
  };

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-sm text-muted-foreground">Loading note...</div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <Input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Note title..."
          className="h-8 flex-1 border-none bg-transparent text-base font-semibold shadow-none focus-visible:ring-0"
          autoFocus
        />
        <div className="flex items-center gap-1.5">
          {/* Auto-save indicator */}
          <span
            className={`text-[10px] text-muted-foreground transition-opacity duration-300 ${
              autoSaveStatus === "idle" ? "opacity-0" : "opacity-100"
            }`}
          >
            {autoSaveStatus === "saving" ? (
              "Saving..."
            ) : autoSaveStatus === "saved" ? (
              <span className="flex items-center gap-0.5">
                <Check className="h-3 w-3" />
                Saved
              </span>
            ) : null}
          </span>
          <Button
            size="sm"
            onClick={handleSave}
            disabled={saving}
            className="h-7 gap-1 px-2.5 text-xs"
          >
            <Save className="h-3.5 w-3.5" />
            {saving ? "Saving..." : "Save"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={onCancel}
            className="h-7 px-2 text-xs"
          >
            <X className="h-3.5 w-3.5" />
          </Button>
          {noteId && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() =>
                showDeleteConfirm ? handleDelete() : setShowDeleteConfirm(true)
              }
              className={`h-7 px-2 text-xs ${
                showDeleteConfirm
                  ? "text-destructive hover:text-destructive"
                  : ""
              }`}
            >
              <Trash2 className="h-3.5 w-3.5" />
              {showDeleteConfirm && (
                <span className="ml-1">Confirm?</span>
              )}
            </Button>
          )}
        </div>
      </div>

      {/* Hints */}
      <div className="flex items-center gap-3 border-b border-border px-4 py-1.5 text-[10px] text-muted-foreground">
        <span>Use #tags to categorize</span>
        <span className="text-border">|</span>
        <span>Use [[Note Title]] to link notes</span>
      </div>

      {/* Content editor */}
      <div className="relative flex-1 p-4">
        <textarea
          ref={textareaRef}
          value={content}
          onChange={(e) => {
            setContent(e.target.value);
            checkAutocomplete();
          }}
          onKeyDown={handleKeyDown}
          placeholder="Start writing...&#10;&#10;Use #tags to organize your notes.&#10;Use [[Note Title]] to link to other notes."
          className="h-full w-full resize-none bg-transparent text-sm leading-relaxed text-foreground placeholder:text-muted-foreground/50 outline-none"
        />

        {/* Wiki-link autocomplete dropdown */}
        {acVisible && (
          <div
            ref={acRef}
            className="absolute left-4 top-16 z-50 w-64 rounded-lg border border-border bg-popover shadow-lg"
          >
            {acMatches.map((note, i) => (
              <button
                key={note.id}
                className={`w-full px-3 py-2 text-left text-sm transition-colors ${
                  i === acIndex
                    ? "bg-accent text-accent-foreground"
                    : "text-popover-foreground hover:bg-accent/50"
                }`}
                onMouseDown={(e) => {
                  e.preventDefault();
                  insertAutocomplete(note.title);
                }}
              >
                {note.title}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Footer: detected tags and links */}
      <div className="flex items-center gap-4 border-t border-border px-4 py-2 text-xs text-muted-foreground">
        <div className="flex items-center gap-1.5">
          <span>Tags:</span>
          {detectedTags.length > 0 ? (
            detectedTags.map((t) => (
              <Badge
                key={t}
                variant="secondary"
                className="h-4 px-1.5 text-[10px]"
              >
                {t}
              </Badge>
            ))
          ) : (
            <span className="text-muted-foreground/50">None</span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <span>Links:</span>
          {detectedLinks.length > 0 ? (
            <span>{detectedLinks.join(", ")}</span>
          ) : (
            <span className="text-muted-foreground/50">None</span>
          )}
        </div>
      </div>
    </div>
  );
}
