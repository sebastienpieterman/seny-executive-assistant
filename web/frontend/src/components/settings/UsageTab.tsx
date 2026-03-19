import { useState, useEffect } from "react";
import { api } from "@/lib/api";
import { Separator } from "@/components/ui/separator";
import { Button } from "@/components/ui/button";

interface DailyBreakdown {
  date: string;
  requests: number;
  cost: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
}

interface UsageSummary {
  total_requests: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cache_write_tokens: number;
  total_cache_read_tokens: number;
  total_cost_usd: number;
  cache_hit_rate: number;
  daily_breakdown: DailyBreakdown[];
}

const DAY_OPTIONS = [7, 30, 90] as const;
type DayOption = (typeof DAY_OPTIONS)[number];

function formatCost(usd: number): string {
  if (usd < 0.01) return "<$0.01";
  return `$${usd.toFixed(2)}`;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function formatPercent(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

export function UsageTab() {
  const [days, setDays] = useState<DayOption>(30);
  const [data, setData] = useState<UsageSummary | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    setData(null);
    api
      .get<UsageSummary>(`/api/usage?days=${days}`)
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [days]);

  return (
    <div className="space-y-6">
      {/* Header + Days Selector */}
      <div className="flex items-start justify-between">
        <div>
          <h3 className="text-lg font-semibold">Usage</h3>
          <p className="text-sm text-muted-foreground">
            Claude API token usage and costs.
          </p>
        </div>
        <div className="flex gap-1">
          {DAY_OPTIONS.map((d) => (
            <Button
              key={d}
              variant={days === d ? "secondary" : "ghost"}
              size="sm"
              className="h-7 px-3 text-xs"
              onClick={() => setDays(d)}
            >
              {d}d
            </Button>
          ))}
        </div>
      </div>

      <Separator />

      {loading ? (
        <div className="space-y-4">
          <div className="grid grid-cols-3 gap-3">
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-20 animate-pulse rounded-lg bg-muted" />
            ))}
          </div>
          <div className="h-40 animate-pulse rounded-lg bg-muted" />
        </div>
      ) : !data ? (
        <p className="text-sm text-muted-foreground">Failed to load usage data.</p>
      ) : (
        <>
          {/* Summary Cards */}
          <div className="grid grid-cols-3 gap-3">
            <div className="rounded-lg border border-border bg-card p-4">
              <p className="text-xs text-muted-foreground">Total Cost</p>
              <p className="mt-1 text-2xl font-semibold">{formatCost(data.total_cost_usd)}</p>
              <p className="text-xs text-muted-foreground">last {days} days</p>
            </div>
            <div className="rounded-lg border border-border bg-card p-4">
              <p className="text-xs text-muted-foreground">Requests</p>
              <p className="mt-1 text-2xl font-semibold">{data.total_requests.toLocaleString()}</p>
              <p className="text-xs text-muted-foreground">API calls</p>
            </div>
            <div className="rounded-lg border border-border bg-card p-4">
              <p className="text-xs text-muted-foreground">Cache Hit Rate</p>
              <p className="mt-1 text-2xl font-semibold">{formatPercent(data.cache_hit_rate)}</p>
              <p className="text-xs text-muted-foreground">tokens served from cache</p>
            </div>
          </div>

          {/* Token Breakdown */}
          <div className="space-y-2">
            <h4 className="text-sm font-medium">Token Breakdown</h4>
            <div className="rounded-lg border border-border bg-card divide-y divide-border">
              {[
                { label: "Input", value: data.total_input_tokens },
                { label: "Output", value: data.total_output_tokens },
                { label: "Cache Write", value: data.total_cache_write_tokens },
                { label: "Cache Read", value: data.total_cache_read_tokens },
              ].map(({ label, value }) => (
                <div key={label} className="flex items-center justify-between px-4 py-2.5">
                  <span className="text-sm text-muted-foreground">{label}</span>
                  <span className="text-sm font-medium tabular-nums">{formatTokens(value)}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Daily Breakdown */}
          {data.daily_breakdown.length > 0 && (
            <div className="space-y-2">
              <h4 className="text-sm font-medium">Daily Breakdown</h4>
              <div className="rounded-lg border border-border overflow-hidden">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border bg-muted/40">
                      <th className="px-3 py-2 text-left font-medium text-muted-foreground">Date</th>
                      <th className="px-3 py-2 text-right font-medium text-muted-foreground">Requests</th>
                      <th className="px-3 py-2 text-right font-medium text-muted-foreground">Cost</th>
                      <th className="px-3 py-2 text-right font-medium text-muted-foreground">Cache Hit</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {data.daily_breakdown.map((row) => {
                      const totalToks =
                        row.input_tokens + row.output_tokens +
                        row.cache_read_tokens + row.cache_write_tokens;
                      const hitRate = totalToks > 0
                        ? row.cache_read_tokens / totalToks
                        : 0;
                      return (
                        <tr key={row.date} className="bg-card hover:bg-muted/20 transition-colors">
                          <td className="px-3 py-2 text-muted-foreground">{row.date}</td>
                          <td className="px-3 py-2 text-right tabular-nums">{row.requests}</td>
                          <td className="px-3 py-2 text-right tabular-nums">{formatCost(row.cost)}</td>
                          <td className="px-3 py-2 text-right tabular-nums">{formatPercent(hitRate)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
