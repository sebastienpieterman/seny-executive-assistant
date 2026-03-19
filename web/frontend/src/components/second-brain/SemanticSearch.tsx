import { useState } from "react";
import { Search, Loader2, Brain } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { api } from "@/lib/api";

interface SemanticResult {
  entity_type: string;
  id: string;
  text: string;
  metadata: Record<string, string>;
  distance: number;
  similarity: number;
}

interface SemanticSearchProps {
  onSelect?: (category: string, id: number) => void;
}

const ENTITY_COLORS: Record<string, string> = {
  items: "bg-blue-500/10 text-blue-600 dark:text-blue-400",
  notes: "bg-green-500/10 text-green-600 dark:text-green-400",
  conversations: "bg-purple-500/10 text-purple-600 dark:text-purple-400",
  people: "bg-orange-500/10 text-orange-600 dark:text-orange-400",
  projects: "bg-cyan-500/10 text-cyan-600 dark:text-cyan-400",
  ideas: "bg-yellow-500/10 text-yellow-600 dark:text-yellow-400",
};

// Entity types that map directly to a Second Brain detail view
const NAVIGABLE: Record<string, string> = {
  people: "people",
  projects: "projects",
  ideas: "ideas",
};

function parseNumericId(id: string): number | null {
  const match = id.match(/_(\d+)$/);
  return match ? parseInt(match[1], 10) : null;
}

export function SemanticSearch({ onSelect }: SemanticSearchProps) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SemanticResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [embeddingsEnabled, setEmbeddingsEnabled] = useState(true);
  const [lastQuery, setLastQuery] = useState("");

  const handleSearch = async () => {
    const q = query.trim();
    if (!q || loading) return;
    setLoading(true);
    setSearched(true);
    setLastQuery(q);
    try {
      const data = await api.post<{
        results: SemanticResult[];
        embeddings_enabled: boolean;
      }>("/api/search/semantic", { query: q, n_results: 15 });
      setResults(data.results);
      setEmbeddingsEnabled(data.embeddings_enabled);
    } catch {
      setResults([]);
    } finally {
      setLoading(false);
    }
  };

  const handleResultClick = (r: SemanticResult) => {
    const category = NAVIGABLE[r.entity_type];
    if (!category || !onSelect) return;
    const numericId = parseNumericId(r.id);
    if (numericId !== null) onSelect(category, numericId);
  };

  return (
    <div className="flex h-full flex-col">
      {/* Search input bar */}
      <div className="border-b border-border px-4 py-2">
        <div className="flex gap-2">
          <div className="relative flex-1">
            <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
              placeholder="Search by concept..."
              className="pl-9"
              autoFocus
            />
          </div>
          <Button onClick={handleSearch} disabled={loading || !query.trim()}>
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : "Search"}
          </Button>
        </div>
      </div>

      {/* Results area */}
      <div className="flex-1 overflow-y-auto">
        {!searched ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
            <Brain className="h-8 w-8 text-muted-foreground/30" />
            <p className="text-sm text-muted-foreground">
              Search across all your data by concept — emails, notes, conversations, people, projects, and ideas.
            </p>
            <p className="text-xs text-muted-foreground/60">
              Try: &ldquo;product launch&rdquo;, &ldquo;budget concerns&rdquo;, &ldquo;follow up with Sarah&rdquo;
            </p>
          </div>
        ) : loading ? (
          <div className="flex h-full items-center justify-center">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : !embeddingsEnabled ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
            <p className="text-sm text-muted-foreground">
              Semantic search requires a Voyage AI API key to be configured.
            </p>
            <p className="text-xs text-muted-foreground/60">
              Add VOYAGE_API_KEY to your Railway environment variables to enable this feature.
            </p>
          </div>
        ) : results.length === 0 ? (
          <div className="flex h-full items-center justify-center">
            <p className="text-sm text-muted-foreground">
              No results found for &ldquo;{lastQuery}&rdquo;
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {results.map((r) => {
              const navigable = !!NAVIGABLE[r.entity_type];
              return (
                <li
                  key={`${r.entity_type}-${r.id}`}
                  onClick={() => handleResultClick(r)}
                  className={`px-4 py-3 hover:bg-accent/30 ${navigable ? "cursor-pointer" : "cursor-default"}`}
                >
                  <div className="mb-1 flex items-center gap-2">
                    <Badge
                      className={`px-1.5 py-0 text-xs font-medium ${ENTITY_COLORS[r.entity_type] ?? ""}`}
                    >
                      {r.entity_type}
                    </Badge>
                    <span className="ml-auto text-xs text-muted-foreground">
                      {Math.round(r.similarity * 100)}% match
                    </span>
                  </div>
                  <p className="line-clamp-2 text-sm text-foreground">
                    {r.metadata?.title ?? r.metadata?.name ?? r.text.slice(0, 120)}
                  </p>
                  {(r.metadata?.title || r.metadata?.name) && (
                    <p className="mt-0.5 line-clamp-1 text-xs text-muted-foreground">
                      {r.text.slice(0, 100)}
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
