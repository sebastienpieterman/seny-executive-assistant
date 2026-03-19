import { cn } from "@/lib/utils";
import { useIsMobile } from "@/hooks/useMediaQuery";

interface SlideOverPanelProps {
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
}

export function SlideOverPanel({ open, onClose, children }: SlideOverPanelProps) {
  const isMobile = useIsMobile();

  if (!open) return null;

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 z-40 bg-black/40 transition-opacity"
        onClick={onClose}
      />
      {/* Panel — bottom sheet on mobile, side panel on tablet/desktop */}
      <div
        className={cn(
          "fixed z-50 bg-background border-border shadow-xl",
          "animate-in duration-200",
          isMobile
            ? "inset-x-0 bottom-0 h-[85vh] rounded-t-2xl border-t slide-in-from-bottom"
            : "right-0 top-0 h-full w-[400px] max-w-[90vw] border-l slide-in-from-right"
        )}
      >
        {/* Drag handle for mobile */}
        {isMobile && (
          <div className="flex justify-center py-2">
            <div className="h-1 w-10 rounded-full bg-muted-foreground/30" />
          </div>
        )}
        <div className={cn("h-full overflow-auto", isMobile && "pb-16")}>
          {children}
        </div>
      </div>
    </>
  );
}
