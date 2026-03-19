import { useState, useEffect } from "react";
import { Brain } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface LCDLayer1Response {
  content: string;
}

interface LCDSynthesisResponse {
  layer2_synthesis: string | null;
}

interface LCDObservation {
  id: number;
  source: string;
  content: string;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relativeTime(isoString: string): string {
  const now = Date.now();
  const then = new Date(isoString).getTime();
  const diffMs = now - then;
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return "just now";
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}d ago`;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function LCDPage() {
  const [layer1Content, setLayer1Content] = useState("");
  const [layer2Synthesis, setLayer2Synthesis] = useState<string | null>(null);
  const [observations, setObservations] = useState<LCDObservation[]>([]);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState("");
  const [refreshMsg, setRefreshMsg] = useState("");

  useEffect(() => {
    // Load Layer 1
    api
      .get<LCDLayer1Response>("/api/lcd/")
      .then((d) => setLayer1Content(d.content || ""))
      .catch(() => {});

    // Load Layer 2 synthesis
    api
      .get<LCDSynthesisResponse>("/api/lcd/synthesis")
      .then((d) => setLayer2Synthesis(d.layer2_synthesis ?? null))
      .catch(() => {});

    // Load observations (limit 20)
    api
      .get<LCDObservation[]>("/api/lcd/observations?limit=20")
      .then((d) => setObservations(d))
      .catch(() => {});
  }, []);

  const saveLayer1 = async () => {
    setSaving(true);
    setSaveMsg("");
    try {
      await api.put("/api/lcd/", { content: layer1Content });
      setSaveMsg("Saved");
      setTimeout(() => setSaveMsg(""), 3000);
    } catch {
      setSaveMsg("Save failed");
      setTimeout(() => setSaveMsg(""), 3000);
    } finally {
      setSaving(false);
    }
  };

  const refreshSynthesis = async () => {
    setRefreshMsg("");
    try {
      await api.post("/api/lcd/synthesis/refresh");
      setRefreshMsg("Cache cleared — will re-synthesize on next chat.");
    } catch {
      setRefreshMsg("Refresh failed.");
    }
    setTimeout(() => setRefreshMsg(""), 5000);
  };

  return (
    <div className="p-6 max-w-3xl mx-auto space-y-8">
      {/* Page header */}
      <h1 className="text-2xl font-semibold flex items-center gap-2">
        <Brain className="h-6 w-6" />
        Living Context
      </h1>

      {/* ------------------------------------------------------------------ */}
      {/* Section 1 — Layer 1 editor */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <CardHeader>
          <CardTitle>Layer 1 — Core Identity</CardTitle>
          <p className="text-sm text-muted-foreground">
            Seny's foundational understanding of who you are. Edit here —
            changes take effect on the next conversation without a deploy.
          </p>
        </CardHeader>
        <CardContent className="space-y-3">
          <Textarea
            className="min-h-[300px] font-mono text-sm resize-y"
            value={layer1Content}
            onChange={(e) => setLayer1Content(e.target.value)}
            placeholder="No Layer 1 content yet. Write a description of who you are and what matters to you."
          />
          <div className="flex items-center gap-3">
            <Button onClick={saveLayer1} disabled={saving} size="sm">
              {saving ? "Saving..." : "Save"}
            </Button>
            {saveMsg && (
              <span
                className={`text-sm ${
                  saveMsg === "Saved" ? "text-green-500" : "text-destructive"
                }`}
              >
                {saveMsg}
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* Section 2 — Layer 2 read-only synthesis */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <CardHeader>
          <CardTitle>Layer 2 — What's Going On Right Now</CardTitle>
          <p className="text-sm text-muted-foreground">
            Synthesized from the observation log. Refreshes automatically every
            2 hours when there are new observations.
          </p>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="min-h-[120px] rounded-md border border-input bg-muted/30 px-3 py-2 text-sm text-muted-foreground">
            {layer2Synthesis ? (
              <p className="whitespace-pre-wrap text-foreground">
                {layer2Synthesis}
              </p>
            ) : (
              <p className="italic">
                (No synthesis yet — observation log is empty)
              </p>
            )}
          </div>
          <div className="flex items-center gap-3">
            <Button onClick={refreshSynthesis} variant="outline" size="sm">
              Refresh synthesis
            </Button>
            {refreshMsg && (
              <span className="text-sm text-muted-foreground">{refreshMsg}</span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* Section 3 — Observation log */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <CardHeader>
          <CardTitle>Recent Observations</CardTitle>
          <p className="text-sm text-muted-foreground">
            What Seny has noticed or been told. Observations build up
            automatically as you use Seny.
          </p>
        </CardHeader>
        <CardContent>
          {observations.length === 0 ? (
            <p className="text-sm text-muted-foreground italic">
              (No observations yet)
            </p>
          ) : (
            <ul className="space-y-3">
              {observations.map((obs) => (
                <li
                  key={obs.id}
                  className="flex flex-col gap-1 rounded-md border border-border p-3 text-sm"
                >
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <Badge variant="secondary" className="text-xs">
                      {obs.source}
                    </Badge>
                    <span>{relativeTime(obs.created_at)}</span>
                  </div>
                  <p className="text-foreground">{obs.content}</p>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export default LCDPage;
