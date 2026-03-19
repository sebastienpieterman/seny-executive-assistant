import { useEffect, useState } from "react";
import { ChevronDown } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { api } from "@/lib/api";

interface ModelInfo {
  id: string;
  display_name: string;
  description?: string | null;
}

interface ModelsResponse {
  models: ModelInfo[];
  current_model: string;
}

interface ModelSelectorProps {
  conversationId: string | null;
  selectedModel: string | null;
  onModelChange: (modelId: string) => void;
}

/** Shorten display names for compact UI. Works for any model version. */
function shortName(displayName: string): string {
  // "Claude Sonnet 4.6 (Balanced)" → "Sonnet 4.6"
  return displayName.replace(/^Claude\s+/i, "").replace(/\s*\(.*\)$/, "").trim();
}

export function ModelSelector({
  conversationId,
  selectedModel,
  onModelChange,
}: ModelSelectorProps) {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [defaultModel, setDefaultModel] = useState<string>("");

  useEffect(() => {
    api
      .get<ModelsResponse>("/api/settings/models")
      .then((data) => {
        setModels(data.models);
        setDefaultModel(data.current_model);
      })
      .catch(() => {
        // Fallback models
        setModels([
          { id: "claude-sonnet-4-5-20250929", display_name: "Claude Sonnet 4.5 (Balanced)" },
        ]);
      });
  }, []);

  const activeId = selectedModel || defaultModel;
  const activeModel = models.find((m) => m.id === activeId);
  const label = activeModel ? shortName(activeModel.display_name) : "Model";

  function handleSelect(modelId: string) {
    onModelChange(modelId);
    // Persist to conversation if one exists
    if (conversationId) {
      api
        .patch(`/api/conversations/${conversationId}/model`, { model: modelId })
        .catch(() => {
          /* silent */
        });
    }
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-[#1e1e1e] hover:text-foreground">
          {label}
          <ChevronDown className="h-3 w-3" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-[200px]">
        {models.map((m) => (
          <DropdownMenuItem
            key={m.id}
            onClick={() => handleSelect(m.id)}
            className={m.id === activeId ? "bg-accent" : ""}
          >
            <div>
              <p className="text-sm font-medium">{shortName(m.display_name)}</p>
              {m.description && (
                <p className="text-xs text-muted-foreground">{m.description}</p>
              )}
            </div>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
