import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { toast } from "sonner";

export function SettingsPage() {
  const { refreshSetupStatus } = useAuth();
  const navigate = useNavigate();
  const [resetting, setResetting] = useState(false);

  const handleRerunWizard = async () => {
    setResetting(true);
    try {
      await api.post("/api/settings/setup/reset");
      await refreshSetupStatus();
      navigate("/setup");
    } catch {
      toast.error("Failed to reset setup.");
    } finally {
      setResetting(false);
    }
  };

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Settings</h1>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Setup Wizard</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground mb-4">
            Re-run the setup wizard to update your name, pronouns, key people,
            projects, and preferences. Your existing data will be pre-filled.
          </p>
          <Button
            variant="outline"
            onClick={handleRerunWizard}
            disabled={resetting}
          >
            {resetting ? "Resetting..." : "Re-run Setup Wizard"}
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
