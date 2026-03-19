import { useState, useEffect, useCallback } from "react";
import { Pencil, Download, Trash2, FileText } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { MarkdownRenderer } from "@/components/chat/MarkdownRenderer";
import { api } from "@/lib/api";
import { toast } from "sonner";

interface LinkedNote {
  id: number;
  title: string;
}

interface Note {
  id: number;
  title: string;
  content: string;
  tags: string[];
  updated_at: string;
  linked_notes?: LinkedNote[];
  backlinks?: LinkedNote[];
}

interface NoteViewProps {
  noteId: number;
  onEdit: () => void;
  onDeleted: () => void;
  onNavigateNote: (id: number) => void;
  refreshKey: number;
}

function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

/**
 * Render note content, converting [[wiki-links]] to clickable spans.
 */
function renderContentWithLinks(
  content: string,
  linkedNotes: LinkedNote[],
  _onNavigateNote: (id: number) => void
): string {
  // Replace [[Title]] with clickable links in the markdown source.
  // We'll embed special markers that the MarkdownRenderer can render,
  // but since MarkdownRenderer just renders markdown, we'll use
  // anchor tags with data attributes.
  return content.replace(/\[\[([^\]]+)\]\]/g, (_match, title: string) => {
    const linked = linkedNotes?.find(
      (ln) => ln.title.toLowerCase() === title.toLowerCase()
    );
    if (linked) {
      return `[${title}](#wiki-link-${linked.id})`;
    }
    return `*${title}*`;
  });
}

export function NoteView({
  noteId,
  onEdit,
  onDeleted,
  onNavigateNote,
  refreshKey,
}: NoteViewProps) {
  const [note, setNote] = useState<Note | null>(null);
  const [loading, setLoading] = useState(true);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);

  const fetchNote = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.get<Note>(`/api/notes/${noteId}`);
      setNote(data);
    } catch {
      toast.error("Failed to load note");
    } finally {
      setLoading(false);
    }
  }, [noteId]);

  useEffect(() => {
    fetchNote();
    setShowDeleteConfirm(false);
  }, [fetchNote, refreshKey]);

  // Handle wiki-link clicks
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (target.tagName === "A") {
        const href = target.getAttribute("href");
        if (href?.startsWith("#wiki-link-")) {
          e.preventDefault();
          const id = parseInt(href.replace("#wiki-link-", ""), 10);
          if (!isNaN(id)) onNavigateNote(id);
        }
      }
    };
    document.addEventListener("click", handleClick);
    return () => document.removeEventListener("click", handleClick);
  }, [onNavigateNote]);

  const handleExport = async () => {
    try {
      const token = localStorage.getItem("seny_access_token");
      const response = await fetch(`/api/notes/${noteId}/export`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error("Export failed");

      const disposition = response.headers.get("Content-Disposition");
      let filename = "note.md";
      if (disposition) {
        const match = disposition.match(/filename="?([^";\n]+)"?/);
        if (match) filename = match[1];
      }

      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      a.remove();
      toast.success("Note exported");
    } catch {
      toast.error("Failed to export note");
    }
  };

  const handleExportAll = async () => {
    try {
      const token = localStorage.getItem("seny_access_token");
      const response = await fetch("/api/notes/export-all", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error("Export failed");

      const disposition = response.headers.get("Content-Disposition");
      let filename = "notes-export.zip";
      if (disposition) {
        const match = disposition.match(/filename="?([^";\n]+)"?/);
        if (match) filename = match[1];
      }

      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      a.remove();
      toast.success("All notes exported");
    } catch {
      toast.error("Failed to export notes");
    }
  };

  const handleDelete = async () => {
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

  if (!note) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 text-muted-foreground">
        <FileText className="h-10 w-10 opacity-30" />
        <p className="text-sm">Note not found</p>
      </div>
    );
  }

  const processedContent = renderContentWithLinks(
    note.content,
    note.linked_notes || [],
    onNavigateNote
  );

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      {/* Header */}
      <div className="flex items-start justify-between border-b border-border px-6 py-4">
        <div className="min-w-0 flex-1">
          <h1 className="text-xl font-bold text-foreground">{note.title}</h1>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Updated {formatDate(note.updated_at)}
          </p>
          {note.tags && note.tags.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {note.tags.map((tag) => (
                <Badge
                  key={tag}
                  variant="secondary"
                  className="text-xs"
                >
                  {tag}
                </Badge>
              ))}
            </div>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <Button
            variant="ghost"
            size="sm"
            onClick={onEdit}
            className="h-7 gap-1 px-2 text-xs"
          >
            <Pencil className="h-3.5 w-3.5" />
            Edit
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={handleExport}
            className="h-7 gap-1 px-2 text-xs"
          >
            <Download className="h-3.5 w-3.5" />
            Export
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={handleExportAll}
            className="h-7 gap-1 px-2 text-xs"
          >
            <Download className="h-3.5 w-3.5" />
            Export All
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() =>
              showDeleteConfirm ? handleDelete() : setShowDeleteConfirm(true)
            }
            className={`h-7 gap-1 px-2 text-xs ${
              showDeleteConfirm
                ? "text-destructive hover:text-destructive"
                : ""
            }`}
          >
            <Trash2 className="h-3.5 w-3.5" />
            {showDeleteConfirm ? "Confirm?" : "Delete"}
          </Button>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 px-6 py-4">
        <MarkdownRenderer content={processedContent} />
      </div>

      {/* Linked notes & backlinks */}
      {((note.linked_notes && note.linked_notes.length > 0) ||
        (note.backlinks && note.backlinks.length > 0)) && (
        <div className="border-t border-border px-6 py-4 space-y-3">
          {note.linked_notes && note.linked_notes.length > 0 && (
            <div>
              <h3 className="text-xs font-semibold text-muted-foreground mb-1.5">
                Linked Notes
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {note.linked_notes.map((ln) => (
                  <button
                    key={ln.id}
                    onClick={() => onNavigateNote(ln.id)}
                    className="rounded-md bg-accent/50 px-2.5 py-1 text-xs text-accent-foreground hover:bg-accent transition-colors"
                  >
                    {ln.title}
                  </button>
                ))}
              </div>
            </div>
          )}
          {note.backlinks && note.backlinks.length > 0 && (
            <div>
              <h3 className="text-xs font-semibold text-muted-foreground mb-1.5">
                Backlinks
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {note.backlinks.map((bl) => (
                  <button
                    key={bl.id}
                    onClick={() => onNavigateNote(bl.id)}
                    className="rounded-md bg-accent/50 px-2.5 py-1 text-xs text-accent-foreground hover:bg-accent transition-colors"
                  >
                    {bl.title}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
