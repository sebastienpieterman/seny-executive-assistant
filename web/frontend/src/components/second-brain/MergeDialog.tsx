import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

interface MergeDialogProps {
  open: boolean;
  category: string;
  winner: { id: number; name?: string; title?: string } | null;
  loser: { id: number; name?: string; title?: string } | null;
  onConfirm: () => void;
  onCancel: () => void;
}

export function MergeDialog({ open, category, winner, loser, onConfirm, onCancel }: MergeDialogProps) {
  if (!winner || !loser) return null;

  const winnerLabel = category === "people" ? winner.name : winner.title;
  const loserLabel = category === "people" ? loser.name : loser.title;

  const transferMessage = category === "people"
    ? "All follow-ups, activity history, detected actions, entity mappings, and cross-references will be transferred to the kept record."
    : "Notes, tags, and cross-references will be merged into the kept record.";

  return (
    <AlertDialog open={open} onOpenChange={(v) => !v && onCancel()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Confirm Merge</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <span className="inline-block rounded bg-green-500/10 px-2 py-0.5 text-xs font-medium text-green-600">Keep</span>
                <span className="font-medium text-foreground">{winnerLabel}</span>
              </div>
              <div className="flex items-center gap-2">
                <span className="inline-block rounded bg-red-500/10 px-2 py-0.5 text-xs font-medium text-red-600">Remove</span>
                <span className="font-medium text-foreground">{loserLabel}</span>
              </div>
              <p className="text-sm text-muted-foreground">{transferMessage}</p>
              <p className="text-sm font-medium text-destructive">This cannot be undone.</p>
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={onConfirm}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            Merge
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
