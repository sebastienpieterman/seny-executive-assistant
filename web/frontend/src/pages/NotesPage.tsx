import { useState, useCallback, useRef, useEffect } from "react";
import { FileText, Plus, Network, List, GripVertical } from "lucide-react";
import { Button } from "@/components/ui/button";
import { NotesList } from "@/components/notes/NotesList";
import { NoteEditor } from "@/components/notes/NoteEditor";
import { NoteView } from "@/components/notes/NoteView";
import { NoteGraph } from "@/components/notes/NoteGraph";

type ViewMode = "view" | "edit" | "graph";

export function NotesPage() {
  const [selectedNoteId, setSelectedNoteId] = useState<number | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>("view");
  const [editingNoteId, setEditingNoteId] = useState<number | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [sidebarWidth, setSidebarWidth] = useState(280);
  const containerRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);

  const refresh = useCallback(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  const handleSelectNote = useCallback((id: number) => {
    setSelectedNoteId(id);
    setViewMode("view");
  }, []);

  const handleNewNote = useCallback(() => {
    setEditingNoteId(null);
    setViewMode("edit");
  }, []);

  const handleEdit = useCallback(() => {
    setEditingNoteId(selectedNoteId);
    setViewMode("edit");
  }, [selectedNoteId]);

  const handleSaved = useCallback(
    (noteId: number) => {
      setSelectedNoteId(noteId);
      setViewMode("view");
      refresh();
    },
    [refresh]
  );

  const handleCancel = useCallback(() => {
    setViewMode("view");
  }, []);

  const handleDeleted = useCallback(() => {
    setSelectedNoteId(null);
    setViewMode("view");
    refresh();
  }, [refresh]);

  const toggleGraph = useCallback(() => {
    setViewMode((m) => (m === "graph" ? "view" : "graph"));
  }, []);

  // Resize drag handling
  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!dragging.current || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const newWidth = Math.min(Math.max(e.clientX - rect.left, 200), 500);
      setSidebarWidth(newWidth);
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

  const startDrag = useCallback(() => {
    dragging.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  return (
    <div ref={containerRef} className="absolute inset-0 flex">
      {/* Left panel: Notes list */}
      <div
        className="flex h-full flex-col border-r border-border"
        style={{ width: sidebarWidth, minWidth: 200, maxWidth: 500, flexShrink: 0 }}
      >
        <div className="flex-1 overflow-hidden">
          <NotesList
            selectedNoteId={selectedNoteId}
            onSelectNote={handleSelectNote}
            onNewNote={handleNewNote}
            refreshKey={refreshKey}
          />
        </div>
        {/* Graph toggle button at bottom of sidebar */}
        <div className="border-t border-border px-3 py-2">
          <Button
            variant={viewMode === "graph" ? "secondary" : "ghost"}
            size="sm"
            onClick={toggleGraph}
            className="w-full justify-start gap-2 text-xs"
          >
            {viewMode === "graph" ? (
              <>
                <List className="h-3.5 w-3.5" />
                List View
              </>
            ) : (
              <>
                <Network className="h-3.5 w-3.5" />
                Graph View
              </>
            )}
          </Button>
        </div>
      </div>

      {/* Resize handle */}
      <div
        className="flex w-1.5 cursor-col-resize items-center justify-center hover:bg-accent/50 active:bg-accent"
        onMouseDown={startDrag}
      >
        <GripVertical className="h-4 w-4 text-muted-foreground/50" />
      </div>

      {/* Main content area */}
      <div className="flex-1 overflow-hidden">
        {viewMode === "graph" ? (
          <div className="relative h-full">
            <NoteGraph onSelectNote={handleSelectNote} />
          </div>
        ) : viewMode === "edit" ? (
          <NoteEditor
            noteId={editingNoteId}
            onSaved={handleSaved}
            onCancel={handleCancel}
            onDeleted={handleDeleted}
          />
        ) : selectedNoteId ? (
          <NoteView
            noteId={selectedNoteId}
            onEdit={handleEdit}
            onDeleted={handleDeleted}
            onNavigateNote={handleSelectNote}
            refreshKey={refreshKey}
          />
        ) : (
          /* Empty state */
          <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
            <FileText className="h-12 w-12 text-muted-foreground/30" />
            <div>
              <h2 className="text-lg font-semibold text-foreground">
                Your Second Brain
              </h2>
              <p className="mt-1 max-w-sm text-sm text-muted-foreground">
                Create notes to capture ideas, link them together with
                [[wiki-links]], and organize with #tags.
              </p>
            </div>
            <Button onClick={handleNewNote} className="gap-1.5">
              <Plus className="h-4 w-4" />
              Create a New Note
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
