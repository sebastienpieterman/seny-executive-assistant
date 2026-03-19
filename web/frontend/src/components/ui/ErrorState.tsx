import { AlertCircle, RefreshCw, WifiOff } from "lucide-react";
import { Button } from "@/components/ui/button";

interface ErrorStateProps {
  message?: string;
  onRetry?: () => void;
}

/** API failure state with retry button */
export function ErrorState({
  message = "Something went wrong. Please try again.",
  onRetry,
}: ErrorStateProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 p-8 text-center">
      <AlertCircle className="h-10 w-10 text-destructive/60" />
      <p className="text-sm text-muted-foreground">{message}</p>
      {onRetry && (
        <Button
          variant="outline"
          size="sm"
          onClick={onRetry}
          className="gap-1.5"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          Try Again
        </Button>
      )}
    </div>
  );
}

/** Offline banner — sits at top of the app */
export function OfflineBanner() {
  return (
    <div className="flex items-center justify-center gap-2 bg-destructive/10 px-4 py-2 text-sm text-destructive">
      <WifiOff className="h-4 w-4" />
      <span>You're offline. Some features may not work.</span>
    </div>
  );
}

interface EmptyStateProps {
  icon: React.ElementType;
  title: string;
  description?: string;
  action?: React.ReactNode;
}

/** Generic empty state with icon, title, description, and optional action */
export function EmptyState({ icon: Icon, title, description, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-12 text-center">
      <Icon className="h-10 w-10 text-muted-foreground/30" />
      <div>
        <p className="text-sm font-medium text-foreground">{title}</p>
        {description && (
          <p className="mt-1 max-w-xs text-xs text-muted-foreground">
            {description}
          </p>
        )}
      </div>
      {action}
    </div>
  );
}
