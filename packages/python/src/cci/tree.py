"""Chronological, bounded-fanout hierarchy over immutable summarized chunks.

Inspired by ChatIndex's topic hierarchy; independently implemented. Contiguous grouping
is deterministic, not LLM topic-boundary detection. Unchanged child lists reuse their
published parent and summary. No provider or database work happens in this planner.
"""

from dataclasses import replace

from ._ids import prefixed_id
from .models import Node


def plan_hierarchy(
    leaves: list[Node], previous: list[Node], max_children: int, revision: int,
) -> tuple[list[Node], dict[str, list[str]]]:
    if max_children < 2:
        raise ValueError("tree_max_children must be at least 2")
    children: dict[str, list[Node]] = {}
    for node in previous:
        if node.parent_id:
            children.setdefault(node.parent_id, []).append(node)
    by_id = {n.node_id: n for n in previous}
    reusable = {
        tuple(n.node_id for n in sorted(group, key=lambda n: n.sibling_order)): by_id[parent]
        for parent, group in children.items() if parent in by_id
    }
    nodes = {n.node_id: replace(n, parent_id=None) for n in leaves}
    layer = sorted(nodes, key=lambda nid: int(nodes[nid].message_range.split("-")[0]))
    summaries: dict[str, list[str]] = {}
    while len(layer) > max_children:
        next_layer = []
        for start in range(0, len(layer), max_children):
            group = layer[start:start + max_children]
            if len(group) == 1:
                next_layer.extend(group)
                continue
            parent = reusable.get(tuple(group))
            if parent is None:
                parent = Node(
                    node_id=prefixed_id("n"), history_id=nodes[group[0]].history_id,
                    parent_id=None, sibling_order=0,
                    message_range=(nodes[group[0]].message_range.split("-")[0] + "-"
                                   + nodes[group[-1]].message_range.split("-")[1]),
                    title=None, summary=None, state="published", index_revision=revision,
                )
                summaries[parent.node_id] = group
            nodes[parent.node_id] = replace(parent, parent_id=None)
            for order, nid in enumerate(group):
                nodes[nid] = replace(nodes[nid], parent_id=parent.node_id, sibling_order=order)
            next_layer.append(parent.node_id)
        layer = next_layer
    for order, nid in enumerate(layer):
        nodes[nid] = replace(nodes[nid], parent_id=None, sibling_order=order)
    return list(nodes.values()), summaries
