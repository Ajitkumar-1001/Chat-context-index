/** Chronological hierarchy; identical grouping/reuse rules to Python tree.py. */
import { prefixedId } from "./ids.js";

export interface TreeNode {
  node_id: string;
  history_id: string;
  parent_id: string | null;
  sibling_order: number;
  message_range: string;
  title: string | null;
  summary: string | null;
  state: string;
  index_revision: number;
}

export function planHierarchy(leaves: TreeNode[], previous: TreeNode[], maxChildren: number, revision: number): {
  nodes: TreeNode[]; summaries: Map<string, string[]>;
} {
  if (maxChildren < 2) throw new RangeError("treeMaxChildren must be at least 2");
  const children = new Map<string, TreeNode[]>();
  const byId = new Map(previous.map(n => [n.node_id, n]));
  for (const node of previous) {
    if (node.parent_id) children.set(node.parent_id, [...(children.get(node.parent_id) ?? []), node]);
  }
  const reusable = new Map<string, TreeNode>();
  for (const [parent, group] of children) {
    if (byId.has(parent)) reusable.set(JSON.stringify(group.sort((a, b) => a.sibling_order - b.sibling_order).map(n => n.node_id)), byId.get(parent)!);
  }
  const nodes = new Map(leaves.map(n => [n.node_id, { ...n, parent_id: null as string | null }]));
  let layer = [...nodes.keys()].sort((a, b) => Number(nodes.get(a)!.message_range.split("-")[0]) - Number(nodes.get(b)!.message_range.split("-")[0]));
  const summaries = new Map<string, string[]>();
  while (layer.length > maxChildren) {
    const nextLayer: string[] = [];
    for (let start = 0; start < layer.length; start += maxChildren) {
      const group = layer.slice(start, start + maxChildren);
      if (group.length === 1) { nextLayer.push(...group); continue; }
      let parent = reusable.get(JSON.stringify(group));
      if (!parent) {
        parent = {
          node_id: prefixedId("n"), history_id: nodes.get(group[0])!.history_id,
          parent_id: null, sibling_order: 0,
          message_range: nodes.get(group[0])!.message_range.split("-")[0] + "-" + nodes.get(group[group.length - 1])!.message_range.split("-")[1],
          title: null, summary: null, state: "published", index_revision: revision,
        };
        summaries.set(parent.node_id, group);
      }
      nodes.set(parent.node_id, { ...parent, parent_id: null });
      group.forEach((nid, order) => nodes.set(nid, { ...nodes.get(nid)!, parent_id: parent!.node_id, sibling_order: order }));
      nextLayer.push(parent.node_id);
    }
    layer = nextLayer;
  }
  layer.forEach((nid, order) => nodes.set(nid, { ...nodes.get(nid)!, parent_id: null, sibling_order: order }));
  return { nodes: [...nodes.values()], summaries };
}
