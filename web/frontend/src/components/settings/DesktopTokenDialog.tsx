import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Copy, Key, AlertTriangle } from "lucide-react";
import { api } from "@/lib/api";
import { toast } from "sonner";

interface DesktopTokenDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function DesktopTokenDialog({
  open,
  onOpenChange,
}: DesktopTokenDialogProps) {
  const [token, setToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function generateToken() {
    setLoading(true);
    try {
      const data = await api.post<{ access_token: string }>(
        "/api/auth/desktop-token"
      );
      setToken(data.access_token);
      try {
        await navigator.clipboard.writeText(data.access_token);
        toast.success("Desktop token generated and copied to clipboard");
      } catch {
        toast.success("Desktop token generated");
      }
    } catch {
      toast.error("Failed to generate desktop token");
    } finally {
      setLoading(false);
    }
  }

  async function copyToken() {
    if (!token) return;
    try {
      await navigator.clipboard.writeText(token);
      toast.success("Token copied to clipboard");
    } catch {
      toast.error("Failed to copy token");
    }
  }

  function handleOpenChange(open: boolean) {
    if (!open) {
      setToken(null);
    }
    onOpenChange(open);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Key className="h-5 w-5" />
            Desktop Token
          </DialogTitle>
          <DialogDescription>
            Generate a token to use Seny from the desktop CLI.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {!token ? (
            <Button onClick={generateToken} disabled={loading}>
              {loading ? "Generating..." : "Generate Token"}
            </Button>
          ) : (
            <>
              <div className="flex gap-2">
                <Input
                  readOnly
                  value={token}
                  className="font-mono text-xs"
                />
                <Button variant="outline" size="icon" onClick={copyToken}>
                  <Copy className="h-4 w-4" />
                </Button>
              </div>

              <div className="flex items-start gap-2 rounded-md border border-yellow-500/30 bg-yellow-500/10 p-3">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-yellow-500" />
                <p className="text-xs text-yellow-200">
                  This token provides full access to your account. Keep it
                  secret and never share it publicly. You can generate a new
                  one at any time, which will invalidate the old one.
                </p>
              </div>
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
