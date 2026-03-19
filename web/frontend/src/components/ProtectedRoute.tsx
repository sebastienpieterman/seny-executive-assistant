import { Navigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import type { ReactNode } from "react";

export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { isAuthenticated, setupComplete, setupLoading } = useAuth();

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  if (setupLoading) {
    // Show minimal loading while checking setup status
    return (
      <div className="flex h-screen items-center justify-center bg-[#0f0f0f]">
        <div className="text-muted-foreground">Loading...</div>
      </div>
    );
  }

  if (!setupComplete) {
    return <Navigate to="/setup" replace />;
  }

  return <>{children}</>;
}
