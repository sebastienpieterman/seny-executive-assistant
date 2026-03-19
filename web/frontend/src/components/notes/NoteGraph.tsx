import { useEffect, useRef, useState, useCallback } from "react";
import * as d3 from "d3";
import { api } from "@/lib/api";
import { Network } from "lucide-react";

interface GraphNode extends d3.SimulationNodeDatum {
  id: number;
  title: string;
  tags: string[];
  size: number;
}

interface GraphEdge {
  source: number | GraphNode;
  target: number | GraphNode;
}

interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  tag_clusters?: { tag: string; notes: number[] }[];
}

const TAG_COLORS = [
  "#4A90E2", "#27ae60", "#e74c3c", "#9b59b6", "#f39c12",
  "#1abc9c", "#e67e22", "#3498db", "#e91e63", "#00bcd4",
];

interface NoteGraphProps {
  onSelectNote: (id: number) => void;
}

export function NoteGraph({ onSelectNote }: NoteGraphProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const simulationRef = useRef<d3.Simulation<GraphNode, GraphEdge> | null>(null);
  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [loading, setLoading] = useState(true);
  const [tagColorMap] = useState<Record<string, string>>({});

  const getTagColor = useCallback(
    (tag: string) => {
      if (!tagColorMap[tag]) {
        const index = Object.keys(tagColorMap).length % TAG_COLORS.length;
        tagColorMap[tag] = TAG_COLORS[index];
      }
      return tagColorMap[tag];
    },
    [tagColorMap]
  );

  useEffect(() => {
    async function fetchGraph() {
      setLoading(true);
      try {
        const data = await api.get<GraphData>("/api/notes/graph");
        setGraphData(data);
      } catch {
        setGraphData(null);
      } finally {
        setLoading(false);
      }
    }
    fetchGraph();
  }, []);

  useEffect(() => {
    if (!graphData || !svgRef.current || !containerRef.current) return;
    if (graphData.nodes.length === 0) return;

    // Cleanup previous simulation
    if (simulationRef.current) simulationRef.current.stop();

    const container = containerRef.current.getBoundingClientRect();
    const width = container.width;
    const height = container.height;

    const svg = d3
      .select(svgRef.current)
      .attr("width", width)
      .attr("height", height);

    svg.selectAll("*").remove();

    // Zoom
    const g = svg.append("g");
    const zoom = d3
      .zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.2, 4])
      .on("zoom", (event) => {
        g.attr("transform", event.transform);
      });
    svg.call(zoom);

    // Build tag colors
    graphData.nodes.forEach((n) =>
      n.tags.forEach((t) => getTagColor(t))
    );

    // Links
    const link = g
      .append("g")
      .selectAll("line")
      .data(graphData.edges)
      .enter()
      .append("line")
      .attr("stroke", "#94a3b8")
      .attr("stroke-opacity", 0.9)
      .attr("stroke-width", 2);

    // Nodes
    const node = g
      .append("g")
      .selectAll<SVGGElement, GraphNode>("g")
      .data(graphData.nodes)
      .enter()
      .append("g")
      .attr("cursor", "pointer")
      .call(
        d3
          .drag<SVGGElement, GraphNode>()
          .on("start", (event, d) => {
            if (!event.active) simulationRef.current?.alphaTarget(0.3).restart();
            d.fx = d.x;
            d.fy = d.y;
          })
          .on("drag", (event, d) => {
            d.fx = event.x;
            d.fy = event.y;
          })
          .on("end", (event, d) => {
            if (!event.active) simulationRef.current?.alphaTarget(0);
            d.fx = null;
            d.fy = null;
          })
      )
      .on("click", (_event, d) => {
        onSelectNote(d.id);
      });

    // Node circles
    node
      .append("circle")
      .attr("r", (d) => 8 + d.size * 2)
      .attr("fill", (d) =>
        d.tags.length > 0 ? getTagColor(d.tags[0]) : "#666"
      )
      .attr("stroke", "#1a1a1a")
      .attr("stroke-width", 2);

    // Node labels
    node
      .append("text")
      .attr("dy", (d) => -(12 + d.size * 2))
      .attr("text-anchor", "middle")
      .attr("fill", "#e5e5e5")
      .attr("font-size", "11px")
      .text((d) =>
        d.title.length > 22 ? d.title.substring(0, 22) + "..." : d.title
      );

    // Hover tooltip
    node.append("title").text((d) => d.title);

    // Force simulation
    const simulation = d3
      .forceSimulation<GraphNode>(graphData.nodes)
      .force(
        "link",
        d3
          .forceLink<GraphNode, GraphEdge>(graphData.edges)
          .id((d) => d.id)
          .distance(100)
      )
      .force("charge", d3.forceManyBody().strength(-200))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force(
        "collision",
        d3.forceCollide<GraphNode>().radius((d) => 15 + d.size * 2)
      );

    simulationRef.current = simulation;

    simulation.on("tick", () => {
      link
        .attr("x1", (d) => (d.source as GraphNode).x ?? 0)
        .attr("y1", (d) => (d.source as GraphNode).y ?? 0)
        .attr("x2", (d) => (d.target as GraphNode).x ?? 0)
        .attr("y2", (d) => (d.target as GraphNode).y ?? 0);

      node.attr("transform", (d) => `translate(${d.x ?? 0},${d.y ?? 0})`);
    });

    return () => {
      simulation.stop();
    };
  }, [graphData, getTagColor, onSelectNote]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-sm text-muted-foreground">Loading graph...</div>
      </div>
    );
  }

  if (!graphData || graphData.nodes.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
        <Network className="h-10 w-10 text-muted-foreground/30" />
        <div>
          <p className="text-sm font-medium text-foreground">No connections yet</p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Create notes with [[wiki-links]] to see connections
          </p>
        </div>
      </div>
    );
  }

  return (
    <div ref={containerRef} className="h-full w-full">
      <svg ref={svgRef} className="h-full w-full" />
      {/* Legend */}
      {graphData.tag_clusters && graphData.tag_clusters.length > 0 && (
        <div className="absolute bottom-4 left-4 flex flex-wrap gap-2 rounded-lg bg-neutral-900/80 px-3 py-2 backdrop-blur">
          {graphData.tag_clusters
            .sort((a, b) => b.notes.length - a.notes.length)
            .slice(0, 8)
            .map((tc) => (
              <div key={tc.tag} className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <span
                  className="h-2.5 w-2.5 rounded-full"
                  style={{ background: getTagColor(tc.tag) }}
                />
                {tc.tag} ({tc.notes.length})
              </div>
            ))}
        </div>
      )}
    </div>
  );
}
