import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { api } from "@/lib/api";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "sonner";
import { Monitor } from "lucide-react";

interface ModelInfo {
  id: string;
  display_name: string;
  description?: string;
}

interface ModelsResponse {
  models: ModelInfo[];
  current_model: string;
}

export function GeneralTab() {
  const navigate = useNavigate();
  const { refreshSetupStatus } = useAuth();
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [currentModel, setCurrentModel] = useState("");
  const [loading, setLoading] = useState(true);
  const [resettingWizard, setResettingWizard] = useState(false);

  // Screen agent state
  const [screenAgentKey, setScreenAgentKey] = useState<string | null>(null);
  const [generatingScreenKey, setGeneratingScreenKey] = useState(false);

  useEffect(() => {
    loadModels();
    loadScreenAgentKey();
  }, []);

  async function loadModels() {
    try {
      const data = await api.get<ModelsResponse>("/api/settings/models");
      setModels(data.models);
      setCurrentModel(data.current_model);
    } catch {
      toast.error("Failed to load models");
    } finally {
      setLoading(false);
    }
  }

  async function loadScreenAgentKey() {
    try {
      const data = await api.get<{ key: string | null }>("/api/settings/screen-agent/key");
      setScreenAgentKey(data.key);
    } catch {
      // Non-fatal — screen agent section will show generate button
    }
  }

  async function generateScreenAgentKey() {
    setGeneratingScreenKey(true);
    try {
      const data = await api.post<{ key: string }>("/api/settings/screen-agent/key", {});
      setScreenAgentKey(data.key);
      toast.success("Screen agent key generated");
    } catch {
      toast.error("Failed to generate screen agent key");
    } finally {
      setGeneratingScreenKey(false);
    }
  }

  async function handleModelChange(modelId: string) {
    setCurrentModel(modelId);
    try {
      await api.put("/api/settings", { claude_model: modelId });
      toast.success("AI model updated");
    } catch {
      toast.error("Failed to update model");
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <h3 className="text-lg font-semibold">General</h3>
        <p className="text-sm text-muted-foreground">
          Manage your account and AI preferences.
        </p>
      </div>

      {/* AI Model */}
      <div className="space-y-2">
        <Label>Default AI Model</Label>
        <p className="text-xs text-muted-foreground">
          Choose which Claude model Seny uses for conversations.
        </p>
        {loading ? (
          <div className="h-9 w-64 animate-pulse rounded-md bg-muted" />
        ) : (
          <Select value={currentModel} onValueChange={handleModelChange}>
            <SelectTrigger className="w-72">
              <SelectValue placeholder="Select a model" />
            </SelectTrigger>
            <SelectContent>
              {models.map((m) => (
                <SelectItem key={m.id} value={m.id}>
                  {m.display_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </div>

      {/* Screen Agent */}
      <div className="space-y-4 rounded-lg border border-border p-4">
        <div className="flex items-center gap-3">
          <Monitor className="h-5 w-5 text-violet-400" />
          <div>
            <p className="font-medium">Screen Agent</p>
            <p className="text-sm text-muted-foreground">
              API key for the desktop screen awareness agent
            </p>
          </div>
        </div>

        <div className="space-y-4">
          {screenAgentKey ? (
            <div className="space-y-2">
              <Label>API Key</Label>
              <div className="flex gap-2">
                <Input
                  readOnly
                  value={screenAgentKey}
                  className="font-mono text-xs"
                />
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    navigator.clipboard.writeText(screenAgentKey);
                    toast.success("Key copied to clipboard");
                  }}
                >
                  Copy
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={generatingScreenKey}
                  onClick={generateScreenAgentKey}
                >
                  {generatingScreenKey ? "Generating..." : "Regenerate"}
                </Button>
              </div>
            </div>
          ) : (
            <div className="space-y-2">
              <p className="text-sm text-muted-foreground">No key generated yet.</p>
              <Button
                size="sm"
                disabled={generatingScreenKey}
                onClick={generateScreenAgentKey}
              >
                {generatingScreenKey ? "Generating..." : "Generate Key"}
              </Button>
            </div>
          )}
          <p className="text-xs text-amber-500">
            Anyone with this key can send screenshots to your account. Store it securely.
          </p>
        </div>
      </div>

      {/* Theme (future) */}
      <div className="space-y-2">
        <Label>Theme</Label>
        <p className="text-xs text-muted-foreground">
          Dark mode is the default. More themes coming soon.
        </p>
        <Select value="dark" disabled>
          <SelectTrigger className="w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="dark">Dark</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {/* Setup Wizard */}
      <div className="space-y-2">
        <Label>Setup Wizard</Label>
        <p className="text-sm text-muted-foreground">
          Re-run the setup wizard to update your name, pronouns, key people,
          projects, and preferences. Your existing data will be pre-filled.
        </p>
        <Button
          variant="outline"
          disabled={resettingWizard}
          onClick={async () => {
            setResettingWizard(true);
            try {
              await api.post("/api/settings/setup/reset");
              await refreshSetupStatus();
              navigate("/setup");
            } catch {
              toast.error("Failed to reset setup.");
            } finally {
              setResettingWizard(false);
            }
          }}
        >
          {resettingWizard ? "Resetting..." : "Re-run Setup Wizard"}
        </Button>
      </div>

      {/* Account info */}
      <div className="space-y-2">
        <Label>Account</Label>
        <p className="text-sm text-muted-foreground">
          Logged in. Manage your session from the user menu in the sidebar.
        </p>
      </div>
    </div>
  );
}
